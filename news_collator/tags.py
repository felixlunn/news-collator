"""Tags: named topic searches that cut across departments.

A tag is a name plus search terms ("Iran": Iran, Tehran, IRGC). Tags work at
two levels:

- Team tags (config/tags.toml) are built into the website. Each one
    1. matches articles already collected from the outlet feeds, using the
       same term syntax as departments.toml, and
    2. optionally runs a media search restricted to UK outlets, so it finds
       coverage beyond our feeds. Providers are tried in the order set in
       tags.toml: by default Google News search (UK edition), then GDELT
       (an open index of world news) if Google News doesn't answer.
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
from .fetch import download, parse_feed
from .models import SEARCH, Article

log = logging.getLogger(__name__)

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MAX_RECORDS = 75
GDELT_PAUSE_SECONDS = 5.5          # GDELT allows one request every 5 seconds
GDELT_RETRY_WAITS = (15, 30)       # back-off after "429 Too Many Requests"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
GOOGLE_RETRY_WAIT = 20             # one retry after "503 Service Unavailable" / 429
PROVIDERS = ("google_news", "gdelt")

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
    providers: tuple[str, ...] = PROVIDERS  # tried in order until one answers


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
    providers = tuple(s.get("providers", PROVIDERS))
    unknown = set(providers) - set(PROVIDERS)
    if unknown:
        raise ValueError(f"tags.toml: unknown search providers {sorted(unknown)}; choose from {PROVIDERS}")
    return tuple(tags), SearchSettings(enabled=s.get("enabled", True), country=s.get("country", "UK"),
                                       providers=providers)


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
    """GDELT DOC syntax: (term OR "multi word term") -exclusion sourcecountry:UK sourcelang:english"""
    terms, exclude = _search_terms(tag, quote_hyphens=True)
    if not terms:
        raise ValueError(f"tag '{tag.slug}': searches need terms of 3 or more characters")
    joined = terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"
    negated = " ".join(f"-{t}" for t in exclude)
    return " ".join(part for part in (joined, negated, f"sourcecountry:{country}", "sourcelang:english") if part)


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


def _search_terms(tag: Tag, quote_hyphens: bool) -> tuple[list[str], list[str]]:
    """Terms and exclusions for a search engine: no wildcards or case markers,
    multi-word terms quoted, terms under 3 characters dropped."""
    def clean(term: str) -> str:
        term = term.lstrip("=").rstrip("*").strip()
        return f'"{term}"' if " " in term or (quote_hyphens and "-" in term) else term
    terms = [clean(t) for t in tag.terms if len(t.lstrip("=").rstrip("*")) >= 3]
    return terms, [clean(t) for t in tag.exclude]


def google_news_query(tag: Tag, hours: int) -> str:
    """Google News search syntax: (a OR "b c") -x when:3d"""
    terms, exclude = _search_terms(tag, quote_hyphens=False)
    joined = terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"
    days = max(1, round(hours / 24))
    return " ".join([joined] + [f"-{t}" for t in exclude] + [f"when:{days}d"])


def parse_google_news(xml_bytes: bytes, tag_slug: str) -> list[Article]:
    """Google News RSS: titles end ' - Outlet'; links go via Google's redirect."""
    articles = []
    for a in parse_feed(xml_bytes, "Google News"):
        title, sep, outlet = a.title.rpartition(" - ")
        if sep and title:
            a.title, a.outlet = title.strip(), outlet.strip()
        a.summary = ""  # Google's summaries just repeat the headline and outlet
        a.kind, a.tags = SEARCH, [tag_slug]
        articles.append(a)
    return articles


def _gdelt(tag: Tag, settings: SearchSettings, hours: int) -> list[Article]:
    """One GDELT search, retrying with back-off if rate-limited. Raises on failure."""
    params = {"query": gdelt_query(tag, settings.country), "mode": "artlist", "format": "json",
              "maxrecords": GDELT_MAX_RECORDS, "timespan": f"{hours}h", "sort": "datedesc"}
    url = f"{GDELT_API}?{urlencode(params)}"
    for wait in (*GDELT_RETRY_WAITS, None):
        try:
            body = download(url, accept="application/json")
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or wait is None:
                raise
            log.info("GDELT rate-limited for tag '%s'; retrying in %ss", tag.slug, wait)
            time.sleep(wait)
    try:
        payload = json.loads(body) if body.strip() else {}
    except ValueError:
        # GDELT explains query problems in plain text rather than JSON.
        raise ValueError(f"GDELT replied: {body[:200].decode('utf-8', 'replace').strip()}") from None
    return parse_gdelt(payload, tag.slug)


def _google_news(tag: Tag, settings: SearchSettings, hours: int) -> list[Article]:
    """One Google News search (UK English edition). Raises on failure."""
    params = {"q": google_news_query(tag, hours), "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    url = f"{GOOGLE_NEWS_RSS}?{urlencode(params)}"
    try:
        body = download(url)
    except urllib.error.HTTPError as exc:
        if exc.code not in (429, 503):
            raise
        log.info("Google News busy for tag '%s' (%s); retrying in %ss", tag.slug, exc.code, GOOGLE_RETRY_WAIT)
        time.sleep(GOOGLE_RETRY_WAIT)
        body = download(url)
    return parse_google_news(body, tag.slug)


_PROVIDER_FUNCS = {"gdelt": _gdelt, "google_news": _google_news}


def search_tags(tags: tuple[Tag, ...], settings: SearchSettings, hours: int
                ) -> tuple[list[Article], dict[str, str]]:
    """Run each searchable tag through the configured providers, in order,
    stopping at the first that answers. Returns (articles, errors)."""
    if not settings.enabled:
        return [], {}
    articles: list[Article] = []
    errors: dict[str, str] = {}
    for i, tag in enumerate(t for t in tags if t.search):
        if i:
            time.sleep(GDELT_PAUSE_SECONDS)
        problems = []
        for provider in settings.providers:
            try:
                found = _PROVIDER_FUNCS[provider](tag, settings, hours)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                problems.append(f"{provider}: {exc}")
                log.warning("%s search for tag '%s' failed: %s", provider, tag.slug, exc)
                continue
            log.info("%s tag %s: %d articles", provider, tag.slug, len(found))
            articles += found
            break
        else:
            errors[f"tag:{tag.slug}"] = "; ".join(problems) or "no providers configured"
    return articles, errors
