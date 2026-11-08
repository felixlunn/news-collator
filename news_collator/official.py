"""Official sources: GOV.UK announcements and written ministerial statements.

These are filed by the department that *issued* them (not by keyword
matching), using the mapping in config/official.toml, and are pinned to the
top of that department's list in the digest.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from .config import Config, Department
from .fetch import MAX_WORKERS, download, parse_date, parse_feed, strip_html
from .models import GOVUK, WRITTEN_STATEMENT, Article

log = logging.getLogger(__name__)

# Score recorded for an official item's issuing department. Purely nominal:
# official items are pinned regardless of score, but a high value keeps the
# stored data self-explanatory ("definitely this department").
OFFICIAL_SCORE = 100

GOVUK_FEED = "https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D={org}"

STATEMENTS_API = "https://questions-statements-api.parliament.uk/api/writtenstatements/statements"
STATEMENT_PAGE = "https://questions-statements.parliament.uk/written-statements/detail/{date}/{uin}"
STATEMENTS_PAGE_SIZE = 100
STATEMENTS_MAX_PAGES = 5  # safety cap: 500 statements is far more than a week's worth


# ---------------------------------------------------------------------------
# GOV.UK
# ---------------------------------------------------------------------------

def _fetch_govuk_org(org: str, dept_slug: str) -> tuple[str, list[Article], str | None]:
    """Fetch one organisation's GOV.UK news-and-communications Atom feed."""
    url = GOVUK_FEED.format(org=quote(org))
    try:
        items = parse_feed(download(url), "GOV.UK")
    except Exception as exc:  # network or XML error; reported, never fatal
        return url, [], str(exc)
    for art in items:
        art.kind = GOVUK
        art.departments = {dept_slug: OFFICIAL_SCORE}
    log.info("GOV.UK %s: %d items", org, len(items))
    return url, items, None


def fetch_govuk(departments: tuple[Department, ...]) -> tuple[list[Article], dict[str, str]]:
    """Fetch GOV.UK announcements for every mapped organisation, concurrently.

    An announcement published jointly by two departments appears in both
    organisations' feeds; it is kept once and tagged with both departments.
    """
    jobs = [(org, d.slug) for d in departments for org in d.govuk_orgs]
    merged: dict[str, Article] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for url, items, error in pool.map(lambda job: _fetch_govuk_org(*job), jobs):
            if error:
                log.warning("%s: %s", url, error)
                errors[url] = error
            for art in items:
                if art.id in merged:
                    merged[art.id].departments.update(art.departments)
                else:
                    merged[art.id] = art
    return list(merged.values()), errors


# ---------------------------------------------------------------------------
# Written ministerial statements (UK Parliament Written Questions & Statements API)
# ---------------------------------------------------------------------------

def _normalise_body(name: str) -> str:
    """Canonical form of an answering-body name for matching:
    lower case, '&' -> 'and', punctuation and a leading 'the' removed."""
    name = name.lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.removeprefix("the ")


def _body_lookup(departments: tuple[Department, ...]) -> dict[str, str]:
    """Map normalised answering-body name -> department slug."""
    return {_normalise_body(body): d.slug for d in departments for body in d.parliament_bodies}


def parse_statements(payload: dict, lookup: dict[str, str]) -> tuple[list[Article], set[str]]:
    """Turn one API response page into Articles.

    Returns (articles, unmatched_body_names). Statements from bodies with no
    department mapping (e.g. the Leader of the House) are skipped but reported,
    so gaps in official.toml are visible in the logs.
    """
    articles: list[Article] = []
    unmatched: set[str] = set()
    for item in payload.get("results", []):
        st = item.get("value", item)  # API wraps each result as {"value": {...}, "links": [...]}
        body = st.get("answeringBodyName") or ""
        slug = lookup.get(_normalise_body(body))
        if not slug:
            unmatched.add(body or "(none)")
            continue
        made = parse_date(st.get("dateMade"))
        uin = st.get("uin")
        title = (st.get("title") or "").strip()
        if not (made and uin and title):
            continue
        house = st.get("house") or ""
        articles.append(Article(
            url=STATEMENT_PAGE.format(date=made.date().isoformat(), uin=quote(str(uin))),
            title=title,
            outlet=f"Parliament ({house})" if house else "Parliament",
            summary=strip_html(st.get("text") or ""),
            published=made,
            kind=WRITTEN_STATEMENT,
            departments={slug: OFFICIAL_SCORE},
        ))
    return articles, unmatched


def _dedupe_bicameral(articles: list[Article]) -> list[Article]:
    """Most statements are made in both Houses, under different UINs (HCWS…/HLWS…).
    Keep one copy, preferring the Commons version."""
    articles = sorted(articles, key=lambda a: a.outlet != "Parliament (Commons)")
    seen: set[tuple] = set()
    kept = []
    for a in articles:
        key = (a.published.date() if a.published else None, a.title.lower(), tuple(a.departments))
        if key not in seen:
            seen.add(key)
            kept.append(a)
    return kept


def fetch_written_statements(departments: tuple[Department, ...], hours: int
                             ) -> tuple[list[Article], dict[str, str]]:
    """Fetch written statements made in the last `hours`, paging through results."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).date().isoformat()
    lookup = _body_lookup(departments)
    articles: list[Article] = []
    unmatched: set[str] = set()

    for page in range(STATEMENTS_MAX_PAGES):
        url = f"{STATEMENTS_API}?" + urlencode({"madeWhenFrom": since, "take": STATEMENTS_PAGE_SIZE,
                                                "skip": page * STATEMENTS_PAGE_SIZE})
        try:
            payload = json.loads(download(url, accept="application/json"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("%s: %s", url, exc)
            return _dedupe_bicameral(articles), {url: str(exc)}
        if page == 0:
            # Diagnostic: confirms the API shape and how many statements exist,
            # so "0 statements" can be told apart from a parsing problem.
            keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            log.info("Written statements API: totalResults=%s, keys=%s",
                     payload.get("totalResults") if isinstance(payload, dict) else "?", keys)
            if isinstance(payload, dict) and payload.get("results"):
                first = payload["results"][0]
                log.info("Written statements API: first result keys=%s",
                         sorted(first.get("value", first)) if isinstance(first, dict) else first)
        batch, missing = parse_statements(payload, lookup)
        articles += batch
        unmatched |= missing
        if len(payload.get("results", [])) < STATEMENTS_PAGE_SIZE:
            break

    if unmatched:
        log.info("Written statements from unmapped bodies (skipped): %s", ", ".join(sorted(unmatched)))
    result = _dedupe_bicameral(articles)
    log.info("Written statements: %d since %s", len(result), since)
    return result, {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def fetch_official(config: Config, hours: int) -> tuple[list[Article], dict[str, str]]:
    """Fetch every enabled official source. Returns (articles, errors)."""
    articles: list[Article] = []
    errors: dict[str, str] = {}
    if config.official.govuk:
        items, errs = fetch_govuk(config.departments)
        articles += items
        errors |= errs
    if config.official.written_statements:
        items, errs = fetch_written_statements(config.departments, hours)
        articles += items
        errors |= errs
    return articles, errors
