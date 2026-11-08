"""The Despatch website: homepage, department and tag pages, channels and open data.

Design notes
------------
- One bold element: a solid Commons-green masthead with a condensed wordmark.
  Everything below it is quiet: white paper, near-black text, hairline rules.
- Type is Archivo throughout, using its width axis for hierarchy: condensed
  and heavy for the wordmark and department titles, slightly narrow for
  headlines (more words per line when scanning), normal width for body text.
- The homepage answers "what matters and where?" with two blocks:
    1. Most reported – stories carried by the most outlets, across departments.
    2. Department overview – a table of item counts per day, shaded by volume.
- Department pages are a log: official items pinned first, then news grouped
  by day with the time in a left-hand column, under a three-day tally.
- Team tag pages use the same layout. The Channels page is a small browser
  app (static/channels.js) that combines departments, team tags and the
  reader's own tags, saved in their browser and shareable by link.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .digest import LOCAL_TZ, Story, _sort_key, _truncate, build_sections, group_stories
from .models import SEARCH, WRITTEN_STATEMENT, Article
from .tags import Tag, TagMatcher

SITE_NAME = "Despatch"
SITE_TAGLINE = "UK policy news by government department"
MOST_REPORTED_LIMIT = 8
MOST_REPORTED_MIN_OUTLETS = 2  # a story needs at least this many outlets to appear

esc = html.escape

# ---------------------------------------------------------------------------
# Styles and script
# ---------------------------------------------------------------------------

_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
          'family=Archivo:wdth,wght@62..125,400..800&display=swap">')

_CSS = """
:root{
  --masthead:#00422e; --accent:#00704a; --accent-ink:#ffffff;
  --paper:#ffffff; --panel:#f4f6f5; --ink:#15191c; --muted:#59616a; --faint:#868e96; --rule:#e1e4e7;
  --visited:#6c737c;
  /* Volume shading for the overview table, light to dark */
  --v1:#e8f3ed; --v2:#c4e2d2; --v3:#86c3a3; --v4:#3f9a6f; --v5:#00704a;
}
@media (prefers-color-scheme:dark){:root{
  --masthead:#0b2e22; --accent:#5cc095; --accent-ink:#0b1410;
  --paper:#121517; --panel:#1a1e21; --ink:#e7e9eb; --muted:#9ba3ab; --faint:#747c84; --rule:#2a3034;
  --visited:#8d959d;
  --v1:#16261f; --v2:#1c3a2d; --v3:#24563f; --v4:#2f7a57; --v5:#3fa173;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:16px/1.5 Archivo,"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.skip{position:absolute;left:-999px}
.skip:focus{left:16px;top:8px;background:var(--paper);padding:4px 8px;z-index:10}

/* Masthead: the one strong element */
.mast{background:var(--masthead);color:#fff}
.mast-inner{max-width:1180px;margin:0 auto;padding:0 24px;height:68px;display:flex;align-items:center;gap:20px}
.wordmark{font-size:30px;line-height:1;font-weight:800;font-stretch:72%;letter-spacing:-.01em;
  text-decoration:none;color:#fff}
.tagline{font-size:14px;color:rgba(255,255,255,.72);margin-right:auto;padding-left:20px;
  border-left:1px solid rgba(255,255,255,.25);line-height:1.3}
.updated{font-size:14px;color:rgba(255,255,255,.85);white-space:nowrap}
.mast-link{font-size:14px;font-weight:600;color:#fff;text-decoration:none;white-space:nowrap;
  padding:6px 0;border-bottom:1px solid rgba(255,255,255,.45)}
.mast-link:hover{border-bottom-color:#fff}
.filter{width:220px;padding:7px 10px;font:inherit;font-size:14px;color:#fff;
  background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.3);border-radius:3px}
.filter::placeholder{color:rgba(255,255,255,.65)}
.filter:focus-visible{outline-color:#fff}

/* Page frames */
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
.frame{display:grid;grid-template-columns:232px minmax(0,1fr);gap:56px}
main{padding:36px 0 72px;min-width:0}
.single main{max-width:none}
.front{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:56px;align-items:start}
.front > .block{margin-top:40px}
.intro{max-width:760px}
.frame main{max-width:760px}

/* Department sidebar */
.depts{position:sticky;top:0;align-self:start;max-height:100vh;overflow-y:auto;padding:36px 0 24px}
.depts ul{list-style:none;margin:0;padding:0}
.depts a{display:flex;justify-content:space-between;gap:12px;padding:5px 0;text-decoration:none;
  font-size:14px;line-height:1.35;color:var(--muted);border-left:2px solid transparent;padding-left:10px;margin-left:-12px}
.depts a:hover{color:var(--ink)}
.depts a[aria-current]{color:var(--ink);font-weight:600;border-left-color:var(--accent)}
.depts a.none{color:var(--faint)}
.depts .n{color:var(--faint);font-weight:400}
.depts ul.home{margin-bottom:6px}
.nav-head{font-size:12px;font-weight:600;color:var(--faint);margin:18px 0 4px;padding-top:12px;border-top:1px solid var(--rule)}
.dept-select{display:none}

/* Headings */
.page-title{font-size:44px;line-height:1.02;font-weight:800;font-stretch:78%;letter-spacing:-.015em;margin:0}
.page-sub{color:var(--muted);margin:10px 0 0;font-size:15px}
.page-sub a{color:inherit}
.terms{color:var(--faint);margin:4px 0 0;font-size:13px}
.block-head p a{color:var(--muted)}
.block{margin-top:44px}
.block-head{border-bottom:2px solid var(--ink);padding-bottom:8px;margin-bottom:4px}
.block-head h2{font-size:22px;line-height:1.2;font-weight:750;font-stretch:85%;margin:0}
.block-head p{margin:2px 0 0;font-size:14px;color:var(--muted)}

/* Story rows (shared by every list) */
.items{list-style:none;margin:0;padding:0}
.item{display:grid;grid-template-columns:54px minmax(0,1fr);gap:14px;padding:12px 0}
.item + .item{border-top:1px solid var(--rule)}
.gutter{color:var(--faint);font-size:14px;line-height:1.45;padding-top:2px}
.headline{font-weight:650;font-stretch:92%;font-size:18px;line-height:1.3;text-decoration:none}
.headline:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
.headline:visited{color:var(--visited);font-weight:550}
.source{margin:4px 0 0;font-size:14px;color:var(--muted)}
.source a,.filed a{color:var(--muted);text-decoration:none;border-bottom:1px solid var(--rule)}
.source a:hover,.filed a:hover{color:var(--ink);border-bottom-color:currentColor}
.summary{margin:6px 0 0;font-size:15px;line-height:1.5;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.filed{margin:6px 0 0;font-size:13px;color:var(--faint)}

/* Most reported: the gutter carries the outlet count */
.reach{display:flex;flex-direction:column;line-height:1}
.reach b{font-size:30px;font-weight:800;font-stretch:72%;color:var(--accent)}
.reach span{font-size:12px;color:var(--faint);margin-top:4px}

/* Department overview table */
.overview{width:100%;border-collapse:collapse;margin-top:2px}
.overview th,.overview td{padding:0;border-bottom:1px solid var(--rule);text-align:left}
.overview thead th{font-size:13px;font-weight:600;color:var(--muted);padding:10px 0 8px}
.overview thead th.num{text-align:center}
.overview tbody th{font-weight:500;font-size:15px}
.overview tbody th a{display:block;padding:9px 12px 9px 0;text-decoration:none}
.overview tbody tr:hover th a{text-decoration:underline;text-underline-offset:3px}
.overview td{width:58px;text-align:center}
.overview td a{display:block;margin:3px 2px;padding:6px 0;font-size:14px;text-decoration:none;border-radius:2px}
.overview td.zero a{color:var(--faint)}
.overview td.official a{font-weight:600;color:var(--accent)}
.overview td.official.zero a{color:var(--faint);font-weight:400}
.v1{background:var(--v1)}.v2{background:var(--v2)}.v3{background:var(--v3)}
.v4{background:var(--v4);color:#fff}.v5{background:var(--v5);color:#fff}
@media (prefers-color-scheme:dark){.v3,.v4,.v5{color:#fff}}
.short{display:none}

/* Department page */
.dept-head{display:flex;justify-content:space-between;align-items:flex-end;gap:24px}
.tally{display:flex;gap:24px;margin:0;flex:none}
.tally div{display:flex;flex-direction:column;line-height:1}
.tally dd{margin:0;font-size:30px;font-weight:800;font-stretch:72%;color:var(--accent);order:-1}
.tally dt{font-size:12px;color:var(--faint);margin-top:4px}
.group{margin-top:30px}
.group h3{font-size:15px;font-weight:700;font-stretch:88%;margin:0 0 2px;padding-bottom:6px;border-bottom:1px solid var(--ink)}
.group.official h3{color:var(--accent);border-bottom-color:var(--accent)}
.official .items{border-left:3px solid var(--accent);padding-left:14px;margin-left:-17px}
.official .source{color:var(--accent)}
.empty{color:var(--muted);margin:24px 0 0}
.no-match{display:none;color:var(--muted);margin-top:28px}

footer{color:var(--faint);font-size:13px;line-height:1.55;border-top:1px solid var(--rule);margin-top:56px;padding-top:16px}

/* Narrow screens */
@media (max-width:900px){
  .tagline{display:none}
  .mast-inner{height:auto;min-height:60px;flex-wrap:wrap;gap:6px 16px;padding:12px 16px}
  .wordmark{font-size:26px;margin-right:auto}
  .updated{order:3;flex-basis:100%;font-size:13px}
  .filter{width:100%;order:3;margin-bottom:2px}
  .wrap{padding:0 16px}
  .frame{display:block}
  .depts{display:none}
  .dept-select{display:block;width:100%;margin:18px 0 0;padding:9px 10px;font:inherit;font-size:15px;
    color:var(--ink);background:var(--panel);border:1px solid var(--rule);border-radius:3px}
  main{padding-top:24px}
  .page-title{font-size:34px}
  .item{grid-template-columns:44px minmax(0,1fr);gap:10px}
  .headline{font-size:17px}
  .official .items{padding-left:10px;margin-left:-13px}
  .overview td{width:46px}
  .overview tbody th{font-size:14px}
  .front{display:block}
  .full{display:none}.short{display:inline}
  .dept-head{display:block}
  .tally{margin-top:16px}
}
@media print{.mast{background:none;color:#000}.wordmark{color:#000}.filter,.depts,.dept-select{display:none}
  .frame{display:block}}
"""

# Filtering hides non-matching rows, then any group left empty.
_JS = """
(function(){
  var sel=document.getElementById('dept-select');
  if(sel)sel.addEventListener('change',function(){if(sel.value)location.href=sel.value;});
  var q=document.getElementById('filter');
  if(!q)return;
  var items=[].slice.call(document.querySelectorAll('.item')), none=document.getElementById('no-match');
  q.addEventListener('input',function(){
    var v=q.value.trim().toLowerCase(), any=false;
    items.forEach(function(li){var hit=!v||li.textContent.toLowerCase().indexOf(v)>-1;li.hidden=!hit;any=any||hit;});
    document.querySelectorAll('.group,.block').forEach(function(g){
      if(g.querySelector('.item'))g.hidden=!g.querySelector('.item:not([hidden])');});
    none.style.display=any?'none':'block';
  });
})();
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _item_day(art: Article) -> date | None:
    """Calendar day an item belongs to, in UK time (statements keep their bare date)."""
    if art.published is None:
        return None
    return art.published.date() if art.kind == WRITTEN_STATEMENT else _local(art.published).date()


def _day_label(day: date, today: date) -> str:
    """'Today', 'Yesterday', or e.g. 'Friday 6 November'."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A} {day.day} {day:%B}"


def _short_day(day: date, today: date) -> str:
    """Compact column label: 'Today', 'Yesterday', 'Fri'."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%a}"


def _time_gutter(art: Article, today: date, *, in_day_group: bool) -> str:
    """Time column. Inside a day group: just the time. Otherwise the time if
    today, else the date. Written statements carry only a date."""
    day = _item_day(art)
    if day is None:
        return ""
    if art.kind == WRITTEN_STATEMENT:
        return "Today" if day == today else f"{day.day} {day:%b}"
    local = _local(art.published)
    if in_day_group or day == today:
        return f"{local:%H:%M}"
    return f"{day.day} {day:%b}"


def _source_line(story: Story) -> str:
    """Who published the lead item, plus links to other outlets with the same story."""
    a = story.lead
    if a.kind == WRITTEN_STATEMENT:
        house = a.outlet[len("Parliament ("):-1] if a.outlet.startswith("Parliament (") else ""
        label = f"Written ministerial statement, {house}" if house else "Written ministerial statement"
    elif a.is_official:
        label = "GOV.UK announcement"
    else:
        label = a.outlet
    text = esc(label)
    if story.others:
        links = ", ".join(f'<a href="{esc(o.url)}" target="_blank" rel="noopener">{esc(o.outlet)}</a>'
                          for o in story.others)
        text += f"; also {links}"
    return text


def _row(story: Story, gutter: str, *, summary: bool, filed: str = "") -> str:
    """One story row. `gutter` is pre-rendered HTML for the left-hand column."""
    a = story.lead
    parts = [f'<li class="item"><div class="gutter">{gutter}</div><div>',
             f'<a class="headline" href="{esc(a.url)}" target="_blank" rel="noopener">{esc(a.title)}</a>',
             f'<p class="source">{_source_line(story)}</p>']
    if summary and a.summary:
        parts.append(f'<p class="summary">{esc(_truncate(a.summary))}</p>')
    if filed:
        parts.append(f'<p class="filed">{filed}</p>')
    parts.append("</div></li>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page parts
# ---------------------------------------------------------------------------

class _Links:
    """Where links point. On the website, pages live at the root (index.html,
    channels.html) or one folder down (departments/, tags/), so links depend on
    `depth`. In a single-file digest they point to anchors on the page."""

    def __init__(self, site: bool, depth: int = 0):
        self.site = site
        up = "../" * depth
        if site:
            self.home, self.channels = f"{up}index.html", f"{up}channels.html"
            self._dept, self._tag = f"{up}departments/{{}}.html", f"{up}tags/{{}}.html"
        else:
            self.home, self.channels = "#top", ""
            self._dept, self._tag = "#{}", "#tag-{}"

    def dept(self, slug: str) -> str:
        return self._dept.format(slug)

    def tag(self, slug: str) -> str:
        return self._tag.format(slug)


def _masthead(links: _Links, updated: str, with_filter: bool) -> str:
    filt = ('<input id="filter" class="filter" type="search" placeholder="Filter headlines" '
            'aria-label="Filter headlines">') if with_filter else ""
    chan = f'<a class="mast-link" href="{links.channels}">Your channels</a>' if links.channels else ""
    return (f'<header class="mast"><div class="mast-inner">'
            f'<a class="wordmark" href="{links.home}">{SITE_NAME}</a>'
            f'<span class="tagline">{esc(SITE_TAGLINE)}</span>'
            f'<span class="updated">{esc(updated)}</span>{chan}{filt}</div></header>')


def _most_reported(sections, names: dict[str, str], links: _Links) -> str:
    """Stories carried by the most outlets, across all departments."""
    seen: dict[str, tuple[Story, list[str]]] = {}
    for slug, _, stories in sections:
        for s in stories:
            if s.lead.is_official or len(s.outlets) < MOST_REPORTED_MIN_OUTLETS:
                continue
            entry = seen.setdefault(s.lead.id, (s, []))
            entry[1].append(slug)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ranked = sorted(seen.values(), key=lambda e: (-len(e[0].outlets), -(e[0].lead.published or epoch).timestamp()))
    if not ranked:
        return ""
    rows = []
    for story, slugs in ranked[:MOST_REPORTED_LIMIT]:
        gutter = f'<span class="reach"><b>{len(story.outlets)}</b><span>outlets</span></span>'
        filed = "Filed under " + ", ".join(f'<a href="{links.dept(s)}">{esc(names[s])}</a>' for s in slugs)
        rows.append(_row(story, gutter, summary=False, filed=filed))
    return (f'<section class="block"><div class="block-head"><h2>Most reported</h2>'
            f'<p>Stories carried by the most outlets</p></div><ol class="items">{"".join(rows)}</ol></section>')


def _no_multi_outlet() -> str:
    return ('<section class="block"><div class="block-head"><h2>Most reported</h2></div>'
            '<p class="empty">No story has been carried by more than one outlet yet.</p></section>')


def _overview(rows, href, today: date, heading: str, sub: str, first_col: str) -> str:
    """Table of item counts: official, then per day, shaded by volume.

    `rows` is [(slug, name, stories)]; `href(slug)` gives each row's link.
    """
    days = [today - timedelta(days=i) for i in range(3)]
    counts: dict[str, Counter] = {}
    has_earlier = False
    for slug, _, stories in rows:
        c = Counter()
        for s in stories:
            if s.lead.is_official:
                c["official"] += 1
                continue
            d = _item_day(s.lead)
            key = d if d in days else "earlier"
            has_earlier |= key == "earlier"
            c[key] += 1
        counts[slug] = c
    columns: list = days + (["earlier"] if has_earlier else [])
    peak = max((c[col] for c in counts.values() for col in columns), default=0) or 1

    def shade(n: int) -> str:
        """Five steps relative to the busiest cell in the table."""
        return "" if n == 0 else f"v{min(5, 1 + int(4 * n / peak))}"

    def label(col) -> str:
        """Full column label, plus a short form swapped in on phones."""
        full = "Earlier" if col == "earlier" else _short_day(col, today)
        short = {"Yesterday": "Yest.", "Earlier": "Earl."}.get(full, full)
        return (f'<span class="full">{full}</span><span class="short" aria-hidden="true">{short}</span>'
                if short != full else full)

    head = "".join(f'<th class="num" scope="col">{label(col)}</th>' for col in columns)
    body = []
    for slug, name, _ in rows:
        c, link = counts[slug], href(slug)
        cells = [f'<td class="official{" zero" if not c["official"] else ""}">'
                 f'<a href="{link}" tabindex="-1">{c["official"] or "–"}</a></td>']
        for col in columns:
            n = c[col]
            cells.append(f'<td class="{"zero" if not n else ""}"><a class="{shade(n) or "zero"}" href="{link}" '
                         f'tabindex="-1">{n if n else "–"}</a></td>')
        body.append(f'<tr><th scope="row"><a href="{link}">{esc(name)}</a></th>{"".join(cells)}</tr>')
    return (f'<section class="block"><div class="block-head"><h2>{esc(heading)}</h2><p>{sub}</p></div>'
            f'<table class="overview"><thead><tr><th scope="col">{esc(first_col)}</th>'
            f'<th class="num" scope="col"><span class="full">Official</span>'
            f'<span class="short" aria-hidden="true">Off.</span></th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>')


def _groups(slug: str, stories: list[Story], names: dict[str, str], today: date) -> str:
    """Official group first, then news grouped by day (stories arrive newest first).

    `slug` is the page's own department or tag; other departments an item is
    filed under are listed beneath it.
    """
    official = [s for s in stories if s.lead.is_official]
    news = [s for s in stories if not s.lead.is_official]

    def filed(s: Story) -> str:
        cross = [names[d] for d in s.lead.departments if d != slug and d in names]
        return f"Also filed under {esc(', '.join(cross))}" if cross else ""

    out = []
    if official:
        rows = "".join(_row(s, esc(_time_gutter(s.lead, today, in_day_group=False)), summary=True)
                       for s in official)
        out.append(f'<div class="group official"><h3>Official</h3><ol class="items">{rows}</ol></div>')

    by_day: list[tuple[str, list[Story]]] = []
    for s in news:
        d = _item_day(s.lead)
        label = _day_label(d, today) if d else "Undated"
        if by_day and by_day[-1][0] == label:
            by_day[-1][1].append(s)
        else:
            by_day.append((label, [s]))
    for label, group in by_day:
        rows = "".join(_row(s, esc(_time_gutter(s.lead, today, in_day_group=True)), summary=True, filed=filed(s))
                       for s in group)
        out.append(f'<div class="group"><h3>{esc(label)}</h3><ol class="items">{rows}</ol></div>')
    return "".join(out)


def _tally(stories: list[Story], today: date) -> str:
    """News items today, yesterday and the day before, in the outlet-count style."""
    days = [today - timedelta(days=i) for i in range(3)]
    c = Counter(_item_day(s.lead) for s in stories if not s.lead.is_official)
    cells = "".join(f'<div><dt>{_day_label(d, today).split()[0]}</dt><dd>{c[d]}</dd></div>' for d in days)
    return f'<dl class="tally" aria-label="News items per day">{cells}</dl>'


def _sidebar(sections, tag_sections, links: _Links, current: str = "") -> tuple[str, str]:
    """Desktop navigation and the equivalent phone dropdown.

    `current` is "dept:<slug>", "tag:<slug>" or "channels".
    """
    def entry(href: str, name: str, count: int | None, key: str) -> tuple[str, str]:
        attrs = ' aria-current="page"' if key == current else ""
        if count == 0:
            attrs += ' class="none"'
        n = f'<span class="n">{count}</span>' if count is not None else ""
        sel = " selected" if key == current else ""
        label = f"{esc(name)} ({count})" if count is not None else esc(name)
        return (f'<li><a href="{href}"{attrs}><span>{esc(name)}</span>{n}</a></li>',
                f'<option value="{href}"{sel}>{label}</option>')

    top = [entry(links.home, "Overview", None, "home")]
    if links.channels:
        top.append(entry(links.channels, "Your channels", None, "channels"))
    depts = [entry(links.dept(slug), name, len(st), f"dept:{slug}") for slug, name, st in sections]
    tags = [entry(links.tag(slug), name, len(st), f"tag:{slug}") for slug, name, st in tag_sections]

    nav = (f'<nav class="depts" aria-label="Departments and tags"><ul class="home">{"".join(e[0] for e in top)}</ul>'
           f'<h2 class="nav-head">Departments</h2><ul>{"".join(e[0] for e in depts)}</ul>')
    if tags:
        nav += f'<h2 class="nav-head">Tags</h2><ul>{"".join(e[0] for e in tags)}</ul>'
    nav += "</nav>"
    options = "".join(e[1] for e in top) + \
        f'<optgroup label="Departments">{"".join(e[1] for e in depts)}</optgroup>' + \
        (f'<optgroup label="Tags">{"".join(e[1] for e in tags)}</optgroup>' if tags else "")
    select = f'<select id="dept-select" class="dept-select" aria-label="Go to a page">{options}</select>'
    return nav, select


def _footer(config: Config, searched: bool = False) -> str:
    outlets = ", ".join(dict.fromkeys(o.name for o in config.outlets if o.enabled))  # unique, in order
    official = [label for on, label in ((config.official.govuk, "GOV.UK"),
                                        (config.official.written_statements, "UK Parliament written statements"))
                if on]
    sources = outlets + (f"; and official sources: {', '.join(official)}" if official else "")
    return (f'<footer>Times are UK time. News is matched to departments automatically by keyword; '
            f'official items are filed under the department that issued them. '
            + ('Tags also include results from a media search of UK outlets. ' if searched else '')
            + f'Sources: {esc(sources)}.</footer>')


def _page(title: str, mast: str, content: str, extra_head: str = "", extra_script: str = "") -> str:
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{_FONTS}<style>{_CSS}</style>{extra_head}</head>
<body id="top"><a class="skip" href="#main">Skip to content</a>
{mast}{content}
<script>{_JS}</script>{extra_script}
</body></html>"""


def _updated(now: datetime) -> str:
    return f"Updated {now:%H:%M}, {now:%A} {now.day} {now:%B}"


def _window(hours: int) -> str:
    return "three days" if hours == 72 else _plural(hours, "hour")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def tag_sections(articles: list[Article], tags: tuple[Tag, ...]) -> list[tuple[str, str, list[Story]]]:
    """[(slug, name, stories)] for each team tag, grouped and ordered like a department."""
    matcher = TagMatcher(tags)
    by_tag: dict[str, list[Article]] = {t.slug: [] for t in tags}
    for a in articles:
        for slug in matcher.tags_for(a):
            if slug in by_tag:
                by_tag[slug].append(a)
    return [(t.slug, t.name, group_stories(sorted(by_tag[t.slug], key=lambda a: _sort_key(a, ""))))
            for t in tags]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def render_html(articles: list[Article], config: Config, hours: int, only: str | None = None,
                site: bool = False, tags: tuple[Tag, ...] = ()) -> str:
    """Render the homepage (only=None) or a department page (only=<slug>).

    site=False gives a single-file digest: the homepage then lists every
    department in full, since there are no separate pages to link to.
    """
    names = {d.slug: d.name for d in config.departments}
    sections = build_sections(articles, config)
    tsections = tag_sections(articles, tags) if site else []
    now = _local(datetime.now(timezone.utc))
    today = now.date()
    links = _Links(site, depth=1 if only else 0)

    if only:
        slug, name, stories = next(s for s in sections if s[0] == only)
        n_official = sum(s.lead.is_official for s in stories)
        sub = f"{_plural(len(stories), 'item')} in the last {_window(hours)}"
        sub += f", {n_official} of them official." if n_official else "."
        groups = _groups(slug, stories, names, today) or \
            '<p class="empty">No coverage of this department in this period.</p>'
        nav, select = _sidebar(sections, tsections, links, f"dept:{slug}")
        content = (f'<div class="wrap frame">{nav}<main id="main">{select}'
                   f'<div class="dept-head"><div><h1 class="page-title">{esc(name)}</h1>'
                   f'<p class="page-sub">{sub}</p></div>{_tally(stories, today)}</div>'
                   f'{groups}<p id="no-match" class="no-match">No headlines match that filter.</p>'
                   f'{_footer(config, bool(tags))}</main></div>')
        return _page(f"{name} | {SITE_NAME}", _masthead(links, _updated(now), with_filter=True), content)

    total = sum(len(s[2]) for s in sections)
    parts = [f'<div class="intro"><h1 class="page-title">Policy news, {now:%A} {now.day} {now:%B}</h1>',
             f'<p class="page-sub">{_plural(total, "item")} from the last {_window(hours)}, '
             f'filed across {sum(1 for s in sections if s[2])} departments.</p></div>']
    tables = _overview(sections, links.dept, today, "By department",
                       "Items per day, darker where coverage is heaviest", "Department")
    if tsections:
        tables += _overview(tsections, links.tag, today, "Tags",
                            f'Topics across departments. <a href="{links.channels}">Combine them in your own '
                            f'channel</a>.', "Tag")
    parts.append(f'<div class="front">{_most_reported(sections, names, links) or _no_multi_outlet()}'
                 f'<div>{tables}</div></div>')
    if not site:
        for slug, name, stories in sections:
            if stories:
                parts.append(f'<section class="block" id="{slug}"><div class="block-head"><h2>{esc(name)}</h2>'
                             f'</div>{_groups(slug, stories, names, today)}</section>')
    parts.append('<p id="no-match" class="no-match">No headlines match that filter.</p>')
    parts.append(_footer(config))
    content = f'<div class="wrap single"><main id="main">{"".join(parts)}</main></div>'
    return _page(f"{SITE_NAME}: {SITE_TAGLINE}", _masthead(links, _updated(now), with_filter=not site), content)


def render_tag_page(articles: list[Article], config: Config, hours: int, tags: tuple[Tag, ...], slug: str) -> str:
    """A team tag's page, in the department page layout."""
    names = {d.slug: d.name for d in config.departments}
    sections = build_sections(articles, config)
    tsections = tag_sections(articles, tags)
    tag = next(t for t in tags if t.slug == slug)
    stories = next(st for s, _, st in tsections if s == slug)
    now = _local(datetime.now(timezone.utc))
    links = _Links(True, depth=1)

    searched = sum(1 for s in stories if s.lead.kind == SEARCH)
    sub = f"{_plural(len(stories), 'item')} in the last {_window(hours)}"
    sub += f", {searched} found by media search." if searched else "."
    terms = ", ".join(tag.terms)
    groups = _groups(slug, stories, names, now.date()) or \
        '<p class="empty">Nothing matched this tag in this period.</p>'
    nav, select = _sidebar(sections, tsections, links, f"tag:{slug}")
    content = (f'<div class="wrap frame">{nav}<main id="main">{select}'
               f'<div class="dept-head"><div><h1 class="page-title">{esc(tag.name)}</h1>'
               f'<p class="page-sub">{sub}</p><p class="terms">Searches for: {esc(terms)}</p></div>'
               f'{_tally(stories, now.date())}</div>'
               f'{groups}<p id="no-match" class="no-match">No headlines match that filter.</p>'
               f'{_footer(config, bool(tags))}</main></div>')
    return _page(f"{tag.name} | {SITE_NAME}", _masthead(links, _updated(now), with_filter=True), content)


def export_json(articles: list[Article], config: Config, hours: int, tags: tuple[Tag, ...],
                search_url: str = "") -> dict:
    """Everything collected in the window, as open data and for the Channels page.

    Compact keys keep the file small: u url, t title, s summary, o outlet,
    p published (ISO 8601), k kind, d departments, g team tags.
    """
    matcher = TagMatcher(tags)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    items = []
    for a in sorted(articles, key=lambda a: a.published or epoch, reverse=True):
        if not a.url.startswith(("http://", "https://")):
            continue
        items.append({"u": a.url, "t": a.title, "s": _truncate(a.summary), "o": a.outlet,
                      "p": a.published.isoformat() if a.published else None, "k": a.kind,
                      "d": sorted(a.departments), "g": matcher.tags_for(a)})
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hours": hours,
        "search_url": search_url,  # live search Worker for personal tags; "" = off
        "departments": [{"slug": d.slug, "name": d.name} for d in config.departments],
        "tags": [{"slug": t.slug, "name": t.name, "terms": list(t.terms)} for t in tags],
        "items": items,
    }


def render_channels_page(articles: list[Article], config: Config, hours: int, tags: tuple[Tag, ...],
                         data: dict) -> str:
    """The Channels page: a shell the browser app (static/channels.js) fills in.

    The data is embedded so the page works offline and from file://.
    """
    sections = build_sections(articles, config)
    tsections = tag_sections(articles, tags)
    now = _local(datetime.now(timezone.utc))
    links = _Links(True, depth=0)
    nav, select = _sidebar(sections, tsections, links, "channels")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    content = (f'<div class="wrap frame">{nav}<main id="main">{select}'
               f'<div id="channels-app" aria-live="polite"><p class="empty">Loading channels…</p></div>'
               f'<noscript><p class="empty">Channels need JavaScript, which is turned off in this browser. '
               f'Department and tag pages work without it.</p></noscript>'
               f'{_footer(config, bool(tags))}</main></div>')
    script = (f'<script id="channel-data" type="application/json">{payload}</script>'
              f'<script>{_static("channels.js")}</script>')
    return _page(f"Your channels | {SITE_NAME}", _masthead(links, _updated(now), with_filter=False), content,
                 extra_head=f"<style>{_static('channels.css')}</style>", extra_script=script)


def _static(name: str) -> str:
    return (Path(__file__).parent / "static" / name).read_text(encoding="utf-8")


def drop_duplicate_search_results(articles: list[Article]) -> list[Article]:
    """Remove media-search results that repeat a headline we already have from
    an outlet feed (search links often go via a redirect, so URLs differ).
    The kept feed article inherits the search result's tags."""
    def key(title: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    by_title = {key(a.title): a for a in articles if a.kind != SEARCH}
    kept = []
    for a in articles:
        twin = by_title.get(key(a.title)) if a.kind == SEARCH else None
        if twin is None:
            kept.append(a)
        else:
            twin.tags = sorted(set(twin.tags) | set(a.tags))
    return kept


def build_site(articles: list[Article], config: Config, hours: int, out_dir: Path,
               tags: tuple[Tag, ...] = (), search_url: str = "") -> list[Path]:
    """Write the static website to `out_dir`, ready for GitHub Pages or any web host.

        index.html                  overview: most reported, department and tag tables
        departments/<slug>.html     one page per department
        tags/<slug>.html            one page per team tag
        channels.html               build and share your own channels
        articles.json               everything collected in the window (open data)

    `articles` should include unfiled items (Store.recent(filed_only=False)) so
    tags and channels can search everything. Returns the paths written.
    """
    articles = drop_duplicate_search_results(articles)
    for sub in ("departments", "tags"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
        for old_page in (out_dir / sub).glob("*.html"):  # pages for removed tags or departments
            old_page.unlink()
    (out_dir / ".nojekyll").touch()  # serve files as-is on GitHub Pages

    def write(path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    data = export_json(articles, config, hours, tags, search_url)
    written = [write(out_dir / "index.html", render_html(articles, config, hours, site=True, tags=tags)),
               write(out_dir / "channels.html", render_channels_page(articles, config, hours, tags, data)),
               write(out_dir / "articles.json", json.dumps(data, ensure_ascii=False))]
    for dept in config.departments:
        written.append(write(out_dir / "departments" / f"{dept.slug}.html",
                             render_html(articles, config, hours, only=dept.slug, site=True, tags=tags)))
    for tag in tags:
        written.append(write(out_dir / "tags" / f"{tag.slug}.html",
                             render_tag_page(articles, config, hours, tags, tag.slug)))
    return written
