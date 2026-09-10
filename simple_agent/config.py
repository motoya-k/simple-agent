"""Configuration — env first, optional JSON file second, defaults last."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("SIMPLE_AGENT_HOME", Path.home() / ".simple-agent"))

# The agent's own model, and a cheaper one for the background reviewer that runs
# after every turn. Splitting them is what makes always-on learning affordable.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_REVIEW_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Config:
    provider: str = "anthropic"
    model: str = DEFAULT_MODEL
    review_model: str = DEFAULT_REVIEW_MODEL
    max_tokens: int = 8192

    # Runaway guards on the loop. See loop.py.
    max_iterations: int = 90
    token_budget: int = 2_000_000
    max_parallel_tools: int = 8
    command_timeout: int = 120

    learning: bool = True  # background memory/skill review after each turn

    home: Path = field(default_factory=lambda: HOME)

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
        cfg = cls()

        path = cfg.home / "config.json"
        if path.exists():
            for key, value in json.loads(path.read_text("utf-8")).items():
                if hasattr(cfg, key) and key != "home":
                    setattr(cfg, key, value)

        env_map = {
            "SIMPLE_AGENT_PROVIDER": "provider",
            "SIMPLE_AGENT_MODEL": "model",
            "SIMPLE_AGENT_REVIEW_MODEL": "review_model",
        }
        for env_key, attr in env_map.items():
            if os.environ.get(env_key):
                setattr(cfg, attr, os.environ[env_key])
        if os.environ.get("SIMPLE_AGENT_LEARNING") in {"0", "false", "no"}:
            cfg.learning = False

        for directory in (cfg.home, cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cfg


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
