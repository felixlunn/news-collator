"""Command-line interface.

    python -m news_collator run                    # fetch, classify, store, write digest
    python -m news_collator fetch                  # fetch news + official sources, store
    python -m news_collator digest --dept moj      # render from what's stored
    python -m news_collator site                   # build the static website
    python -m news_collator explain "headline …"   # debug why text maps where it does
    python -m news_collator check-feeds            # test every feed URL
    python -m news_collator audit                  # CSV of every article's classification
    python -m news_collator departments            # list department slugs
    python -m news_collator tags                   # list team tags and their searches
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .classify import DEFAULT_THRESHOLD, Classifier
from .config import DEFAULT_CONFIG_DIR, Config, UnknownDepartmentError, load_config
from .digest import render_markdown
from .web import build_site, render_html
from .fetch import fetch_all, fetch_feed
from .models import Article
from .official import fetch_official
from .store import Store
from .tags import load_tags, search_tags

DEFAULT_DB = Path("data/news.sqlite3")
DEFAULT_OUT = Path("output")
DEFAULT_HOURS = 72  # look-back window; covers a full weekend on Monday mornings


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace, config: Config) -> int:
    """Fetch news, official sources and tag searches; classify news; store.

    Every news article is stored, filed or not, so tags and channels can search
    all collected coverage. Returns a process exit code.
    """
    articles, errors = fetch_all(config.outlets)
    classifier = Classifier.from_config(config, args.threshold)
    relevant = classifier.classify_all(articles)

    # Official items arrive already filed under their issuing department.
    official, official_errors = fetch_official(config, args.official_hours)
    errors |= official_errors

    # Team tags with a media search find coverage beyond our own feeds.
    tags, settings = load_tags(args.config)
    found: list[Article] = []
    if not args.no_search:
        found, search_errors = search_tags(tags, settings, args.official_hours)
        errors |= search_errors

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        new = store.save(articles + official + found)
        if args.prune_days:
            store.prune(args.prune_days)
        # Drop search results belonging to tags that have been removed from the config.
        store.purge_tags({t.slug for t in tags})
        # Re-sort everything already stored under the current rules, so keyword
        # changes apply across the whole window, not just to newly fetched items.
        stored = store.news_articles()
        classifier.classify_all(stored)
        store.save(stored)

    print(f"Fetched {len(articles)} news articles ({len(relevant)} matched a department), "
          f"{len(official)} official items and {len(found)} tag search results; {new} new.")
    if errors:
        print(f"{len(errors)} feed(s) failed (run `check-feeds` for detail).", file=sys.stderr)
    # Only treat it as a failure if *nothing* came back.
    return 1 if errors and not articles else 0


def cmd_digest(args: argparse.Namespace, config: Config) -> int:
    """Render a digest from the database."""
    if args.dept:
        config.department(args.dept)  # validates the slug early
    with Store(args.db) as store:
        articles = store.recent(hours=args.hours, department=args.dept)

    render = render_markdown if args.format == "md" else render_html
    text = render(articles, config, args.hours, only=args.dept)

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"digest-{args.dept or 'all'}.{args.format}"
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {path} ({len(articles)} articles).")
    return 0


def cmd_site(args: argparse.Namespace, config: Config) -> int:
    """Build the multi-page static website from the database."""
    tags, settings = load_tags(args.config)
    with Store(args.db) as store:
        articles = store.recent(hours=args.hours, filed_only=False)
    pages = build_site(articles, config, args.hours, args.out, tags=tags, search_url=settings.live_search_url)
    print(f"Wrote {len(pages)} pages to {args.out}/ ({len(articles)} articles).")
    return 0


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    code = cmd_fetch(args, config)
    cmd_digest(args, config)
    return code


def cmd_explain(args: argparse.Namespace, config: Config) -> int:
    """Show scores and matched terms for a piece of text — for tuning keywords."""
    clf = Classifier.from_config(config, args.threshold)
    art = Article(url="https://example.invalid", title=args.title, outlet="-", summary=args.summary)
    if clf.globally_excluded(art):
        print("Headline matches a global exclude term (e.g. cartoon, podcast); never filed.")
        return 0
    if clf.abroad(art):
        print("Set abroad: names a foreign place and nothing places it in the UK, so it is filed under "
              f"{config.places.department} only.")
    matches = clf.explain(art)
    filed = clf.classify(art)
    if not matches:
        print("No department terms matched.")
    for m in matches:
        if m.excluded:
            status = "excluded"
        elif m.context_only:
            status = "context words only"
        elif m.slug in filed:
            status = "filed"
        elif m.score >= args.threshold:
            status = "too weak beside best match"
        else:
            status = "below threshold"
        print(f"{m.slug:15} score {m.score:3}  [{status}]  {', '.join(m.terms)}")
    return 0


def cmd_check_feeds(args: argparse.Namespace, config: Config) -> int:
    """Test feeds and report item counts, newest item, errors and staleness.

    With --url, test those addresses instead of the configured outlets, e.g. to
    vet a new outlet before adding it to outlets.toml.
    """
    if args.url:
        jobs = [(url, "candidate") for url in args.url]
    else:
        for outlet in config.outlets:
            if not outlet.enabled:
                print(f"-- {outlet.name}: disabled")
        jobs = [(url, o.name) for o in config.outlets if o.enabled for url in o.feeds]

    failures = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (url, items, err), (_, name) in zip(pool.map(lambda j: fetch_feed(*j), jobs), jobs):
            failures += bool(err)
            dated = [a.published for a in items if a.published]
            newest = f"newest {max(dated):%d %b %H:%M}" if dated else "no dates"
            print(f"{'FAIL' if err else ' ok '}  {name:18} {len(items):4} items  {newest:18}  {url}"
                  + (f"\n      {err}" if err else ""))
    return 1 if failures else 0


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    """Write a CSV of every fetched news article with its classification.

    For tuning keywords: sort by `assigned` to review what each department
    received, or filter for blanks to find relevant stories that were missed.
    Nothing is stored in the database.
    """
    clf = Classifier.from_config(config, args.threshold)
    jobs = [(url, o.name) for o in config.outlets if o.enabled for url in o.feeds]
    seen: set[str] = set()
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for feed, items, err in pool.map(lambda job: fetch_feed(*job), jobs):
            if err:
                print(f"FAIL {feed}: {err}", file=sys.stderr)
            for art in items:
                if art.id in seen:
                    continue
                seen.add(art.id)
                matches = clf.explain(art)
                assigned = list(clf.classify(art))
                rows.append({
                    "feed": feed, "outlet": art.outlet,
                    "published": art.published.isoformat() if art.published else "",
                    "title": art.title, "summary": art.summary[:400],
                    "assigned": " ".join(assigned),
                    "scores": "; ".join(f"{m.slug}={m.score}{' EXCLUDED' if m.excluded else ''} "
                                        f"[{', '.join(m.terms)}]" for m in matches[:4]),
                })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["title"])
        writer.writeheader()
        writer.writerows(rows)
    matched = sum(1 for r in rows if r["assigned"])
    print(f"Wrote {args.out}: {len(rows)} articles, {matched} assigned to a department.")
    return 0


def cmd_tags(args: argparse.Namespace, config: Config) -> int:
    """List team tags, their terms and the media search each runs."""
    from .tags import gdelt_query, google_news_query
    tags, settings = load_tags(args.config)
    if not tags:
        print("No team tags defined (config/tags.toml).")
    for t in tags:
        print(f"{t.slug:20} {t.name}\n{'':20} terms: {', '.join(t.terms)}")
        if t.search and settings.enabled:
            queries = {"gdelt": lambda: gdelt_query(t, settings.country),
                       "google_news": lambda: google_news_query(t, DEFAULT_HOURS)}
            for provider in settings.providers:
                print(f"{'':20} {provider}: {queries[provider]()}")
    return 0


def cmd_departments(args: argparse.Namespace, config: Config) -> int:
    for d in config.departments:
        print(f"{d.slug:15} {d.name}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="news_collator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_DIR, help="directory with the TOML files")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                   help=f"minimum score to assign a department (default {DEFAULT_THRESHOLD})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="fetch, classify and store articles")
    fetch.add_argument("--prune-days", type=int, default=30,
                       help="delete stored articles older than this (0 = keep forever)")
    fetch.add_argument("--official-hours", type=int, default=DEFAULT_HOURS,
                       help="how far back to request written statements (default %(default)s)")
    fetch.add_argument("--no-search", action="store_true", help="skip team tags' media searches")

    digest = sub.add_parser("digest", help="render a digest from stored articles")
    run = sub.add_parser("run", help="fetch then digest")
    run.add_argument("--prune-days", type=int, default=30)
    run.add_argument("--official-hours", type=int, default=DEFAULT_HOURS)
    run.add_argument("--no-search", action="store_true")
    for sp in (digest, run):
        sp.add_argument("--dept", help="only this department (see `departments`)")
        sp.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help=f"look-back window in hours (default {DEFAULT_HOURS})")
        sp.add_argument("--format", choices=("html", "md"), default="html")
        sp.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")

    site = sub.add_parser("site", help="build the static website (homepage + a page per department)")
    site.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                      help=f"look-back window in hours (default {DEFAULT_HOURS})")
    site.add_argument("--out", type=Path, default=Path("site"), help="output directory (default site/)")

    explain = sub.add_parser("explain", help="show how a headline would be classified")
    explain.add_argument("title")
    explain.add_argument("--summary", default="")

    audit = sub.add_parser("audit", help="write a CSV of every article and how it was classified")
    audit.add_argument("--out", type=Path, default=Path("output/audit.csv"))

    check = sub.add_parser("check-feeds", help="test every configured feed (or --url ones)")
    check.add_argument("--url", nargs="+", help="test these feed addresses instead of the configured ones")
    sub.add_parser("departments", help="list department slugs")
    sub.add_parser("tags", help="list team tags and their media searches")
    return p


COMMANDS = {
    "fetch": cmd_fetch, "digest": cmd_digest, "site": cmd_site, "run": cmd_run, "explain": cmd_explain,
    "check-feeds": cmd_check_feeds, "audit": cmd_audit, "departments": cmd_departments, "tags": cmd_tags,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        return COMMANDS[args.command](args, config)
    except UnknownDepartmentError as exc:  # bad --dept slug
        print(exc, file=sys.stderr)
        return 2
