"""Loading and validating the TOML configuration files."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Default config directory: <repo>/config, alongside the package.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


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
    strong: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # Official-source mapping (from official.toml): who this department is on
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

    def department(self, slug: str) -> Department:
        """Look up a department by slug; the error lists valid slugs."""
        for dept in self.departments:
            if dept.slug == slug:
                return dept
        valid = ", ".join(d.slug for d in self.departments)
        raise UnknownDepartmentError(f"Unknown department '{slug}'. Valid options: {valid}")


def load_config(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> Config:
    """Read outlets.toml, departments.toml and (optional) official.toml from `config_dir`."""
    config_dir = Path(config_dir)

    with open(config_dir / "outlets.toml", "rb") as fh:
        raw_outlets = tomllib.load(fh).get("outlet", [])
    with open(config_dir / "departments.toml", "rb") as fh:
        raw_depts = tomllib.load(fh).get("departments", {})

    raw_official: dict = {}
    if (config_dir / "official.toml").exists():
        with open(config_dir / "official.toml", "rb") as fh:
            raw_official = tomllib.load(fh)
    official_depts = raw_official.get("departments", {})
    unknown = set(official_depts) - set(raw_depts)
    if unknown:
        raise ValueError(f"official.toml names departments not in departments.toml: {sorted(unknown)}")

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
            exclude=tuple(d.get("exclude", [])),
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
    return Config(outlets=outlets, departments=departments, official=official)
