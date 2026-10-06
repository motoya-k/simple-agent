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
    "model",
    "review_model",
    "learning",
    "database_url",
    "memory_namespace",
    "profile",
    "mods",
    "max_concurrent_turns",
    "disabled_tools",
)


@dataclass
class Config:
    provider: str = "anthropic"
    model: str = ""  # empty = the provider's default
    review_model: str = ""
    learning: bool = True  # background memory/skill review after each turn
    # Jev yes/no probability below which a review pass is skipped. Only used
    # when TYPESAFE_API_KEY is set. See review_gate.py.
    review_gate_threshold: float = 0.15
    # Everything this agent writes — transcripts, long-term memory, skills —
    # goes to one place. Empty keeps it in files under `home`: SQLite for
    # transcripts, JSONL for memory, directories for skills, all editable by
    # hand. A postgresql:// URL moves all three to Postgres, for a host whose
    # disk does not outlive it. See state_postgres.py, memory.py, skills.py.
    database_url: str = ""
    # The team or org long-term memory belongs to. A profile may name its own.
    memory_namespace: str = "default"
    # Who the agent is: its instructions, toolset, whether it may learn, and
    # its memory namespace. A name in `profiles_dir`, or one of the built-ins
    # (`terminal`, `email`). See profile.py.
    profile: str = ""
    # Mods that run on every route, comma-separated, whatever a profile adds.
    # A mod stands between the model and a tool and may refuse the call; see
    # mods.py. Named here, it cannot be dropped by a profile.
    mods: str = ""
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
    def profiles_dir(self) -> Path:
        return self.home / "profiles"

    @property
    def mods_dir(self) -> Path:
        return self.home / "mods"

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
        for directory in (
            cfg.home,
            cfg.memories_dir,
            cfg.skills_dir,
            cfg.profiles_dir,
            cfg.mods_dir,
            cfg.shell_state_dir,
        ):
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
