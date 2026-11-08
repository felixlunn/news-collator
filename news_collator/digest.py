"""Rendering department digests as HTML (for reading) or Markdown (for pasting)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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


# ---------------------------------------------------------------------------
# HTML
#
# Design: a monitoring log. Each item is a row with its time in a left-hand
# gutter so researchers can scan times and headlines down the page. Official
# items sit in their own group at the top of each department, marked with a
# Commons-green rule. Visited links turn grey so readers can see what they've
# already opened. No cards, no badges, no decoration.
# ---------------------------------------------------------------------------

# Items per group on the homepage before linking through to the department page.
INDEX_OFFICIAL_LIMIT = 3
INDEX_NEWS_LIMIT = 6

_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
          'family=Public+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap">')

_CSS = """
:root{
  --paper:#ffffff; --panel:#f3f4f6; --ink:#1d2125; --muted:#5f6670; --faint:#8a929c;
  --rule:#dde1e6; --accent:#006548; --visited:#6c737c; --mark:#fff3b0;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#16191d; --panel:#1d2126; --ink:#e6e8eb; --muted:#9aa3ad; --faint:#737c86;
  --rule:#2c3238; --accent:#4fb08a; --visited:#8b939c; --mark:#4a4320;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:16px/1.5 "Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-variant-numeric:tabular-nums}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.skip{position:absolute;left:-999px}
.skip:focus{left:16px;top:8px;background:var(--paper);padding:4px 8px;z-index:10}

/* Masthead */
.top{border-bottom:1px solid var(--rule)}
.top-inner{max-width:1180px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:24px}
.brand{font-weight:700;font-size:17px;letter-spacing:-.01em;text-decoration:none;white-space:nowrap}
.updated{color:var(--muted);font-size:14px;margin-right:auto}
.filter{width:240px;padding:7px 10px;font:inherit;font-size:14px;color:var(--ink);background:var(--paper);
  border:1px solid var(--rule);border-radius:4px}
.filter::placeholder{color:var(--faint)}

/* Two-column frame */
.frame{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:248px minmax(0,1fr);gap:48px;padding:0 24px}
.depts{position:sticky;top:0;align-self:start;max-height:100vh;overflow-y:auto;padding:24px 0}
.depts ul{list-style:none;margin:0;padding:0}
.depts a{display:flex;justify-content:space-between;gap:12px;padding:6px 10px;margin:0 -10px;border-radius:4px;
  text-decoration:none;font-size:14px;line-height:1.35;color:var(--muted)}
.depts a:hover{background:var(--panel);color:var(--ink)}
.depts a[aria-current]{background:var(--panel);color:var(--ink);font-weight:600}
.depts .n{color:var(--faint);font-weight:400}
.depts a.none{color:var(--faint)}
.depts .all{margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--rule)}
.dept-select{display:none}

main{padding:28px 0 64px;max-width:760px}
.page-title{font-size:30px;line-height:1.15;font-weight:700;letter-spacing:-.02em;margin:0 0 6px}
.page-sub{color:var(--muted);margin:0 0 8px;font-size:15px}

/* Department sections */
.dept{padding-top:28px}
.dept + .dept{margin-top:20px;border-top:1px solid var(--rule)}
.dept-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:4px}
.dept-head h2{font-size:21px;line-height:1.25;font-weight:700;letter-spacing:-.01em;margin:0}
.dept-head a{font-size:14px;color:var(--muted);white-space:nowrap}
.group{margin-top:18px}
.group h3{font-size:14px;font-weight:600;color:var(--muted);margin:0 0 4px}
.group.official h3{color:var(--accent)}
.items{list-style:none;margin:0;padding:0}

