"""Downloading and parsing RSS 2.0 / Atom feeds using only the standard library."""

from __future__ import annotations

import gzip
import html
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .config import Outlet
from .models import Article

log = logging.getLogger(__name__)

USER_AGENT = "PolicyNewsCollator/0.1"
TIMEOUT_SECONDS = 15
MAX_WORKERS = 8
STALE_AFTER_DAYS = 7

_ATOM = "{http://www.w3.org/2005/Atom}"
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects visible text from an HTML fragment (feed summaries often contain markup)."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(fragment: str) -> str:
    """Convert an HTML snippet to plain, whitespace-collapsed text."""
    if not fragment:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(fragment))
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def parse_date(value: str | None) -> datetime | None:
    """Parse RFC 822 (RSS) or ISO 8601 (Atom) dates into aware UTC datetimes."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)  # RSS style: "Sun, 08 Nov 2026 17:00:00 GMT"
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom style
        except ValueError:
            log.debug("Unparseable date: %r", value)
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_feed(xml_bytes: bytes, outlet: str) -> list[Article]:
    """Parse a feed document (RSS 2.0 or Atom) into Articles.

    Malformed items are skipped rather than failing the whole feed.
    """
    root = ET.fromstring(xml_bytes)
    articles: list[Article] = []

    if root.tag == f"{_ATOM}feed":
        for entry in root.findall(f"{_ATOM}entry"):
            # Prefer the rel="alternate" link; fall back to the first link.
            links = entry.findall(f"{_ATOM}link")
            href = next((l.get("href") for l in links if l.get("rel", "alternate") == "alternate"), None)
            href = href or (links[0].get("href") if links else None)
            title = strip_html(_text(entry.find(f"{_ATOM}title")))
            if not href or not title:
                continue
            summary = _text(entry.find(f"{_ATOM}summary")) or _text(entry.find(f"{_ATOM}content"))
            published = _text(entry.find(f"{_ATOM}published")) or _text(entry.find(f"{_ATOM}updated"))
            articles.append(Article(url=href, title=title, outlet=outlet,
                                    summary=strip_html(summary), published=parse_date(published)))
    else:
        # RSS 2.0: <rss><channel><item>…  (also tolerates RSS 1.0-ish docs with bare <item>s)
        for item in root.iter("item"):
            link = _text(item.find("link")) or _text(item.find("guid"))
            title = strip_html(_text(item.find("title")))
            if not link.startswith("http") or not title:
                continue
            articles.append(Article(
                url=link,
                title=title,
                outlet=outlet,
                summary=strip_html(_text(item.find("description"))),
                published=parse_date(_text(item.find("pubDate"))),
            ))
    return articles


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def decompress(body: bytes) -> bytes:
    """Undo gzip or deflate compression if present.

    urllib doesn't decode compressed responses, and some outlets (e.g. the
    Independent) compress even when the client never asked for it, so we
    detect compression from the bytes themselves rather than trusting headers.
    """
    if body[:2] == b"\x1f\x8b":  # gzip magic number
        return gzip.decompress(body)
    if body[:1] == b"\x78":  # zlib/deflate header byte
        try:
            return zlib.decompress(body)
        except zlib.error:
            pass  # just happened to start with 'x'; leave untouched
    return body


FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml"


def download(url: str, accept: str = FEED_ACCEPT) -> bytes:
    """GET a URL and return the (decompressed) body. Raises urllib errors on failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return decompress(resp.read())


def stale_warning(articles: list[Article], now: datetime | None = None) -> str | None:
    """A message if a feed's newest dated item is older than STALE_AFTER_DAYS.

    Feeds sometimes keep responding after an outlet stops updating them (the
    Telegraph's old section feeds did this), which otherwise goes unnoticed.
    """
    dates = [a.published for a in articles if a.published]
    if not dates:
        return None
    newest = max(dates)
    now = now or datetime.now(timezone.utc)
    if now - newest > timedelta(days=STALE_AFTER_DAYS):
        return f"stale feed: newest item is from {newest:%d %b %Y}"
    return None


def fetch_feed(url: str, outlet: str) -> tuple[str, list[Article], str | None]:
    """Fetch and parse one feed. Returns (url, articles, problem_or_None).

    Never raises: one broken feed must not stop the rest of the run. A stale
    feed returns its (old) articles along with a warning.
    """
    try:
        items = parse_feed(download(url), outlet)
        return url, items, stale_warning(items)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return url, [], f"network error: {exc}"
    except ET.ParseError as exc:
        return url, [], f"not valid XML: {exc}"


def fetch_all(outlets: tuple[Outlet, ...]) -> tuple[list[Article], dict[str, str]]:
    """Fetch every enabled outlet's feeds concurrently.

    Returns (articles, errors) where errors maps feed URL -> message.
    Articles are de-duplicated by normalised URL; the first outlet seen wins.
    """
    jobs = [(url, o.name) for o in outlets if o.enabled for url in o.feeds]
    seen: dict[str, Article] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for url, articles, error in pool.map(lambda job: fetch_feed(*job), jobs):
            if error:
                log.warning("%s: %s", url, error)
                errors[url] = error
            for art in articles:
                seen.setdefault(art.id, art)

    return list(seen.values()), errors
