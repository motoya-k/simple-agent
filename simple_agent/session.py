"""Conversation identity — the one place that decides what "the same conversation" means.

Every wrong answer of the form "the bot replied to the wrong person" or "it
lost the thread" is a bug in :func:`build_session_key`.  So it lives alone in
one small module, with the isolation rules written down rather than spread
across the hosts that call it.

The rules, and the reason each one is the way it is:

* **DMs are never shared.**  Keyed on the chat, so two private conversations
  can never collapse into one.
* **Threads are shared across participants.**  Everyone talking in one thread
  is talking to one agent, which is how a human reads a thread.
* **Non-thread group messages are isolated per participant.**  Two people
  chatting in the same busy channel are not having one conversation.
* **Every key is namespaced** by the profile's namespace and the platform, so
  ids minted by different services can never collide, and two teams sharing a
  deployment share neither their conversations nor their memory.

Ported from Hermes ``gateway/session.py``.  The shape is the same; the types
are simplified (plain strings instead of enums) so a host can name a platform
without registering it first.

Adapted from hermes-agent (https://github.com/NousResearch/hermes-agent),
Copyright (c) 2025 Nous Research, MIT License.  See LICENSE.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any

DEFAULT_PROFILE = "main"
LOCAL_PLATFORM = "local"

#: Chat types that mean "one private conversation with one person".
DM_CHAT_TYPES = frozenset({"dm", "im"})

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class SessionSource:
    """Where a message came from.

    Used for three things: building the session key, routing the reply back,
    and telling the agent where it is (see :meth:`description`).
    """

    platform: str = LOCAL_PLATFORM
    chat_id: str = ""
    chat_type: str = "dm"  # dm | group | channel
    user_id: str = ""
    user_name: str = ""
    chat_name: str = ""
    thread_id: str = ""
    message_id: str = ""
    profile: str = ""  # the profile's namespace; "" means DEFAULT_PROFILE

    @property
    def is_dm(self) -> bool:
        return self.chat_type in DM_CHAT_TYPES

    @property
    def description(self) -> str:
        """A human-readable one-liner, injected into the system prompt.

        An agent that knows it is in a shared channel behaves differently from
        one that thinks it is in a private terminal — mostly around what it is
        willing to write down.
        """
        if self.platform == LOCAL_PLATFORM:
            return "local terminal"

        if self.is_dm:
            who = self.user_name or self.user_id or "user"
            where = f"direct message with {who}"
        else:
            where = f"{self.chat_type}: {self.chat_name or self.chat_id}"
            if self.user_name or self.user_id:
                where += f" (speaking: {self.user_name or self.user_id})"
        if self.thread_id:
            where += f", thread {self.thread_id}"
        return f"{self.platform} — {where}"

    def with_thread(self, thread_id: str) -> "SessionSource":
        return replace(self, thread_id=thread_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "chat_name": self.chat_name,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "profile": self.profile,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionSource":
        known = {f: data.get(f, "") for f in cls.__annotations__}
        known["platform"] = known.get("platform") or LOCAL_PLATFORM
        known["chat_type"] = known.get("chat_type") or "dm"
        return cls(**known)

    @classmethod
    def local(cls, cwd: str = "") -> "SessionSource":
        """The terminal REPL's source. One conversation per working directory."""
        return cls(platform=LOCAL_PLATFORM, chat_id=cwd or "-", chat_type="dm")


def _namespace(profile: str | None) -> str:
    """The first slot of the key: the namespace a profile works in.

    The same value long-term memory uses (see
    :attr:`simple_agent.profile.Profile.namespace`), so one namespace means one
    team's conversations *and* one team's knowledge. Kept as its own slot so
    every key has the same positional layout whether or not it is set.
    """
    if not profile or profile == "default":
        return DEFAULT_PROFILE
    return profile


def build_session_key(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    profile: str | None = None,
) -> str:
    """Build the deterministic identity of a conversation.

    The single source of truth. Hosts must not assemble keys themselves.

    Layout::

        <profile>:<platform>:<chat_type>:<chat_id>[:<thread_id>][:<user_id>]

    ``group_sessions_per_user`` isolates participants in a group chat that is
    not a thread (default on).  ``thread_sessions_per_user`` does the same
    inside threads (default *off*, because a thread reads as one conversation).
    """
    ns = _namespace(profile or source.profile)
    platform = source.platform or "unknown"

    if source.is_dm:
        # chat_id first; fall back to the sender so a source that omits the
        # chat id can never collapse every DM into one shared session — that
        # is cross-user history bleed, not a cosmetic key problem.
        anchor = source.chat_id or source.user_id
        if anchor:
            if source.thread_id:
                return f"{ns}:{platform}:dm:{anchor}:{source.thread_id}"
            return f"{ns}:{platform}:dm:{anchor}"
        if source.thread_id:
            return f"{ns}:{platform}:dm:{source.thread_id}"
        return f"{ns}:{platform}:dm"

    parts = [ns, platform, source.chat_type or "group"]
    if source.chat_id:
        parts.append(source.chat_id)
    if source.thread_id:
        parts.append(source.thread_id)

    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False
    if isolate_user and source.user_id:
        parts.append(source.user_id)

    return ":".join(parts)


def is_shared_multi_user_session(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> bool:
    """True when more than one person can speak into this session.

    Mirrors the isolation rules above. Callers use it to decide what is safe
    to write to durable memory: a note about "the user" means something else
    when six people share the conversation.
    """
    if source.is_dm:
        return False
    if source.thread_id:
        return not thread_sessions_per_user
    return not group_sessions_per_user


def session_slug(session_key: str, length: int = 40) -> str:
    """A filesystem-safe name for a session key.

    Readable prefix plus a hash of the whole key, so two keys that sanitize to
    the same characters still get separate directories.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]
    readable = _UNSAFE_PATH_CHARS.sub("_", session_key).strip("_")
    return f"{readable[:length]}-{digest}" if readable else digest
