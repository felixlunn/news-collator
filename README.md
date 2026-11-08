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

## Quick start

```bash
python -m news_collator run                  # fetch everything, write output/digest-all.html
python -m news_collator run --dept moj       # Ministry of Justice only
python -m news_collator digest --format md   # Markdown, for email / briefing docs
python -m news_collator digest --hours 72    # wider look-back window
```

Articles are stored in `data/news.sqlite3`, so running `fetch` regularly (e.g.
hourly) builds up a history and nothing is lost when an item drops off a feed.

## Commands

| Command | What it does |
|---|---|
| `run` | `fetch` then `digest` |
| `fetch` | Download all feeds, classify, save to the database. Prunes items older than 30 days (`--prune-days`). |
| `digest` | Render HTML or Markdown from stored articles (`--dept`, `--hours`, `--format`, `--out`). |
| `explain "headline" --summary "…"` | Show every department's score and the terms that matched. Use this when tuning. |
| `check-feeds` | Test every feed URL and report item counts or errors. |
| `departments` | List department slugs. |

Global options: `--config DIR`, `--db PATH`, `--threshold N`, `-v`.

## Configuration

Everything editable lives in `config/`:

- **`outlets.toml`** — outlets and their RSS/Atom feed URLs. Toggle with `enabled`.
  GOV.UK official announcements are included but off by default.
- **`departments.toml`** — one block per department with three term lists:
  - `strong` — names that almost certainly mean "this department" (3 points)
  - `keywords` — policy-area vocabulary (1 point)
  - `exclude` — vetoes the department if present (e.g. "tennis" for MoJ's "court")

  A match in the headline counts double. An article is assigned when it scores
  **3 or more** (`--threshold`). Terms are whole-word and case-insensitive;
  `prison*` matches any ending; ALL-CAPS acronyms like `SEND` or `AI` are
  case-sensitive so they don't catch ordinary words.

Ministers' names are intentionally not included as they change at reshuffles —
add them locally if useful.

## Running on a schedule

`.github/workflows/digest.yml` runs the collator every weekday morning on
GitHub Actions and uploads the digests as a downloadable artifact. Adjust the
cron line to taste, or run `python -m news_collator fetch` from a server cron
job instead.

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
  classify.py        rule-based department scoring
  store.py           SQLite persistence
  digest.py          story grouping, HTML and Markdown rendering
  cli.py             command-line interface
tests/               unit tests and fixture feeds
```

## Notes on use

This tool stores headlines, short feed summaries and links only, and always
links back to the original article. Respect each outlet's terms of use; some
(e.g. the FT, Telegraph) are partly paywalled and feed content may be limited.
