"""Skills: procedures the agent writes for itself.

A skill is a directory under ``~/.simple-agent/skills/<name>/`` containing a
``SKILL.md`` with YAML-ish frontmatter::

    ---
    name: deploy-staging
    description: How to ship this repo to staging, including the gotchas.
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
        return f"Created skill {name!r}."

    def patch(self, name: str, old: str, new: str) -> str:
        skill = self.get(name)
        if skill is None:
            return f"No such skill: {name}"
        if old not in skill.body:
            return "Text to replace not found — view the skill first and match it exactly."
        skill.body = skill.body.replace(old, new, 1)
        skill.meta["status"] = "active"
        skill.save()
        return f"Patched {name!r}."

    def append(self, name: str, text: str) -> str:
        skill = self.get(name)
        if skill is None:
            return f"No such skill: {name}"
        skill.body = f"{skill.body.rstrip()}\n\n{text.strip()}"
        skill.meta["status"] = "active"
        skill.save()
        return f"Appended to {name!r}."

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
