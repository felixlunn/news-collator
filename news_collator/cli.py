"""Command-line interface.

    python -m news_collator run                    # fetch, classify, store, write digest
    python -m news_collator fetch                  # fetch news + official sources, store
    python -m news_collator digest --dept moj      # render from what's stored
    python -m news_collator site                   # build the static website
    python -m news_collator explain "headline …"   # debug why text maps where it does
    python -m news_collator check-feeds            # test every feed URL
    python -m news_collator audit                  # CSV of every article's classification
    python -m news_collator departments            # list department slugs
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

DEFAULT_DB = Path("data/news.sqlite3")
DEFAULT_OUT = Path("output")
DEFAULT_HOURS = 72  # look-back window; covers a full weekend on Monday mornings


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace, config: Config) -> int:
    """Fetch news and official sources, classify news, and store. Returns exit code."""
    articles, errors = fetch_all(config.outlets)
    relevant = Classifier(config.departments, args.threshold).classify_all(articles)

    # Official items arrive already filed under their issuing department.
    official, official_errors = fetch_official(config, args.official_hours)
    errors |= official_errors

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        new = store.save(relevant + official)
        if args.prune_days:
            store.prune(args.prune_days)

    print(f"Fetched {len(articles)} news articles ({len(relevant)} matched a department) "
          f"and {len(official)} official items; {new} new.")
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
    with Store(args.db) as store:
        articles = store.recent(hours=args.hours)
    pages = build_site(articles, config, args.hours, args.out)
    print(f"Wrote {len(pages)} pages to {args.out}/ ({len(articles)} articles).")
    return 0


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    code = cmd_fetch(args, config)
    cmd_digest(args, config)
    return code


def cmd_explain(args: argparse.Namespace, config: Config) -> int:
    """Show scores and matched terms for a piece of text — for tuning keywords."""
    clf = Classifier(config.departments, args.threshold)
    art = Article(url="https://example.invalid", title=args.title, outlet="-", summary=args.summary)
    matches = clf.explain(art)
    if not matches:
        print("No department terms matched.")
    for m in matches:
        status = "EXCLUDED" if m.excluded else ("assigned" if m.score >= args.threshold else "below threshold")
        print(f"{m.slug:15} score {m.score:3}  [{status}]  {', '.join(m.terms)}")
    return 0


def cmd_check_feeds(args: argparse.Namespace, config: Config) -> int:
    """Hit every feed and report item counts or errors."""
    failures = 0
    for outlet in config.outlets:
        if not outlet.enabled:
            print(f"-- {outlet.name}: disabled")
            continue
        for url in outlet.feeds:
            _, items, err = fetch_feed(url, outlet.name)
            failures += bool(err)
            print(f"{'FAIL' if err else ' ok '}  {outlet.name:18} {len(items):4} items  {url}"
                  + (f"\n      {err}" if err else ""))
    return 1 if failures else 0


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    """Write a CSV of every fetched news article with its classification.

    For tuning keywords: sort by `assigned` to review what each department
    received, or filter for blanks to find relevant stories that were missed.
    Nothing is stored in the database.
    """
    clf = Classifier(config.departments, args.threshold)
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
                assigned = [m.slug for m in matches if m.score >= args.threshold and not m.excluded]
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

    digest = sub.add_parser("digest", help="render a digest from stored articles")
    run = sub.add_parser("run", help="fetch then digest")
    run.add_argument("--prune-days", type=int, default=30)
    run.add_argument("--official-hours", type=int, default=DEFAULT_HOURS)
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

    sub.add_parser("check-feeds", help="test every configured feed")
    sub.add_parser("departments", help="list department slugs")
    return p


COMMANDS = {
    "fetch": cmd_fetch, "digest": cmd_digest, "site": cmd_site, "run": cmd_run, "explain": cmd_explain,
    "check-feeds": cmd_check_feeds, "audit": cmd_audit, "departments": cmd_departments,
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
