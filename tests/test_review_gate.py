"""Review gate tests — no network. Jev is replaced by canned answers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simple_agent import review  # noqa: E402
from simple_agent.review_gate import JevReviewGate, last_turn_state  # noqa: E402

TRANSCRIPT = [
    {"role": "user", "content": "old question"},
    {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
    {"role": "user", "content": "もっと短く答えて。ls して"},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "terminal", "input": {"command": "ls"}}],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "a.py"}]},
]


class CannedGate(JevReviewGate):
    def __init__(self, answers=None, error=None, threshold=0.15):
        super().__init__("key", threshold=threshold)
        self.answers, self.error, self.sent = answers, error, None

    def _ask(self, state):
        self.sent = state
        if self.error:
            raise self.error
        return self.answers


def test_only_the_newest_turn_is_sent():
    state = last_turn_state(TRANSCRIPT)

    assert state[0] == {"role": "user", "text": "もっと短く答えて。ls して"}
    assert "old" not in str(state)
    assert '[called terminal {"command": "ls"}]' in state[1]["text"]
    assert state[2]["text"] == "[tool result: a.py]"


def test_passes_below_the_threshold_are_skipped():
    gate = CannedGate({"memory": {"noul": 0.02}, "skills": {"noul": 0.9}})
    assert gate.passes(TRANSCRIPT) == {"skills"}


def test_the_gate_fails_open():
    assert CannedGate(error=OSError("down")).passes(TRANSCRIPT) == {"memory", "skills"}
    assert CannedGate({"memory": {"noul": 0.9}}).passes(TRANSCRIPT) == {"memory", "skills"}


def test_review_runs_only_the_passes_the_gate_keeps(monkeypatch):
    prompts = []
    monkeypatch.setattr(
        review, "run_conversation", lambda **kw: prompts.append(kw["messages"][-1]["content"])
    )
    agent = SimpleNamespace(
        registry=SimpleNamespace(subset=lambda names: None),
        memory=SimpleNamespace(namespace="default"),
        skills=SimpleNamespace(catalog=lambda: ""),
        provider=None,
        config=SimpleNamespace(review_model="m", review_gate_threshold=0.15),
    )

    review._review(agent, TRANSCRIPT, CannedGate({"memory": {"noul": 0.8}, "skills": {"noul": 0.0}}))
    assert prompts == [review.MEMORY_REVIEW_PROMPT]

    prompts.clear()
    review._review(agent, TRANSCRIPT, CannedGate({"memory": {"noul": 0.0}, "skills": {"noul": 0.0}}))
    assert prompts == []
