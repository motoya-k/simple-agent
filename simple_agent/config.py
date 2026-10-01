"""Configuration — env first, ``~/.simple-agent/config.yaml`` second, defaults last.

Only what a user actually changes lives here.  Everything else (loop limits,
compaction thresholds, timeouts) is a default on the code that uses it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .registry import DEFAULT_IDLE_SECONDS, DEFAULT_MAX_AGENTS

HOME = Path(os.environ.get("SIMPLE_AGENT_HOME", Path.home() / ".simple-agent"))

# The agent's own model, and a cheaper one for the background reviewer that runs
# after every turn. Splitting them is what makes always-on learning affordable.
# Model ids are provider-specific, so each provider brings its own pair.  The
# Bedrock ones are Japan inference profiles: requests stay in Tokyo/Osaka.
PROVIDER_DEFAULT_MODELS = {
    "anthropic": ("claude-opus-5", "claude-haiku-4-5-20251001"),
    "bedrock": (
        "jp.anthropic.claude-sonnet-4-6",
        "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
    "openai": ("gpt-5-codex", "gpt-5-mini"),
}

KEYS = (
    "provider",
    "engine",
    "engine_command",
    "engine_args",
    "pi_command",  # older names for engine_command / engine_args
    "pi_args",
    "model",
    "review_model",
    "learning",
    "database_url",
    "memory_backend",
    "memory_namespace",
    "imap_host",
    "imap_user",
    "imap_mailbox",
    "email_allow",
    "email_tools",
    "max_concurrent_turns",
    "disabled_tools",
)


@dataclass
class Config:
    provider: str = "anthropic"
    # Who runs the turn: "loop" (this repo) or an external harness — "pi",
    # "claude-code", ... (see engines/). engine_command overrides the executable;
    # engine_args is passed through to it, e.g. to override its model flag.
    engine: str = "loop"
    engine_command: str = ""
    engine_args: str = ""
    pi_command: str = ""
    pi_args: str = ""
    model: str = ""  # empty = the provider's default
    review_model: str = ""
    learning: bool = True  # background memory/skill review after each turn
    # Jev yes/no probability below which a review pass is skipped. Only used
    # when TYPESAFE_API_KEY is set. See review_gate.py.
    review_gate_threshold: float = 0.15
    # Empty = SQLite at state_db. A postgresql:// URL moves the transcript
    # store to Postgres; see state_postgres.py for when that is worth it.
    database_url: str = ""
    # Long-term memory: the team's shared knowledge. local | mem0 | hindsight.
    # The namespace is the team or org it belongs to — mem0's app_id,
    # Hindsight's bank. See memory.py.
    memory_backend: str = "local"
    memory_namespace: str = "default"
    # How many conversations a message host keeps live at once, and how long an
    # idle one stays resident. See registry.AgentRegistry.
    # The email host (`simple-agent --email`). The password is read from
    # SIMPLE_AGENT_IMAP_PASSWORD only, never from config.yaml. email_allow is
    # comma-separated addresses or @domains; email_tools is the comma-separated
    # toolset for mail, read-only by default. See host.py for why.
    imap_host: str = ""
    imap_user: str = ""
    imap_mailbox: str = "INBOX"
    email_allow: str = ""
    email_tools: str = "skill_view"
    # Messages a host works on at once (one conversation still runs in order).
    max_concurrent_turns: int = 4
    # Tools removed everywhere, comma-separated, patterns allowed. The
    # container image sets "terminal": no shell on an unattended host.
    disabled_tools: str = ""
    max_agents: int = DEFAULT_MAX_AGENTS
    agent_idle_seconds: float = DEFAULT_IDLE_SECONDS
    home: Path = field(default_factory=lambda: HOME)

    def __post_init__(self) -> None:
        model, review_model = PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))
        self.model = self.model or model
        self.review_model = self.review_model or review_model

    @property
    def memories_dir(self) -> Path:
        return self.home / "memories"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    @property
    def shell_state_dir(self) -> Path:
        return self.home / "shell"

    @classmethod
    def load(cls) -> "Config":
        values = _read_flat_yaml(HOME / "config.yaml")
        unknown = sorted(set(values) - set(KEYS))
        if unknown:
            raise ValueError(f"Unknown config.yaml keys: {', '.join(unknown)}")
        for key in KEYS:
            if os.environ.get(f"SIMPLE_AGENT_{key.upper()}"):
                values[key] = os.environ[f"SIMPLE_AGENT_{key.upper()}"]
        if "learning" in values:
            values["learning"] = values["learning"].lower() not in {"0", "false", "no", "off"}

        cfg = cls(home=HOME, **values)
        for directory in (cfg.home, cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cfg


def _read_flat_yaml(path: Path) -> dict[str, str]:
    """``key: value`` lines only — enough for this file, and no PyYAML."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader so the repo stays dependency-free."""
    path = path or Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
