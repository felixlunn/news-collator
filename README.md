# Policy News Collator

Collects articles from major UK news outlets and sorts them into research
departments that mirror UK government ministerial departments (Home Office,
Ministry of Justice, HM Treasury, DHSC, and so on), producing a quick-to-scan
digest for each.

- **No dependencies** — Python 3.11+ standard library only.
- **Transparent classification** — keyword rules you can read and edit, with an
  `explain` command showing exactly why a headline landed where it did.
- **One story, many outlets** — the same event reported by the BBC, Guardian and
  Sky appears once, with "Also covered by" links.
- **Cross-cutting stories** — an article can belong to several departments
  (e.g. a Budget story touching NHS and prisons appears under all three).
- **Official sources pinned** — GOV.UK announcements and written ministerial
  statements to Parliament are filed under the department that issued them and
  pinned to the top of that department's list.

## Quick start

```bash
python -m news_collator run                  # fetch everything, write output/digest-all.html
python -m news_collator run --dept moj       # Ministry of Justice only
python -m news_collator digest --format md   # Markdown, for email / briefing docs
python -m news_collator digest --hours 24    # narrower window (default 72 hours)
```

Articles are stored in `data/news.sqlite3`, so running `fetch` regularly (e.g.
hourly) builds up a history and nothing is lost when an item drops off a feed.

## Commands

| Command | What it does |
|---|---|
| `run` | `fetch` then `digest` |
| `fetch` | Download all feeds, classify, save to the database. Prunes items older than 30 days (`--prune-days`). |
| `digest` | Render HTML or Markdown from stored articles (`--dept`, `--hours`, `--format`, `--out`). |
| `site` | Build the static website: homepage plus one page per department (`--hours`, `--out`). |
| `explain "headline" --summary "…"` | Show every department's score and the terms that matched. Use this when tuning. |
| `check-feeds` | Test every feed URL and report item counts, errors and stale feeds. |
| `audit` | Write a CSV of every fetched article with its scores and matched terms, for tuning. The workflow also saves one each run as a downloadable artifact. |
| `departments` | List department slugs. |
| `tags` | List team tags and the media search each runs. |

Global options: `--config DIR`, `--db PATH`, `--threshold N`, `-v`.

## Configuration

Everything editable lives in `config/`:

- **`outlets.toml`** — outlets and their RSS/Atom feed URLs. Toggle with `enabled`.
  GOV.UK official announcements are included but off by default.
- **`departments.toml`** — one block per department (mirroring the current
  machinery of government) plus *Parliament & party politics*. Each has:
  - `strong` — names that almost certainly mean this department (4 points)
  - `keywords` — policy-area vocabulary (2 points)
  - `context` — ambiguous words that support a match but can't make one (1 point)
  - `exclude` — vetoes the department if present

  Headline matches count double. A story is filed when a department scores
  **4 or more** and has at least one strong term, keyword or name; it's filed
  under further departments only if they score at least half as well as the
  best. A `[global] exclude` list drops cartoons, podcasts, letters and
  obituaries. Terms are whole-word and case-insensitive; `prison*` matches any
  ending; ALL-CAPS acronyms and terms starting `=` (e.g. `=Budget`) are
  case-sensitive.
- **`tags.toml`** — team tags; see *Tags and channels* below.
- **`people.toml`** — ministers' names per department, scored as keywords.
  **Update it at every reshuffle**; a warning is logged once it is six months old.
- **`official.toml`** — switches GOV.UK announcements and written statements on
  or off, and maps each department to its GOV.UK organisation slug(s) and the
  name Parliament uses for it. Run with `-v` to log any written statements from
  bodies that aren't mapped (e.g. the Leader of the House), which are skipped.

## Website

The digests are published as a website, **Despatch**, on GitHub Pages, rebuilt around 06:00,
12:00 and 17:00 UTC every weekday by `.github/workflows/digest.yml`, showing
the last 72 hours (so Monday mornings cover the weekend):

- **Homepage** (`index.html`): the most reported stories across all outlets, and
  a table of each department's coverage per day, shaded by volume.
- **Department pages** (`departments/<slug>.html`): one per research team, for
  bookmarking, e.g. `departments/moj.html` or `departments/home-office.html`.

One-off setup: in the repo go to **Settings → Pages** and set **Source** to
**GitHub Actions**. To publish immediately, open the **Actions** tab and choose
**Publish digest site → Run workflow**.

To build the site locally: `python -m news_collator fetch && python -m news_collator site`
(writes to `site/`). The workflow keeps the article database between runs
(via the Actions cache, pruned after 14 days), so stories stay on the site for
the whole 72 hours even after they drop off an outlet's feed.

## Tags and channels

**Team tags** (`config/tags.toml`) are topics that cut across departments, such
as *Violence against women and girls* or *Iran*. Each tag is a name plus search
terms, using the same syntax as department keywords. On every rebuild a tag:

1. collects matching articles from the outlet feeds, and
2. runs a media search of UK outlets through [GDELT](https://www.gdeltproject.org/),
   an open index of world news, so it also finds coverage from outlets we don't
   follow directly. Set `search = false` on a tag to skip this.

Each team tag gets a page (`tags/<slug>.html`) and a row in the homepage's Tags
table. Run `python -m news_collator tags` to see the search each tag runs.

**Channels** (`channels.html`) let anyone build their own feed: any mix of
departments, team tags and personal tags they write themselves. Personal tags
search everything the site has collected. Channels are saved in the reader's
browser (there's no server or log-in); the *Copy share link* button encodes the
whole channel in a link, so colleagues can open it and save their own copy.

**Open data:** `articles.json` holds every item collected in the window with its
departments and tags.

## Tests

```bash
python -m unittest discover tests
```

Tests use fixture feeds in `tests/fixtures/` and need no network access.

## Project layout

```
config/              outlets and department term lists (TOML)
news_collator/
  models.py          Article type, URL normalisation for de-duplication
  config.py          config loading
  fetch.py           concurrent feed download + RSS/Atom parsing
  official.py        GOV.UK announcements and Parliament written statements
  tags.py            team tags and GDELT media search
  static/            Channels page script and styles
  classify.py        rule-based department scoring
  store.py           SQLite persistence
  digest.py          story grouping and ordering, Markdown digest
  web.py             website and HTML digest (design notes at the top)
  cli.py             command-line interface
tests/               unit tests and fixture feeds
```

## Notes on use

This tool stores headlines, short feed summaries and links only, and always
links back to the original article. Respect each outlet's terms of use; some
(e.g. the FT, Telegraph) are partly paywalled and feed content may be limited.
