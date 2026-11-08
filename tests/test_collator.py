"""Offline tests: run with `python -m unittest discover tests`."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news_collator.classify import Classifier, compile_term
from news_collator.config import load_config
from news_collator.digest import group_stories, render_markdown
from news_collator.web import build_site, render_html
from news_collator.fetch import decompress, parse_date, parse_feed, strip_html
import json
import sqlite3

from news_collator.models import GOVUK, WRITTEN_STATEMENT, Article, normalise_url
from news_collator.official import _body_lookup, _dedupe_bicameral, parse_statements
from news_collator.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()


def load_fixture_articles() -> list[Article]:
    rss = parse_feed((FIXTURES / "sample_rss.xml").read_bytes(), "Outlet A")
    atom = parse_feed((FIXTURES / "sample_atom.xml").read_bytes(), "Outlet B")
    return rss + atom


class TestParsing(unittest.TestCase):
    def test_rss_and_atom(self):
        arts = load_fixture_articles()
        self.assertEqual(len(arts), 5)  # linkless RSS item dropped
        self.assertEqual(arts[0].summary, "The Home Office will expand returns agreements as asylum claims rise.")
        self.assertEqual(arts[3].url, "https://other.example.org/boats")

    def test_dates_normalised_to_utc(self):
        self.assertEqual(parse_date("2026-11-08T06:00:00+01:00"),
                         datetime(2026, 11, 8, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_date("Sun, 08 Nov 2026 09:00:00 GMT").hour, 9)
        self.assertIsNone(parse_date("not a date"))

    def test_compressed_feeds(self):
        import gzip, zlib
        raw = (FIXTURES / "sample_rss.xml").read_bytes()
        self.assertEqual(decompress(gzip.compress(raw)), raw)
        self.assertEqual(decompress(zlib.compress(raw)), raw)
        self.assertEqual(decompress(raw), raw)  # uncompressed passes through
        self.assertEqual(len(parse_feed(decompress(gzip.compress(raw)), "X")), 3)

    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello&nbsp;<b>world</b></p>"), "Hello world")


class TestUrls(unittest.TestCase):
    def test_tracking_params_removed(self):
        self.assertEqual(normalise_url("http://News.Example.com/a/?utm_source=x&id=3&at_medium=RSS#top"),
                         "https://news.example.com/a?id=3")


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.clf = Classifier(CONFIG.departments)

    def classify(self, title, summary=""):
        return self.clf.classify(Article(url="https://x", title=title, outlet="t", summary=summary))

    def test_wildcards_and_acronyms(self):
        self.assertTrue(compile_term("prison*").search("Prisoners released"))
        self.assertFalse(compile_term("nurse").search("nursery places"))
        self.assertTrue(compile_term("SEND").search("SEND reforms"))
        self.assertFalse(compile_term("SEND").search("send a letter"))  # acronym is case-sensitive
        self.assertTrue(compile_term("victims' commissioner").search("the victims’ commissioner said"))

    def test_fixture_assignments(self):
        arts = {a.url: self.clf.classify(a) for a in load_fixture_articles()}
        self.assertIn("home-office", arts["https://news.example.com/politics/boats-plan?at_medium=RSS&utm_source=feed"])
        self.assertIn("moj", arts["https://news.example.com/uk/prisons-capacity"])
        self.assertIn("dhsc", arts["https://other.example.org/nhs-strike"])
        self.assertEqual(arts["https://news.example.com/sport/derby"], {})

    def test_multi_department(self):
        result = self.classify("Chancellor's Budget squeezes NHS and prison spending",
                               "Probation services and hospital trusts face real-terms cuts.")
        self.assertTrue({"treasury", "dhsc", "moj"} <= result.keys())

    def test_exclude(self):
        self.assertNotIn("moj", self.classify("Judge rules on tennis court dispute", "tennis prison"))


class TestDigestAndStore(unittest.TestCase):
    def test_story_grouping_across_outlets(self):
        arts = load_fixture_articles()
        boats = [a for a in arts if "boats" in a.title.lower()]
        stories = group_stories(boats)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].outlets, ["Outlet A", "Outlet B"])

    def test_store_roundtrip_and_render(self):
        clf = Classifier(CONFIG.departments)
        arts = clf.classify_all(load_fixture_articles())
        # Make dates recent so they fall inside the look-back window.
        for a in arts:
            a.published = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / "t.db") as store:
            self.assertEqual(store.save(arts), len(arts))
            self.assertEqual(store.save(arts), 0)  # idempotent
            recent = store.recent(hours=1)
            self.assertEqual(len(recent), len(arts))
            only_moj = store.recent(hours=1, department="moj")
            self.assertTrue(all("moj" in a.departments for a in only_moj))

        html = render_html(arts, CONFIG, 24)
        self.assertIn("; also <a", html)  # cross-outlet grouping shown
        self.assertIn('id="home-office"', html)
        md = render_markdown(arts, CONFIG, 24, only="moj")
        self.assertIn("## Ministry of Justice", md)
        self.assertNotIn("## Home Office", md)

    def test_build_site(self):
        arts = Classifier(CONFIG.departments).classify_all(load_fixture_articles())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            pages = build_site(arts, CONFIG, 24, out)
            self.assertEqual(len(pages), 1 + len(CONFIG.departments))
            self.assertTrue((out / ".nojekyll").exists())
            index = (out / "index.html").read_text()
            self.assertIn('href="departments/moj.html"', index)
            moj = (out / "departments" / "moj.html").read_text()
            self.assertIn('href="../index.html"', moj)
            self.assertIn('href="moj.html" aria-current="page"', moj)
            self.assertIn("Prisons near capacity", moj)
            self.assertNotIn("small boats", moj)


class TestOfficial(unittest.TestCase):
    def setUp(self):
        payload = json.loads((FIXTURES / "written_statements.json").read_text())
        self.statements, self.unmatched = parse_statements(payload, _body_lookup(CONFIG.departments))

    def test_statements_filed_by_issuing_department(self):
        by_title = {a.title: a for a in _dedupe_bicameral(self.statements)}
        self.assertEqual(set(by_title["Sanctions Designations"].departments), {"fcdo"})  # '&' vs 'and'
        moj = by_title["Prison Capacity Update"]
        self.assertEqual(moj.kind, WRITTEN_STATEMENT)
        self.assertEqual(moj.summary, "Today I am announcing new measures.")
        self.assertEqual(moj.url, "https://questions-statements.parliament.uk/written-statements/detail/2026-11-05/HCWS101")
        self.assertEqual(self.unmatched, {"Leader of the House"})

    def test_bicameral_duplicates_keep_commons(self):
        kept = _dedupe_bicameral(self.statements)
        prison = [a for a in kept if a.title == "Prison Capacity Update"]
        self.assertEqual(len(prison), 1)
        self.assertEqual(prison[0].outlet, "Parliament (Commons)")

    def test_official_items_pinned_and_never_grouped(self):
        news = Classifier(CONFIG.departments).classify_all(load_fixture_articles())
        release = Article(url="https://www.gov.uk/government/news/prisons", outlet="GOV.UK", kind=GOVUK,
                          title="Prisons near capacity as early release scheme extended",  # same headline as news
                          published=datetime(2026, 11, 1, tzinfo=timezone.utc), departments={"moj": 100})
        md = render_markdown(news + [release], CONFIG, 24, only="moj")
        items = [l for l in md.splitlines() if l.startswith("- ")]
        self.assertTrue(items[0].startswith("- *Official, GOV.UK:*"))  # pinned despite being oldest
        self.assertEqual(len(items), 2)  # identical headline not merged into the release
        html = render_html(news + [release], CONFIG, 24, only="moj")
        self.assertIn('<div class="group official"><h3>Official</h3>', html)

    def test_statements_windowed_by_day(self):
        """A statement dated midnight on the window's first day is still included."""
        a = _dedupe_bicameral(self.statements)[0]
        start_of_day = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(hour=0, minute=0, second=0,
                                                                                microsecond=0)
        a.published = start_of_day
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / "t.db") as store:
            store.save([a])
            # Window starts after midnight today, but the statement's day is today.
            self.assertEqual(len(store.recent(hours=1)), 1)

    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,"
                         " summary TEXT NOT NULL DEFAULT '', outlet TEXT NOT NULL, published TEXT,"
                         " first_seen TEXT NOT NULL)")
            conn.commit(); conn.close()
            with Store(db) as store:
                a = self.statements[0]
                a.published = datetime.now(timezone.utc)
                store.save([a])
                self.assertEqual(store.recent(hours=1)[0].kind, WRITTEN_STATEMENT)


if __name__ == "__main__":
    unittest.main()
