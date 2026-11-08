"""Rule-based classification of news articles into government departments.

Deliberately simple and transparent: every assignment can be explained by the
terms that matched, which matters when researchers need to trust (and tune)
what reaches them. Term lists live in config/departments.toml and
config/people.toml.

Scoring, per department (each distinct term counts once):

    tier       weight   in headline (x2)
    strong       4          8
    keyword      2          4      <- also ministers' names (people.toml)
    context      1          2

An article is filed under a department when that department scores at least
THRESHOLD (4): one keyword in the headline, one strong term anywhere, two
keywords in the summary, and so on. Context terms can add to a score but a
department needs at least one strong term, keyword or name to be filed:
"Parliament" and "election" together don't make a story about Westminster.

Further departments are added only if they also reach SECONDARY_RATIO of the
best department's score, so a story squarely about prisons isn't also filed
under Health because the summary mentions a hospital.

Headlines matching a global exclude (cartoons, podcasts, quizzes…) are never
filed. A department's own exclude terms veto that department only.

Stories set abroad (config/places.toml): if the headline names a place outside
the UK and nothing in the headline or summary places the story in the UK, it
is filed under the Foreign Office only, whatever else it matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config, Department, Places
from .models import Article

STRONG_WEIGHT = 4
KEYWORD_WEIGHT = 2
CONTEXT_WEIGHT = 1
TITLE_MULTIPLIER = 2
DEFAULT_THRESHOLD = 4
SECONDARY_RATIO = 0.5


def compile_term(term: str) -> re.Pattern[str]:
    """Turn a config term into a whole-word regex.

    - Leading '='  forces case-sensitive matching ("=Budget" ≠ "under budget").
    - Trailing '*' allows any word ending ("prison*" -> prisons, prisoner).
    - All-caps terms (acronyms like "SEND", "AI") are case-sensitive too.
    - Straight and curly apostrophes are equivalent; spaces match any whitespace.
    """
    case_sensitive = term.startswith("=")
    term = term.lstrip("=")
    wildcard = term.endswith("*")
    core = term.rstrip("*")
    pattern = re.escape(core).replace("'", "['’]").replace(r"\ ", r"\s+")
    pattern = rf"(?<!\w){pattern}{r'\w*' if wildcard else ''}(?!\w)"
    if core.isupper() and len(core.replace(" ", "")) >= 2:
        case_sensitive = True
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


@dataclass
class _CompiledDepartment:
    slug: str
    tiers: list[tuple[int, list[tuple[str, re.Pattern[str]]]]]  # (weight, [(term, regex)])
    exclude: list[re.Pattern[str]]


@dataclass
class Match:
    """Why an article was (or wasn't) assigned to a department — for tuning."""
    slug: str
    score: int
    terms: list[str] = field(default_factory=list)
    excluded: bool = False
    context_only: bool = False  # matched only on context terms; never filed


class Classifier:
    """Compiles term lists once, then scores many articles quickly."""

    def __init__(self, departments: tuple[Department, ...], threshold: int = DEFAULT_THRESHOLD,
                 global_exclude: tuple[str, ...] = (), places: Places = Places()):
        self.threshold = threshold
        self._global_exclude = [compile_term(t) for t in global_exclude]
        # "Happened abroad" rule. Domestic departments' strong terms are UK anchors too.
        self._abroad_dept = places.department
        self._foreign = [compile_term(t) for t in places.foreign] if places.department else []
        domestic_strong = [t for d in departments if d.slug != places.department for t in d.strong]
        self._uk = [compile_term(t) for t in (*places.uk, *domestic_strong)]

        def compiled(terms):
            return [(t, compile_term(t)) for t in terms]

        self._depts = [
            _CompiledDepartment(
                slug=d.slug,
                tiers=[(STRONG_WEIGHT, compiled(d.strong)),
                       (KEYWORD_WEIGHT, compiled(d.keywords + d.people)),
                       (CONTEXT_WEIGHT, compiled(d.context))],
                exclude=[compile_term(t) for t in d.exclude],
            )
            for d in departments
        ]

    @classmethod
    def from_config(cls, config: Config, threshold: int = DEFAULT_THRESHOLD) -> "Classifier":
        return cls(config.departments, threshold, config.global_exclude, config.places)

    def globally_excluded(self, article: Article) -> bool:
        """True for headlines that are never policy news (cartoons, podcasts…)."""
        return any(rx.search(article.title) for rx in self._global_exclude)

    def abroad(self, article: Article) -> bool:
        """True if the headline names a foreign place and nothing places the story in the UK."""
        if not self._foreign or not any(rx.search(article.title) for rx in self._foreign):
            return False
        return not any(rx.search(article.title) or rx.search(article.summary) for rx in self._uk)

    def explain(self, article: Article) -> list[Match]:
        """Score `article` against every department; matches with score > 0, best first."""
        title, body = article.title, article.summary
        results: list[Match] = []
        for dept in self._depts:
            score, terms, substantive = 0, [], False
            for weight, group in dept.tiers:
                for term, rx in group:
                    if rx.search(title):
                        score += weight * TITLE_MULTIPLIER
                    elif rx.search(body):
                        score += weight
                    else:
                        continue
                    terms.append(term)
                    substantive |= weight > CONTEXT_WEIGHT
            if score:
                excluded = any(rx.search(title) or rx.search(body) for rx in dept.exclude)
                results.append(Match(dept.slug, score, terms, excluded, context_only=not substantive))
        return sorted(results, key=lambda m: m.score, reverse=True)

    def classify(self, article: Article) -> dict[str, int]:
        """Return {department_slug: score} for the departments the article is filed under."""
        if self.globally_excluded(article):
            return {}
        if self.abroad(article):
            own = next((m.score for m in self.explain(article) if m.slug == self._abroad_dept), 0)
            return {self._abroad_dept: max(own, self.threshold)}
        eligible = [m for m in self.explain(article)
                    if m.score >= self.threshold and not m.excluded and not m.context_only]
        if not eligible:
            return {}
        floor = eligible[0].score * SECONDARY_RATIO
        return {m.slug: m.score for m in eligible if m.score >= floor}

    def classify_all(self, articles: list[Article]) -> list[Article]:
        """Classify in place and return only articles filed under at least one department."""
        relevant = []
        for art in articles:
            art.departments = self.classify(art)
            if art.departments:
                relevant.append(art)
        return relevant
