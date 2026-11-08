"""Offline tests: run with `python -m unittest discover tests`."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from news_collator.classify import Classifier, compile_term
from news_collator.config import load_config
from news_collator.digest import group_stories, render_html, render_markdown
from news_collator.fetch import parse_date, parse_feed, strip_html
from news_collator.models import Article, normalise_url
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
        self.assertIn("Also covered by", html)
        self.assertIn('id="home-office"', html)
        md = render_markdown(arts, CONFIG, 24, only="moj")
        self.assertIn("## Ministry of Justice", md)
        self.assertNotIn("## Home Office", md)


if __name__ == "__main__":
    unittest.main()
