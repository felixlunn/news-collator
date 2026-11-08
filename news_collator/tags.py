"""Tags: named topic searches that cut across departments.

A tag is a name plus search terms ("Iran": Iran, Tehran, IRGC). Tags work at
two levels:

- Team tags (config/tags.toml) are built into the website. Each one
    1. matches articles already collected from the outlet feeds, using the
       same term syntax as departments.toml, and
    2. optionally runs a media search against GDELT, an open index of world
       news, restricted to UK outlets, so it finds coverage beyond our feeds.
- Personal tags are created by readers on the website's Channels page and
  matched in their browser against everything the site has collected
  (articles.json). They use the same term syntax; see web.py.

Search results are stored as articles of kind "search" with a link to the tag
that found them. They appear on tag pages and in channels, never in
department lists (they haven't been classified or vetted).
"""

from __future__ import annotations

import json
import logging
import time
import tomllib
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .classify import compile_term
from .config import DEFAULT_CONFIG_DIR
from .fetch import download
from .models import SEARCH, Article

log = logging.getLogger(__name__)

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MAX_RECORDS = 75
GDELT_PAUSE_SECONDS = 5.5  # GDELT allows one request every 5 seconds

# Display names for common GDELT domains; anything else shows its domain.
OUTLET_NAMES = {
    "bbc.co.uk": "BBC News", "bbc.com": "BBC News", "theguardian.com": "The Guardian",
    "telegraph.co.uk": "The Telegraph", "independent.co.uk": "The Independent",
    "news.sky.com": "Sky News", "ft.com": "Financial Times", "thetimes.co.uk": "The Times",
    "thetimes.com": "The Times", "dailymail.co.uk": "Daily Mail", "mirror.co.uk": "Daily Mirror",
    "express.co.uk": "Daily Express", "thesun.co.uk": "The Sun", "inews.co.uk": "The i",
    "politicshome.com": "PoliticsHome", "standard.co.uk": "Evening Standard",
    "newstatesman.com": "New Statesman", "spectator.co.uk": "The Spectator",
    "itv.com": "ITV News", "channel4.com": "Channel 4 News", "economist.com": "The Economist",
    "reuters.com": "Reuters", "lbc.co.uk": "LBC", "gbnews.com": "GB News",
}


@dataclass(frozen=True)
class Tag:
    slug: str
    name: str
    terms: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    search: bool = True  # also run a GDELT media search


@dataclass(frozen=True)
class SearchSettings:
    enabled: bool = True
    country: str = "UK"  # GDELT source-country code (FIPS: UK = United Kingdom)


def load_tags(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> tuple[tuple[Tag, ...], SearchSettings]:
    """Read config/tags.toml; no file means no team tags."""
    path = Path(config_dir) / "tags.toml"
    if not path.exists():
        return (), SearchSettings()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    tags = []
    for slug, t in raw.get("tags", {}).items():
        terms = tuple(t.get("terms", []))
        if not terms:
            raise ValueError(f"tags.toml: tag '{slug}' has no terms")
        tags.append(Tag(slug=slug, name=t.get("name", slug), terms=terms,
                        exclude=tuple(t.get("exclude", [])), search=t.get("search", True)))
    s = raw.get("search", {})
    return tuple(tags), SearchSettings(enabled=s.get("enabled", True), country=s.get("country", "UK"))


# ---------------------------------------------------------------------------
# Matching collected articles
# ---------------------------------------------------------------------------

class TagMatcher:
    """Decides which team tags an article belongs to.

    An article belongs to a tag if any term appears in its headline or summary
    (and no exclude term does), or if the tag's media search found it.
    """

    def __init__(self, tags: tuple[Tag, ...]):
        self._tags = [(t.slug, [compile_term(x) for x in t.terms], [compile_term(x) for x in t.exclude])
                      for t in tags]

    def tags_for(self, article: Article) -> list[str]:
        text = f"{article.title}\n{article.summary}"
        found = [slug for slug, terms, exclude in self._tags
                 if any(rx.search(text) for rx in terms) and not any(rx.search(text) for rx in exclude)]
        return sorted(set(found) | set(article.tags))


# ---------------------------------------------------------------------------
# GDELT media search
# ---------------------------------------------------------------------------

def gdelt_query(tag: Tag, country: str) -> str:
    """Build a GDELT DOC query: (term OR "multi word term") sourcecountry:UK sourcelang:english.

    Wildcards and case markers from the term syntax are dropped, since GDELT
    matches words case-insensitively. Exclusions become -terms.
    """
    def clean(term: str) -> str:
        term = term.lstrip("=").rstrip("*").strip()
        return f'"{term}"' if " " in term or "-" in term else term

    terms = [clean(t) for t in tag.terms if len(t.lstrip("=").rstrip("*")) >= 3]
    if not terms:
        raise ValueError(f"tag '{tag.slug}': GDELT needs terms of 3 or more characters")
    joined = terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"
    negated = " ".join(f"-{clean(t)}" for t in tag.exclude)
    return " ".join(part for part in (joined, negated, f"sourcecountry:{country}", "sourcelang:english")
                    if part)


def parse_gdelt(payload: dict, tag_slug: str) -> list[Article]:
    """Turn a GDELT artlist response into search Articles tagged with `tag_slug`."""
    articles = []
    for item in payload.get("articles", []) or []:
        url, title = item.get("url"), (item.get("title") or "").strip()
        if not url or not title:
            continue
        published = None
        seen = item.get("seendate")  # e.g. "20261108T143000Z"
        if seen:
            try:
                published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        domain = (item.get("domain") or "").lower().removeprefix("www.")
        articles.append(Article(url=url, title=title, outlet=OUTLET_NAMES.get(domain, domain or "Web"),
                                published=published, kind=SEARCH, tags=[tag_slug]))
    return articles


def search_tags(tags: tuple[Tag, ...], settings: SearchSettings, hours: int
                ) -> tuple[list[Article], dict[str, str]]:
    """Run each searchable tag through GDELT, one request at a time."""
    if not settings.enabled:
        return [], {}
    articles: list[Article] = []
    errors: dict[str, str] = {}
    searchable = [t for t in tags if t.search]
    for i, tag in enumerate(searchable):
        if i:
            time.sleep(GDELT_PAUSE_SECONDS)
        params = {"query": gdelt_query(tag, settings.country), "mode": "artlist", "format": "json",
                  "maxrecords": GDELT_MAX_RECORDS, "timespan": f"{hours}h", "sort": "datedesc"}
        url = f"{GDELT_API}?{urlencode(params)}"
        try:
            body = download(url, accept="application/json")
            payload = json.loads(body) if body.strip() else {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("GDELT search for tag '%s' failed: %s", tag.slug, exc)
            errors[url] = str(exc)
            continue
        found = parse_gdelt(payload, tag.slug)
        log.info("GDELT tag %s: %d articles", tag.slug, len(found))
        articles += found
    return articles, errors
