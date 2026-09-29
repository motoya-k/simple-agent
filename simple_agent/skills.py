"""Skills: abstract procedures the agent writes for itself.

A skill is *how to do a class of task* — debug a flaky test, ship a release,
answer a customer escalation — written so it would still be correct at
another company.  It is not where team facts go.  "Staging is ``stg-01``",
"Ops owns deploys", "we release on Fridays" are long-term memory (see
:mod:`simple_agent.memory`); the skill says "the staging host", "the deploy
owner", "the team's release day", and the agent recalls the values when it
runs.  Kept apart, the two improve independently: a skill sharpens with every
use, and a fact is corrected once and every skill that needs it picks it up.
:func:`find_specifics` flags the most obvious leaks (URLs, addresses, emails)
when a skill is written.

A skill is a directory under ``~/.simple-agent/skills/<name>/`` containing a
``SKILL.md`` with YAML-ish frontmatter::

    ---
    name: debug-flaky-test
    description: Isolate a test that fails intermittently, then fix or quarantine it.
    status: active
    uses: 4
    updated: 2026-09-10
    ---

    ## When to use
    ...

Only the frontmatter (name + description) is resident in the system prompt; the
body is loaded on demand by ``skill_view``.  That keeps an ever-growing library
from eating the context window — a hundred skills cost a few hundred tokens.

The **curator** ages skills instead of deleting them: ``active`` → ``stale``
(30 days unused) → ``archived`` (90 days).  Archived skills drop out of the
prompt listing but stay on disk, because "the agent decided this was useless"
is a judgment that should be reversible.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

STALE_AFTER = timedelta(days=30)
ARCHIVE_AFTER = timedelta(days=90)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

# Values that belong to one team, not to a procedure. Not exhaustive — names of
# people and systems cannot be caught by a regex — just the ones that can.
_SPECIFIC_RES = (
    re.compile(r"https?://[^\s)>\]]+"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
)


def find_specifics(text: str) -> list[str]:
    """Team-specific values in a skill body, which belong in long-term memory."""
    found: list[str] = []
    for pattern in _SPECIFIC_RES:
        for match in pattern.findall(text):
            if match not in found:
                found.append(match)
    return found


def _abstraction_warning(body: str) -> str:
    specifics = find_specifics(body)
    if not specifics:
        return ""
    return (
        f" Warning: it contains team-specific values ({', '.join(specifics[:5])}). "
        "Skills should be abstract: save these with memory_save and refer to them "
        "by role in the skill (e.g. 'the staging URL')."
    )


class Skill:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.parent.name
        self.meta, self.body = _parse(path.read_text("utf-8"))

    @property
    def description(self) -> str:
        return self.meta.get("description", "")

    @property
    def status(self) -> str:
        return self.meta.get("status", "active")

    @property
    def updated(self) -> date:
        try:
            return datetime.strptime(self.meta.get("updated", ""), "%Y-%m-%d").date()
        except ValueError:
            return date.today()

    def save(self) -> None:
        self.meta["name"] = self.name
        self.meta["updated"] = date.today().isoformat()
        front = "\n".join(f"{k}: {v}" for k, v in self.meta.items())
        self.path.write_text(f"---\n{front}\n---\n\n{self.body.strip()}\n", "utf-8")


class SkillLibrary:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Skill]:
        skills = []
        for path in sorted(self.dir.glob("*/SKILL.md")):
            try:
                skills.append(Skill(path))
            except OSError:
                continue
        return skills

    def get(self, name: str) -> Skill | None:
        path = self.dir / name / "SKILL.md"
        return Skill(path) if path.exists() else None

    def create(self, name: str, description: str, body: str) -> str:
        if not _NAME_RE.match(name):
            return "Invalid name: use lowercase letters, digits and hyphens (2-64 chars)."
        path = self.dir / name / "SKILL.md"
        if path.exists():
            return f"Skill {name!r} already exists — patch it instead of recreating it."
        if not description.strip():
            return "A skill needs a description; it is the only part always in context."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n---\n\n", "utf-8")  # placeholder so Skill() can load
        skill = Skill(path)
        skill.meta = {"name": name, "description": description.strip(), "status": "active", "uses": "0"}
        skill.body = body.strip()
        skill.save()
        return f"Created skill {name!r}." + _abstraction_warning(skill.body)

    def patch(self, name: str, old: str, new: str) -> str:
        skill = self.get(name)
        if skill is None:
            return f"No such skill: {name}"
        if old not in skill.body:
            return "Text to replace not found — view the skill first and match it exactly."
        skill.body = skill.body.replace(old, new, 1)
        skill.meta["status"] = "active"
        skill.save()
        return f"Patched {name!r}." + _abstraction_warning(new)

    def append(self, name: str, text: str) -> str:
        skill = self.get(name)
        if skill is None:
            return f"No such skill: {name}"
        skill.body = f"{skill.body.rstrip()}\n\n{text.strip()}"
        skill.meta["status"] = "active"
        skill.save()
        return f"Appended to {name!r}." + _abstraction_warning(text)

    def mark_used(self, name: str) -> None:
        skill = self.get(name)
        if skill is None:
            return
        skill.meta["uses"] = str(int(skill.meta.get("uses", "0") or 0) + 1)
        skill.meta["status"] = "active"
        skill.save()

    def curate(self) -> list[str]:
        """Age skills by last-touched date. Never deletes."""
        today = date.today()
        changes = []
        for skill in self.all():
            age = today - skill.updated
            target = skill.status
            if age >= ARCHIVE_AFTER:
                target = "archived"
            elif age >= STALE_AFTER and skill.status == "active":
                target = "stale"
            if target != skill.status:
                skill.meta["status"] = target
                # Write directly: save() would refresh `updated` and reset the clock.
                front = "\n".join(f"{k}: {v}" for k, v in skill.meta.items())
                skill.path.write_text(f"---\n{front}\n---\n\n{skill.body.strip()}\n", "utf-8")
                changes.append(f"{skill.name}: {target}")
        return changes

    def catalog(self) -> str:
        """The one-line-per-skill listing that goes into the system prompt."""
        live = [s for s in self.all() if s.status != "archived"]
        if not live:
            return "(no skills yet — write one when you learn something reusable)"
        return "\n".join(f"- {s.name}: {s.description}" for s in live)


def _parse(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---\n")
    front, _, body = rest.partition("---\n")
    meta: dict[str, str] = {}
    for line in front.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, body
