"""Tags: named topic searches that cut across departments.

A tag is a name plus search terms (e.g. "Sanctions": sanction*, asset freeze). Tags work at
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

Searches only keep results from UK outlets (see is_uk_source). Results are
stored as articles of kind "search" with a link to the tag that found them. They appear on tag pages and in channels, never in
department lists (they haven't been classified or vetted).
"""

from __future__ import annotations

import json
import logging
import time
import tomllib
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

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

# UK outlets whose domains don't end in .uk, with display names. Search results
# are kept only if their domain is listed here or ends in .uk (see is_uk_source).
# The list lives in config/uk_outlets.json, shared with the search Worker
# (worker/src/index.js); add domains in tags.toml [search] uk_domains, or there.
UK_OUTLETS: dict[str, str] = json.loads((DEFAULT_CONFIG_DIR / "uk_outlets.json").read_text(encoding="utf-8"))


def _domain(url_or_domain: str) -> str:
    host = urlparse(url_or_domain).netloc if "//" in url_or_domain else url_or_domain
    return host.lower().split(":")[0].removeprefix("www.")


def is_uk_source(url_or_domain: str, extra: tuple[str, ...] = ()) -> bool:
    """True for UK outlets: a .uk domain, or a known UK outlet on another domain."""
    d = _domain(url_or_domain)
    if not d:
        return False
    known = set(UK_OUTLETS) | {_domain(x) for x in extra}
    return d.endswith(".uk") or d == "uk" or any(d == k or d.endswith("." + k) for k in known)


def outlet_name(url_or_domain: str) -> str:
    d = _domain(url_or_domain)
    for k, name in UK_OUTLETS.items():
        if d == k or d.endswith("." + k):
            return name
    return d or "Web"


@dataclass(frozen=True)
class Tag:
    slug: str
    name: str
    terms: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    search: bool = True  # also run a media search (UK outlets only)


@dataclass(frozen=True)
class SearchSettings:
    enabled: bool = True
    country: str = "UK"  # GDELT source-country code (FIPS: UK = United Kingdom)
    providers: tuple[str, ...] = PROVIDERS  # tried in order until one answers
    uk_domains: tuple[str, ...] = ()  # extra UK outlet domains to accept (beyond .uk and UK_OUTLETS)


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
                                       providers=providers, uk_domains=tuple(s.get("uk_domains", [])))


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
        self._known = {t.slug for t in tags}

    def tags_for(self, article: Article) -> list[str]:
        text = f"{article.title}\n{article.summary}"
        found = [slug for slug, terms, exclude in self._tags
                 if any(rx.search(text) for rx in terms) and not any(rx.search(text) for rx in exclude)]
        # Search links are only honoured for tags still in the config.
        return sorted(set(found) | (set(article.tags) & self._known))


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


def parse_gdelt(payload: dict, tag_slug: str, uk_domains: tuple[str, ...] = ()) -> list[Article]:
    """Turn a GDELT artlist response into search Articles tagged with `tag_slug`.
    Results not from a UK outlet are dropped."""
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
        domain = item.get("domain") or url
        if not is_uk_source(domain, uk_domains):
            continue
        articles.append(Article(url=url, title=title, outlet=outlet_name(domain),
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


def parse_google_news(xml_bytes: bytes, tag_slug: str, uk_domains: tuple[str, ...] = ()) -> list[Article]:
    """Google News RSS -> search Articles. Each item names its outlet in
    <source url="...">; results not from a UK outlet are dropped. Titles end
    ' - Outlet', which is removed. Links go via Google's redirect."""
    sources = {}  # link -> outlet homepage URL
    for item in ET.fromstring(xml_bytes).iter("item"):
        src = item.find("source")
        sources[(item.findtext("link") or "").strip()] = src.get("url", "") if src is not None else ""
    articles = []
    for a in parse_feed(xml_bytes, "Google News"):
        source_url = sources.get(a.url, "")
        if not is_uk_source(source_url, uk_domains):
            continue
        title, sep, _ = a.title.rpartition(" - ")
        if sep and title:
            a.title = title.strip()
        a.outlet = outlet_name(source_url)
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
    return parse_gdelt(payload, tag.slug, settings.uk_domains)


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
    return parse_google_news(body, tag.slug, settings.uk_domains)


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
