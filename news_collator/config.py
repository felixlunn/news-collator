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


@dataclass(frozen=True)
class Config:
    outlets: tuple[Outlet, ...]
    departments: tuple[Department, ...] = field(default_factory=tuple)

    def department(self, slug: str) -> Department:
        """Look up a department by slug; the error lists valid slugs."""
        for dept in self.departments:
            if dept.slug == slug:
                return dept
        valid = ", ".join(d.slug for d in self.departments)
        raise UnknownDepartmentError(f"Unknown department '{slug}'. Valid options: {valid}")


def load_config(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> Config:
    """Read outlets.toml and departments.toml from `config_dir`."""
    config_dir = Path(config_dir)

    with open(config_dir / "outlets.toml", "rb") as fh:
        raw_outlets = tomllib.load(fh).get("outlet", [])
    with open(config_dir / "departments.toml", "rb") as fh:
        raw_depts = tomllib.load(fh).get("departments", {})

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
        )
        for slug, d in raw_depts.items()
    )

    if not outlets:
        raise ValueError(f"No outlets defined in {config_dir / 'outlets.toml'}")
    if not departments:
        raise ValueError(f"No departments defined in {config_dir / 'departments.toml'}")
    return Config(outlets=outlets, departments=departments)
