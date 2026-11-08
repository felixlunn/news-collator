"""Core data types shared across the package."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from. Stripping them
# lets us recognise the same article reached via different feeds.
_TRACKING_PARAMS = {"at_medium", "at_campaign", "at_custom1", "at_custom2", "at_custom3",
                    "at_custom4", "cmp", "CMP", "ito", "fbclid", "gclid", "ns_mchannel",
                    "ns_source", "ns_campaign", "ns_linkname", "ns_fee"}


def normalise_url(url: str) -> str:
    """Return a canonical form of `url` for de-duplication.

    Lower-cases the host, forces https, drops the fragment, utm_* and other
    tracking parameters, and any trailing slash on the path.
    """
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.startswith("utm_") and k not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, urlencode(query), ""))


# Article kinds. Official kinds are filed by the issuing department and pinned
# to the top of that department's list; search results only appear under tags.
NEWS = "news"
GOVUK = "govuk"                          # GOV.UK announcement (press release, speech, …)
WRITTEN_STATEMENT = "written_statement"  # written ministerial statement to Parliament
SEARCH = "search"                        # found by a team tag's media search (tags.py)
OFFICIAL_KINDS = frozenset({GOVUK, WRITTEN_STATEMENT})
KIND_LABELS = {GOVUK: "GOV.UK", WRITTEN_STATEMENT: "Written statement"}


@dataclass
class Article:
    """A single item: a news article or an official publication."""

    url: str
    title: str
    outlet: str
    summary: str = ""
    published: datetime | None = None  # always timezone-aware (UTC) when set
    kind: str = NEWS
    # Filled in by the classifier: department slug -> relevance score.
    departments: dict[str, int] = field(default_factory=dict)
    # Team tags whose media search found this article (tag matching by text is
    # done at render time; see tags.TagMatcher).
    tags: list[str] = field(default_factory=list)

    @property
    def is_official(self) -> bool:
        return self.kind in OFFICIAL_KINDS

    @property
    def id(self) -> str:
        """Stable identifier derived from the normalised URL."""
        return hashlib.sha1(normalise_url(self.url).encode("utf-8")).hexdigest()
