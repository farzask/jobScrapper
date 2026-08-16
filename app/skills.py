"""User skill profile: storage, matching, and scoring.

Skills live in data/skills.yaml rather than config.yaml so the dashboard can
rewrite them freely without destroying config.yaml's comments.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from app.config import ROOT

SKILLS_FILE = ROOT / "data" / "skills.yaml"

# Seeded from the roles you're targeting; edit freely in the dashboard.
DEFAULT_SKILLS = [
    ("flutter", True), ("dart", True), ("firebase", True),
    ("android", True), ("ios", False), ("kotlin", False), ("swift", False),
    ("react native", True), ("mobile", False),
    ("javascript", False), ("typescript", False), ("react", False),
    ("node", False), ("python", False), ("git", False), ("rest api", False),
    ("sql", False), ("agile", False), ("scrum", False), ("jira", False),
]


@dataclass
class Skill:
    name: str
    core: bool = False          # core skills weigh more and can be required

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkillProfile:
    skills: list[Skill]
    # Jobs matching fewer than this many of your skills are filtered out.
    # 0 disables the filter entirely.
    min_matches: int = 0
    # When true, a job must mention at least one skill marked "core".
    require_core: bool = False

    def names(self) -> list[str]:
        return [s.name for s in self.skills]


def _default_profile() -> SkillProfile:
    return SkillProfile(skills=[Skill(n, c) for n, c in DEFAULT_SKILLS])


def load_skills() -> SkillProfile:
    if not SKILLS_FILE.exists():
        prof = _default_profile()
        save_skills(prof)
        return prof
    try:
        data = yaml.safe_load(SKILLS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return _default_profile()

    raw = data.get("skills") or []
    skills: list[Skill] = []
    for item in raw:
        if isinstance(item, str):
            skills.append(Skill(item.strip().lower()))
        elif isinstance(item, dict) and item.get("name"):
            skills.append(Skill(str(item["name"]).strip().lower(),
                                bool(item.get("core", False))))
    if not skills:
        skills = [Skill(n, c) for n, c in DEFAULT_SKILLS]

    return SkillProfile(
        skills=skills,
        min_matches=int(data.get("min_matches", 0) or 0),
        require_core=bool(data.get("require_core", False)),
    )


def save_skills(profile: SkillProfile) -> None:
    SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "min_matches": profile.min_matches,
        "require_core": profile.require_core,
        "skills": [s.to_dict() for s in profile.skills],
    }
    SKILLS_FILE.write_text(
        "# Your skills. Edit here or in the dashboard at /skills.\n"
        "#   core: true      -> weighs more, and can be required\n"
        "#   min_matches     -> hide jobs matching fewer skills than this\n"
        "#   require_core    -> hide jobs that mention none of your core skills\n\n"
        + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8")


def _pattern(name: str) -> re.Pattern:
    """Word-boundary match that tolerates '.', '-' and spaces between words.

    Plain \\b fails on skills like 'node.js' and 'c++', and a naive substring
    match makes 'r' or 'go' hit almost every posting.
    """
    parts = [re.escape(p) for p in re.split(r"[\s._-]+", name.strip()) if p]
    if not parts:
        return re.compile(r"(?!)")
    body = r"[\s._-]*".join(parts)
    return re.compile(rf"(?<![a-z0-9+#]){body}(?![a-z0-9+#])", re.I)


_CACHE: dict[str, re.Pattern] = {}


def match_skills(text: str, profile: SkillProfile) -> list[str]:
    """Which of the user's skills this text mentions."""
    if not text:
        return []
    blob = text[:12000]
    out = []
    for s in profile.skills:
        pat = _CACHE.get(s.name)
        if pat is None:
            pat = _CACHE[s.name] = _pattern(s.name)
        if pat.search(blob):
            out.append(s.name)
    return out


def skill_score(matched: list[str], profile: SkillProfile) -> int:
    """0-30 points, weighting core skills roughly double."""
    if not matched:
        return 0
    core = {s.name for s in profile.skills if s.core}
    pts = sum(7 if m in core else 4 for m in matched)
    return min(pts, 30)


def passes_skill_filter(matched: list[str], profile: SkillProfile) -> tuple[bool, str]:
    if profile.min_matches and len(matched) < profile.min_matches:
        return False, (f"matches {len(matched)} skills, "
                       f"need {profile.min_matches}")
    if profile.require_core:
        core = {s.name for s in profile.skills if s.core}
        if core and not (set(matched) & core):
            return False, "mentions none of your core skills"
    return True, ""
