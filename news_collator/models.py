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


@dataclass
class Article:
    """A single news item pulled from a feed."""

    url: str
    title: str
    outlet: str
    summary: str = ""
    published: datetime | None = None  # always timezone-aware (UTC) when set
    # Filled in by the classifier: department slug -> relevance score.
    departments: dict[str, int] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable identifier derived from the normalised URL."""
        return hashlib.sha1(normalise_url(self.url).encode("utf-8")).hexdigest()
