"""Story grouping and ordering, plus the Markdown digest. HTML lives in web.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .models import KIND_LABELS, WRITTEN_STATEMENT, Article

# Similar headlines from different outlets are grouped into one "story" so
# researchers see each event once, with links to every outlet's version.
SIMILARITY_THRESHOLD = 0.45

# Readers are UK-based: days and times are shown in UK time.
try:
    LOCAL_TZ = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError:  # e.g. Windows without the tzdata package
    LOCAL_TZ = timezone.utc
SUMMARY_CHARS = 280

_STOPWORDS = set("""a an the and or but of to in on at for with by from as is are was were be been
it its this that these those after over into about says say said will would could may new uk
""".split())


# ---------------------------------------------------------------------------
# Story grouping
# ---------------------------------------------------------------------------

def _tokens(text: str) -> frozenset[str]:
    """Content words of a headline, used for similarity."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap coefficient: shared words relative to the shorter headline."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Story:
    """One event, possibly reported by several outlets."""
    lead: Article
    others: list[Article] = field(default_factory=list)
    tokens: frozenset[str] = field(default=frozenset(), repr=False)

    @property
    def outlets(self) -> list[str]:
        return [self.lead.outlet] + [a.outlet for a in self.others]


def group_stories(articles: list[Article]) -> list[Story]:
    """Greedy single-pass grouping. Input should be sorted by priority;
    the first article seen becomes each story's lead.

    Only groups articles from *different* outlets — one outlet rarely runs two
    versions of the same story, and merging them would hide distinct pieces.
    Official items (GOV.UK, written statements) are never grouped: each is
    shown as published, pinned in its own right.
    """
    stories: list[Story] = []
    for art in articles:
        if art.is_official:
            stories.append(Story(lead=art))
            continue
        toks = _tokens(art.title)
        for story in stories:
            if story.lead.is_official:
                continue
            if art.outlet not in story.outlets and _similarity(toks, story.tokens) >= SIMILARITY_THRESHOLD:
                story.others.append(art)
                break
        else:
            stories.append(Story(lead=art, tokens=toks))
    return stories


def _sort_key(art: Article, slug: str) -> tuple:
    """Official items pinned first (newest first); then news.

    News is ordered newest first. The page groups it by day with times in a
    left-hand column, so strict time order is what reads correctly; relevance
    only breaks ties. Undated items sort last within their group.
    """
    pinned = 0 if art.is_official else 1
    if art.published is None:
        return (pinned, 1, 0, 0, 0)
    ts = art.published.timestamp()
    if art.is_official:
        return (pinned, 0, 0, 0, -ts)
    return (pinned, 0, -ts, -art.departments.get(slug, 0), 0)


def build_sections(articles: list[Article], config: Config) -> list[tuple[str, str, list[Story]]]:
    """Return [(slug, display_name, stories)] for every department, in config order."""
    sections = []
    for dept in config.departments:
        mine = sorted((a for a in articles if dept.slug in a.departments),
                      key=lambda a: _sort_key(a, dept.slug))
        sections.append((dept.slug, dept.name, group_stories(mine)))
    return sections


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = SUMMARY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _when(art: Article) -> str:
    """Display time. Written statements only carry a date, so show just that."""
    if art.published is None:
        return "undated"
    if art.kind == WRITTEN_STATEMENT:
        return art.published.strftime("%d %b")
    return art.published.astimezone(LOCAL_TZ).strftime("%d %b %H:%M")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(articles: list[Article], config: Config, hours: int,
                    only: str | None = None) -> str:
    """Plain Markdown digest — good for email or pasting into a briefing doc."""
    generated = datetime.now(LOCAL_TZ).strftime("%d %B %Y, %H:%M")
    lines = ["# Policy news digest", f"_Last {hours} hours · generated {generated}_", ""]
    names = {d.slug: d.name for d in config.departments}

    for slug, name, stories in build_sections(articles, config):
        if (only and slug != only) or not stories:
            continue
        lines += [f"## {name} ({len(stories)})", ""]
        for s in stories:
            a = s.lead
            pin = f"*Official, {KIND_LABELS[a.kind]}:* " if a.is_official else ""
            lines.append(f"- {pin}**[{a.title}]({a.url})** — {a.outlet}, {_when(a)}")
            if a.summary:
                lines.append(f"  {_truncate(a.summary)}")
            if s.others:
                also = ", ".join(f"[{o.outlet}]({o.url})" for o in s.others)
                lines.append(f"  _Also covered by: {also}_")
            cross = [names[d] for d in a.departments if d != slug and d in names]
            if cross:
                lines.append(f"  _Also relevant to: {', '.join(cross)}_")
        lines.append("")
    return "\n".join(lines)
