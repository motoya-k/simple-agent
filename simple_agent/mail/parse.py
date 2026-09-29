"""Raw RFC 5322 bytes -> :class:`~simple_agent.seams.InboundMessage`.

A pure function, kept apart from IMAP so the part most likely to be wrong —
what counts as "the same conversation", what counts as the body — is tested
against ``.eml`` files without a server.

Conversation identity: an email thread is one session per sender.  The thread
is named by its first message's ``Message-ID``, recovered from ``References``
(oldest first) or, failing that, ``In-Reply-To``.  A thread that several people
reply into splits per sender, because ``chat_id`` is the sender's address —
the safe direction: two strangers on one CC line never share a transcript.
"""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

from ..seams import InboundMessage

PLATFORM = "email"

_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
# The attribution line a mail client puts above the quoted original.
_ATTRIBUTION = re.compile(r"^(On .+ wrote:|.+\d{4}.+<[^>]+>.*:)\s*$")
_TAG = re.compile(r"<[^>]+>")


def parse_message(raw: bytes) -> InboundMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)

    name, address = parseaddr(str(message.get("From", "")))
    address = address.lower()
    message_id = _first_id(message.get("Message-ID", ""))
    subject = str(message.get("Subject", "")).strip()

    body = strip_quoted(_body_text(message))
    attachments = tuple(
        part.get_filename() or "unnamed" for part in message.iter_attachments()
    )

    lines = [f"From: {name} <{address}>" if name else f"From: {address}"]
    if subject:
        lines.append(f"Subject: {subject}")
    if attachments:
        lines.append(f"Attachments (not downloaded): {', '.join(attachments)}")
    text = "\n".join(lines) + "\n\n" + body

    return InboundMessage(
        platform=PLATFORM,
        chat_id=address,
        user_id=address,
        user_name=name,
        chat_name=subject,
        chat_type="dm",
        thread_id=thread_root(message) or message_id,
        message_id=message_id,
        text=text,
        attachments=attachments,
    )


def thread_root(message: EmailMessage) -> str:
    """The Message-ID that started this thread, or "" for a fresh message."""
    references = _MESSAGE_ID.findall(str(message.get("References", "")))
    if references:
        return references[0]
    return _first_id(message.get("In-Reply-To", ""))


def strip_quoted(text: str) -> str:
    """Drop the quoted original a reply carries below (or around) the new text.

    The transcript already holds the earlier messages of the thread, so the
    quote only costs tokens — and gives an old message a second vote.
    """
    kept: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        if _ATTRIBUTION.match(line.strip()):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _body_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_subtype() == "html":
        content = _TAG.sub("", re.sub(r"(?i)<br\s*/?>|</p>", "\n", content))
    return content


def _first_id(value: object) -> str:
    found = _MESSAGE_ID.findall(str(value or ""))
    return found[0] if found else ""
