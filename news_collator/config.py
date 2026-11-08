"""Loading and validating the TOML configuration files.

config/
  outlets.toml       news outlets and their feeds
  departments.toml   departments and the term lists used to classify news
  people.toml        ministers' names per department (optional)
  official.toml      GOV.UK / Parliament mapping per department (optional)
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Default config directory: <repo>/config, alongside the package.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# people.toml goes stale at reshuffles; warn once it hasn't been reviewed for this long.
PEOPLE_REVIEW_DAYS = 180


class UnknownDepartmentError(LookupError):
    """Raised when a department slug isn't in departments.toml."""


@dataclass(frozen=True)
class Outlet:
    name: str
    feeds: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class Department:
    slug: str
    name: str
    # Classification term lists (departments.toml); see classify.py for weights.
    strong: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    people: tuple[str, ...] = ()  # from people.toml; scored like keywords
    # Official-source mapping (official.toml): who this department is on
    # GOV.UK and in Parliament's written statements.
    govuk_orgs: tuple[str, ...] = ()
    parliament_bodies: tuple[str, ...] = ()


@dataclass(frozen=True)
class OfficialSources:
    """Which official sources are switched on (official.toml [sources])."""
    govuk: bool = False
    written_statements: bool = False


@dataclass(frozen=True)
class Config:
    outlets: tuple[Outlet, ...]
    departments: tuple[Department, ...] = field(default_factory=tuple)
    official: OfficialSources = OfficialSources()
    # Headline terms that mark content as never policy news (cartoons, quizzes…).
    global_exclude: tuple[str, ...] = ()

    def department(self, slug: str) -> Department:
        """Look up a department by slug; the error lists valid slugs."""
        for dept in self.departments:
            if dept.slug == slug:
                return dept
        valid = ", ".join(d.slug for d in self.departments)
        raise UnknownDepartmentError(f"Unknown department '{slug}'. Valid options: {valid}")


def _load_optional(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _check_slugs(filename: str, mapping: dict, valid: dict) -> None:
    """Catch typos: every department named in a side file must exist."""
    unknown = set(mapping) - set(valid)
    if unknown:
        raise ValueError(f"{filename} names departments not in departments.toml: {sorted(unknown)}")


def load_config(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> Config:
    """Read and cross-check every config file in `config_dir`."""
    config_dir = Path(config_dir)

    with open(config_dir / "outlets.toml", "rb") as fh:
        raw_outlets = tomllib.load(fh).get("outlet", [])
    with open(config_dir / "departments.toml", "rb") as fh:
        raw = tomllib.load(fh)
    raw_depts = raw.get("departments", {})

    raw_official = _load_optional(config_dir / "official.toml")
    official_depts = raw_official.get("departments", {})
    _check_slugs("official.toml", official_depts, raw_depts)

    raw_people = _load_optional(config_dir / "people.toml")
    people_depts = raw_people.get("departments", {})
    _check_slugs("people.toml", people_depts, raw_depts)
    as_of = raw_people.get("as_of")
    if isinstance(as_of, date) and (date.today() - as_of).days > PEOPLE_REVIEW_DAYS:
        log.warning("people.toml was last reviewed on %s; check it against the current government.", as_of)

    outlets = tuple(
        Outlet(name=o["name"], feeds=tuple(o.get("feeds", [])), enabled=o.get("enabled", True))
        for o in raw_outlets
    )
    departments = tuple(
        Department(
            slug=slug,
            name=d.get("name", slug),
            strong=tuple(d.get("strong", [])),
            keywords=tuple(d.get("keywords", [])),
            context=tuple(d.get("context", [])),
            exclude=tuple(d.get("exclude", [])),
            people=tuple(people_depts.get(slug, {}).get("people", [])),
            govuk_orgs=tuple(official_depts.get(slug, {}).get("govuk_orgs", [])),
            parliament_bodies=tuple(official_depts.get(slug, {}).get("parliament_bodies", [])),
        )
        for slug, d in raw_depts.items()
    )
    sources = raw_official.get("sources", {})
    official = OfficialSources(govuk=sources.get("govuk", False),
                               written_statements=sources.get("written_statements", False))

    if not outlets:
        raise ValueError(f"No outlets defined in {config_dir / 'outlets.toml'}")
    if not departments:
        raise ValueError(f"No departments defined in {config_dir / 'departments.toml'}")
    return Config(outlets=outlets, departments=departments, official=official,
                  global_exclude=tuple(raw.get("global", {}).get("exclude", [])))