/* A single item: time gutter + body */
.item{display:grid;grid-template-columns:52px minmax(0,1fr);gap:14px;padding:10px 0}
.item + .item{border-top:1px solid var(--rule)}
.item time{color:var(--faint);font-size:14px;line-height:1.5;padding-top:1px}
.official .items{border-left:3px solid var(--accent);padding-left:14px;margin-left:-17px}
.headline{font-weight:600;font-size:17px;line-height:1.35;text-decoration:none}
.headline:hover{text-decoration:underline;text-underline-offset:3px}
.headline:visited{color:var(--visited);font-weight:500}
.source{margin:3px 0 0;font-size:14px;color:var(--muted)}
.official .source{color:var(--accent)}
.source a{color:var(--muted)}
.summary{margin:5px 0 0;font-size:15px;line-height:1.5;color:var(--ink);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.cross{margin:5px 0 0;font-size:13px;color:var(--faint)}
.more{display:inline-block;margin-top:10px;font-size:14px;color:var(--muted)}
.empty{color:var(--muted);margin:12px 0 0;font-size:15px}
.no-match{display:none;color:var(--muted);margin-top:24px}
footer{color:var(--faint);font-size:13px;line-height:1.5;border-top:1px solid var(--rule);margin-top:48px;padding-top:16px}
mark{background:var(--mark);color:inherit}

/* Phones and narrow windows: department list becomes a dropdown */
@media (max-width:860px){
  .top-inner{flex-wrap:wrap;gap:8px 16px;padding:12px 16px}
  .updated{margin-right:0;flex:1 1 auto}
  .filter{width:100%;order:3}
  .frame{display:block;padding:0 16px}
  .depts{display:none}
  .dept-select{display:block;width:100%;margin:16px 0 22px;padding:8px 10px;font:inherit;font-size:15px;
    color:var(--ink);background:var(--panel);border:1px solid var(--rule);border-radius:4px}
  main{padding-top:20px}
  .page-title{font-size:25px}
  .item{grid-template-columns:44px minmax(0,1fr);gap:10px}
  .official .items{padding-left:10px;margin-left:-13px}
  .headline{font-size:16px}
}
@media print{.top .filter,.depts,.dept-select{display:none}.frame{display:block}}
"""

# Filtering hides non-matching items, then any group or department left empty.
_JS = """
(function(){
  var q=document.getElementById('filter'), sel=document.getElementById('dept-select');
  if(sel){sel.addEventListener('change',function(){if(sel.value)location.href=sel.value;});}
  if(!q)return;
  var items=[].slice.call(document.querySelectorAll('.item'));
  var none=document.getElementById('no-match');
  q.addEventListener('input',function(){
    var v=q.value.trim().toLowerCase(), any=false;
    items.forEach(function(li){var hit=!v||li.textContent.toLowerCase().indexOf(v)>-1;li.hidden=!hit;any=any||hit;});
    document.querySelectorAll('.group').forEach(function(g){g.hidden=!g.querySelector('.item:not([hidden])');});
    document.querySelectorAll('.dept').forEach(function(d){
      d.hidden=!!v&&!d.querySelector('.item:not([hidden])');});
    none.style.display=any?'none':'block';
  });
})();
"""


def _local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def _day_label(day: date, today: date) -> str:
    """'Today', 'Yesterday', or e.g. 'Friday 6 November'."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A} {day.day} {day:%B}"


def _gutter(art: Article, today: date, *, in_day_group: bool) -> tuple[str, str]:
    """(display text, machine datetime) for the time column.

    News inside a day group shows just the time. Official items are not grouped
    by day, so they show the time if today and the date otherwise. Written
    statements carry only a date.
    """
    if art.published is None:
        return "", ""
    local = _local(art.published)
    machine = local.isoformat()
    if art.kind == WRITTEN_STATEMENT:
        day = art.published.date()  # stored as a bare date; don't shift across midnight
        return ("Today" if day == today else f"{day.day} {day:%b}"), day.isoformat()
    if in_day_group or local.date() == today:
        return f"{local:%H:%M}", machine
    return f"{local.day} {local:%b}", machine


def _source_line(story: Story) -> str:
    """Who published it, plus links to other outlets covering the same story."""
    esc, a = html.escape, story.lead
    if a.kind == WRITTEN_STATEMENT:
        house = a.outlet.removeprefix("Parliament (").rstrip(")") if "(" in a.outlet else ""
        label = f"Written ministerial statement, {house}" if house else "Written ministerial statement"
    elif a.is_official:
        label = "GOV.UK announcement"
    else:
        label = a.outlet
    text = esc(label)
    if story.others:
        links = ", ".join(f'<a href="{esc(o.url)}" target="_blank" rel="noopener">{esc(o.outlet)}</a>'
                          for o in story.others)
        text += f"; also reported by {links}"
    return text


def _item(story: Story, slug: str, names: dict[str, str], today: date, *, in_day_group: bool,
          summary: bool = True) -> str:
    esc, a = html.escape, story.lead
    shown, machine = _gutter(a, today, in_day_group=in_day_group)
    parts = [f'<li class="item"><time datetime="{machine}">{esc(shown)}</time><div>',
             f'<a class="headline" href="{esc(a.url)}" target="_blank" rel="noopener">{esc(a.title)}</a>',
             f'<p class="source">{_source_line(story)}</p>']
    if summary and a.summary:
        parts.append(f'<p class="summary">{esc(_truncate(a.summary))}</p>')
    cross = [names[d] for d in a.departments if d != slug and d in names]
    if cross and summary:
        parts.append(f'<p class="cross">Also filed under {esc(", ".join(cross))}</p>')
    parts.append("</div></li>")
    return "".join(parts)


def _department_body(slug: str, stories: list[Story], names: dict[str, str], today: date,
                     limit: bool) -> str:
    """Official group, then news grouped by day.

    With `limit` (homepage), show a headline-only preview of each group.
    """
    official = [s for s in stories if s.lead.is_official]
    news = [s for s in stories if not s.lead.is_official]
    if limit:
        official, news = official[:INDEX_OFFICIAL_LIMIT], news[:INDEX_NEWS_LIMIT]

    out = []
    if official:
        rows = "".join(_item(s, slug, names, today, in_day_group=False, summary=not limit) for s in official)
        out.append(f'<div class="group official"><h3>Official</h3><ol class="items">{rows}</ol></div>')

    # News arrives sorted newest day first, so consecutive runs share a day.
    by_day: list[tuple[str, list[Story]]] = []
    for s in news:
        label = _day_label(_local(s.lead.published).date(), today) if s.lead.published else "Undated"
        if by_day and by_day[-1][0] == label:
            by_day[-1][1].append(s)
        else:
            by_day.append((label, [s]))
    for label, group in by_day:
        rows = "".join(_item(s, slug, names, today, in_day_group=True, summary=not limit) for s in group)
        out.append(f'<div class="group"><h3>{html.escape(label)}</h3><ol class="items">{rows}</ol></div>')
    return "".join(out)


def _count_phrase(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render_html(articles: list[Article], config: Config, hours: int,
                only: str | None = None, site: bool = False) -> str:
    """Self-contained HTML page.

    - only=None: every department (the homepage). In site mode each department
      shows a preview and links to its own page.
    - only=<slug>: one department in full.
    - site=True: page belongs to the multi-page website from `build_site`
      (index.html at the root, department pages under departments/).
    """
    esc = html.escape
    names = {d.slug: d.name for d in config.departments}
    sections = build_sections(articles, config)
    now = datetime.now(timezone.utc)
    today = _local(now).date()
    local_now = _local(now)
    updated = f"Updated {local_now.day} {local_now:%B %Y, %H:%M}"
    window = "3 days" if hours == 72 else _count_phrase(hours, "hour")

    # Links: between pages in site mode, to anchors in a single-file digest.
    if site:
        home = "../index.html" if only else "index.html"
        dept_href = (lambda s: f"{s}.html") if only else (lambda s: f"departments/{s}.html")
    else:
        home, dept_href = "#top", (lambda s: f"#{s}")

    # Sidebar (desktop) and dropdown (phones) list every department with its count.
    home_current = "" if only else ' aria-current="page"'
    side = [f'<li class="all"><a href="{home}"{home_current}>All departments</a></li>']
    options = [f'<option value="{home}">All departments</option>']
    for slug, name, stories in sections:
        current = ' aria-current="page"' if slug == only else ""
        current += "" if stories else ' class="none"'
        side.append(f'<li><a href="{dept_href(slug)}"{current}><span>{esc(name)}</span>'
                    f'<span class="n">{len(stories)}</span></a></li>')
        selected = " selected" if slug == only else ""
        options.append(f'<option value="{dept_href(slug)}"{selected}>{esc(name)} ({len(stories)})</option>')

    # Main content.
    body = []
    if only:
        slug, name, stories = next(s for s in sections if s[0] == only)
        n_official = sum(s.lead.is_official for s in stories)
        sub = f"{_count_phrase(len(stories), 'item')} from the last {window}"
        if n_official:
            sub += f", including {n_official} official"
        body.append(f'<h1 class="page-title">{esc(name)}</h1><p class="page-sub">{sub}.</p>')
        content = (_department_body(slug, stories, names, today, limit=False)
                   or '<p class="empty">No coverage in this period.</p>')
        body.append(f'<section class="dept" id="{slug}">{content}</section>')
    else:
        total = sum(len(s[2]) for s in sections)
        body.append(f'<h1 class="page-title">Policy news</h1>'
                    f'<p class="page-sub">Coverage and official announcements from the last {window}, '
                    f'filed by department. {_count_phrase(total, "item")} in total.</p>')
        for slug, name, stories in sections:
            if not stories:
                continue
            # The homepage previews each department; the full list is on its own page.
            n_off = sum(s.lead.is_official for s in stories)
            shown = min(n_off, INDEX_OFFICIAL_LIMIT) + min(len(stories) - n_off, INDEX_NEWS_LIMIT)
            more = (f'<a href="{dept_href(slug)}">View all {len(stories)}</a>'
                    if site and len(stories) > shown else "")
            body.append(f'<section class="dept" id="{slug}"><div class="dept-head"><h2>{esc(name)}</h2>{more}</div>'
                        f'{_department_body(slug, stories, names, today, limit=site)}</section>')

    outlets = ", ".join(o.name for o in config.outlets if o.enabled)
    official = [label for on, label in ((config.official.govuk, "GOV.UK"),
                                        (config.official.written_statements, "UK Parliament written statements"))
                if on]
    sources = outlets + (f"; official: {', '.join(official)}" if official else "")

    title = f"{names[only]}: policy news" if only else "Policy news"
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{_FONTS}<style>{_CSS}</style></head>
<body id="top">
<a class="skip" href="#main">Skip to stories</a>
<header class="top"><div class="top-inner">
<a class="brand" href="{home}">Policy news monitor</a>
<span class="updated">{updated}</span>
<input id="filter" class="filter" type="search" placeholder="Filter headlines" aria-label="Filter headlines">
</div></header>
<div class="frame">
<nav class="depts" aria-label="Departments"><ul>{''.join(side)}</ul></nav>
<main id="main">
<select id="dept-select" class="dept-select" aria-label="Choose a department">{''.join(options)}</select>
{''.join(body)}
<p id="no-match" class="no-match">No headlines match that filter.</p>
<footer>Times are UK time. Headlines are matched to departments automatically by keyword; official items are filed by the issuing department. Sources: {esc(sources)}.</footer>
</main></div>
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
