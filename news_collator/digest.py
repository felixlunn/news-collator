"""Rendering department digests as HTML (for reading) or Markdown (for pasting)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import Article

# Similar headlines from different outlets are grouped into one "story" so
# researchers see each event once, with links to every outlet's version.
SIMILARITY_THRESHOLD = 0.45
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
    """
    stories: list[Story] = []
    for art in articles:
        toks = _tokens(art.title)
        for story in stories:
            if art.outlet not in story.outlets and _similarity(toks, story.tokens) >= SIMILARITY_THRESHOLD:
                story.others.append(art)
                break
        else:
            stories.append(Story(lead=art, tokens=toks))
    return stories


def _sort_key(art: Article, slug: str) -> tuple:
    """Most relevant first, then newest."""
    ts = art.published.timestamp() if art.published else 0
    return (-art.departments.get(slug, 0), -ts)


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


def _when(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%d %b %H:%M UTC") if dt else "undated"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(articles: list[Article], config: Config, hours: int,
                    only: str | None = None) -> str:
    """Plain Markdown digest — good for email or pasting into a briefing doc."""
    generated = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    lines = ["# Policy news digest", f"_Last {hours} hours · generated {generated}_", ""]
    names = {d.slug: d.name for d in config.departments}

    for slug, name, stories in build_sections(articles, config):
        if (only and slug != only) or not stories:
            continue
        lines += [f"## {name} ({len(stories)})", ""]
        for s in stories:
            a = s.lead
            lines.append(f"- **[{a.title}]({a.url})** — {a.outlet}, {_when(a.published)}")
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


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e3e1da;--accent:#1f4e79;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1c1e21;--ink:#e8e6e1;--muted:#9a988f;--line:#2c2f33;--accent:#8ab4e0;--chip:#24292f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:20px 16px 8px;max-width:900px;margin:auto}
h1{margin:0;font-size:22px}
.meta{color:var(--muted);font-size:13px}
nav{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);z-index:1}
nav .wrap{max-width:900px;margin:auto;padding:8px 16px;display:flex;gap:6px;overflow-x:auto}
nav a{white-space:nowrap;text-decoration:none;color:var(--ink);background:var(--chip);padding:4px 10px;border-radius:999px;font-size:13px}
nav a span{color:var(--muted)}
#q{width:100%;padding:8px 10px;margin-top:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit}
main{max-width:900px;margin:auto;padding:0 16px 40px}
section{scroll-margin-top:52px}
h2{font-size:17px;margin:28px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--accent)}
article{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
article a.t{color:var(--accent);font-weight:600;text-decoration:none}
article a.t:hover{text-decoration:underline}
.src{color:var(--muted);font-size:12px;margin-top:2px}
.sum{margin:6px 0 0;font-size:14px}
.also{font-size:12px;color:var(--muted);margin-top:6px}
.also a{color:var(--muted)}
.tag{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;margin-right:4px;font-size:11px}
.empty{color:var(--muted);font-style:italic}
nav a.on{background:var(--accent);color:var(--bg)}nav a.on span{color:var(--bg)}
h2 a.page{float:right;font-size:12px;font-weight:normal;color:var(--accent);text-decoration:none;margin-top:4px}
"""

# Client-side filter: hides stories whose text doesn't contain the query.
_JS = """
const q=document.getElementById('q');
q.addEventListener('input',()=>{const v=q.value.toLowerCase();
document.querySelectorAll('article').forEach(a=>{a.style.display=a.textContent.toLowerCase().includes(v)?'':'none'});});
"""


def render_html(articles: list[Article], config: Config, hours: int,
                only: str | None = None, site: bool = False) -> str:
    """Self-contained HTML page with a department navigation bar and text filter.

    With site=True the page is part of the multi-page website built by
    `build_site`: the navigation links between pages instead of to anchors
    (index.html at the root, department pages under departments/).
    """
    esc = html.escape
    names = {d.slug: d.name for d in config.departments}
    all_sections = build_sections(articles, config)
    sections = [s for s in all_sections if not only or s[0] == only]
    generated = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    total = len(articles) if not only else sum(len(s[2]) for s in sections)

    if site and only:
        # Department page: jump to the homepage or any other department's page.
        chips = ['<a href="../index.html">← All departments</a>']
        for slug, name, stories in all_sections:
            current = ' class="on" aria-current="page"' if slug == only else ""
            chips.append(f'<a href="{slug}.html"{current}>{esc(name)} <span>{len(stories)}</span></a>')
        nav = "".join(chips)
    else:
        nav = "".join(f'<a href="#{slug}">{esc(name)} <span>{len(stories)}</span></a>'
                      for slug, name, stories in sections if stories)

    body = []
    for slug, name, stories in sections:
        if not stories and not only:
            continue  # skip empty departments in the combined view
        team_link = (f' <a class="page" href="departments/{slug}.html">Department page →</a>'
                     if site and not only else "")
        body.append(f'<section id="{slug}"><h2>{esc(name)}{team_link}</h2>')
        if not stories:
            body.append('<p class="empty">Nothing in this window.</p>')
        for s in stories:
            a = s.lead
            parts = [f'<article><a class="t" href="{esc(a.url)}" target="_blank" rel="noopener">{esc(a.title)}</a>',
                     f'<div class="src">{esc(a.outlet)} · {_when(a.published)}</div>']
            if a.summary:
                parts.append(f'<p class="sum">{esc(_truncate(a.summary))}</p>')
            if s.others:
                links = ", ".join(f'<a href="{esc(o.url)}" target="_blank" rel="noopener">{esc(o.outlet)}</a>'
                                  for o in s.others)
                parts.append(f'<div class="also">Also covered by: {links}</div>')
            cross = [names[d] for d in a.departments if d != slug and d in names]
            if cross:
                tags = "".join(f'<span class="tag">{esc(c)}</span>' for c in cross)
                parts.append(f'<div class="also">Also relevant to: {tags}</div>')
            parts.append("</article>")
            body.append("".join(parts))
        body.append("</section>")

    title = f"{names[only]} — news digest" if only else "Policy news digest"
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{_CSS}</style></head>
<body>
<header><h1>{esc(title)}</h1>
<div class="meta">Last {hours} hours · {total} article{"" if total == 1 else "s"} · generated {generated}</div>
<input id="q" type="search" placeholder="Filter stories…" aria-label="Filter stories"></header>
<nav><div class="wrap">{nav}</div></nav>
<main>{''.join(body)}</main>
<script>{_JS}</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Static website
# ---------------------------------------------------------------------------

def build_site(articles: list[Article], config: Config, hours: int, out_dir: Path) -> list[Path]:
    """Write a static website to `out_dir`, ready for GitHub Pages or any web host.

        index.html                  combined digest, all departments
        departments/<slug>.html     one page per department (bookmarkable by teams)

    Returns the paths written.
    """
    (out_dir / "departments").mkdir(parents=True, exist_ok=True)
    # Tell GitHub Pages to serve files as-is rather than through Jekyll.
    (out_dir / ".nojekyll").touch()

    written = [out_dir / "index.html"]
    written[0].write_text(render_html(articles, config, hours, site=True), encoding="utf-8")
    for dept in config.departments:
        path = out_dir / "departments" / f"{dept.slug}.html"
        path.write_text(render_html(articles, config, hours, only=dept.slug, site=True), encoding="utf-8")
        written.append(path)
    return written
