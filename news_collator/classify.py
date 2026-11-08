"""Rule-based classification of articles into government departments.

Deliberately simple and transparent: every assignment can be explained by the
terms that matched, which matters when researchers need to trust (and tune)
what lands in their inbox. See config/departments.toml for the term lists.

Scoring per department:
    strong term match   -> STRONG_WEIGHT points
    keyword match       -> 1 point
    match in headline   -> multiplied by TITLE_MULTIPLIER
Each distinct term counts once, however often it repeats, so a long summary
can't win just by repetition. An article is assigned to every department whose
score reaches the threshold, unless an `exclude` term for that department is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Department
from .models import Article

STRONG_WEIGHT = 3
TITLE_MULTIPLIER = 2
DEFAULT_THRESHOLD = 3


def compile_term(term: str) -> re.Pattern[str]:
    """Turn a config term into a whole-word regex.

    - A trailing '*' allows any word ending ("prison*" -> prisons, prisoner).
    - All-caps terms (acronyms like "SEND", "AI") match case-sensitively;
      everything else is case-insensitive.
    - Straight and curly apostrophes are treated as equivalent.
    """
    wildcard = term.endswith("*")
    core = term.rstrip("*")
    pattern = re.escape(core).replace("'", "['’]").replace(r"\ ", r"\s+")
    pattern = rf"(?<!\w){pattern}{r'\w*' if wildcard else ''}(?!\w)"
    is_acronym = core.isupper() and len(core.replace(" ", "")) >= 2
    return re.compile(pattern, 0 if is_acronym else re.IGNORECASE)


@dataclass
class _CompiledDepartment:
    slug: str
    strong: list[tuple[str, re.Pattern[str]]]
    keywords: list[tuple[str, re.Pattern[str]]]
    exclude: list[re.Pattern[str]]


@dataclass
class Match:
    """Why an article was (or wasn't) assigned to a department — handy for tuning."""
    slug: str
    score: int
    terms: list[str]
    excluded: bool = False


class Classifier:
    """Compiles department term lists once, then scores many articles quickly."""

    def __init__(self, departments: tuple[Department, ...], threshold: int = DEFAULT_THRESHOLD):
        self.threshold = threshold
        self._depts = [
            _CompiledDepartment(
                slug=d.slug,
                strong=[(t, compile_term(t)) for t in d.strong],
                keywords=[(t, compile_term(t)) for t in d.keywords],
                exclude=[compile_term(t) for t in d.exclude],
            )
            for d in departments
        ]

    def explain(self, article: Article) -> list[Match]:
        """Score `article` against every department, returning matches with any score > 0."""
        title, body = article.title, article.summary
        results: list[Match] = []

        for dept in self._depts:
            score, terms = 0, []
            for weight, group in ((STRONG_WEIGHT, dept.strong), (1, dept.keywords)):
                for term, rx in group:
                    if rx.search(title):
                        score += weight * TITLE_MULTIPLIER
                        terms.append(term)
                    elif rx.search(body):
                        score += weight
                        terms.append(term)
            if score:
                excluded = any(rx.search(title) or rx.search(body) for rx in dept.exclude)
                results.append(Match(dept.slug, score, terms, excluded))

        return sorted(results, key=lambda m: m.score, reverse=True)

    def classify(self, article: Article) -> dict[str, int]:
        """Return {department_slug: score} for departments that pass the threshold."""
        return {m.slug: m.score for m in self.explain(article)
                if m.score >= self.threshold and not m.excluded}

    def classify_all(self, articles: list[Article]) -> list[Article]:
        """Classify in place and return only articles assigned to at least one department."""
        relevant = []
        for art in articles:
            art.departments = self.classify(art)
            if art.departments:
                relevant.append(art)
        return relevant
