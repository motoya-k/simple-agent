"""simple-agent — a small, self-improving agent core."""

from .agent import Agent
from .compaction import Compactor, TailCompactor
from .config import Config
from .context import current_session_key, session_scope
from .memory import LocalMemory, LongTermMemory, open_memory
from .profile import Profile, load_profile
from .registry import AgentRegistry
from .session import SessionSource, build_session_key

__all__ = [
    "Agent",
    "AgentRegistry",
    "Compactor",
    "Config",
    "LocalMemory",
    "LongTermMemory",
    "Profile",
    "SessionSource",
    "TailCompactor",
    "build_session_key",
    "current_session_key",
    "load_profile",
    "open_memory",
    "session_scope",
]
__version__ = "0.2.0"
