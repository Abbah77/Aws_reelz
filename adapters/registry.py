"""
Adapter registry.

TO ADD A NEW SITE:
  1. Create adapters/your_site.py subclassing SiteAdapter (see
     vidsrc_to.py for a JS-gated example, vidlink_pro.py for a
     light-resolution example).
  2. Import it and add one line below.
That's the entire integration surface — nothing else in the codebase
needs to change.

Order here is the DEFAULT priority order used when a caller doesn't
specify a source_id (see api/routes.py) — first adapter that isn't
currently should_skip()-flagged and successfully resolves wins. This is
intentionally the same "sequential best first" idea as the Android app's
SourceRegistry.sorted(), just centralized server-side now.
"""
from __future__ import annotations

from core.base_adapter import SiteAdapter
from adapters.vidsrc_to import VidsrcToAdapter
from adapters.vidlink_pro import VidlinkProAdapter

ADAPTERS: list[SiteAdapter] = [
    VidlinkProAdapter(),
    VidsrcToAdapter(),
]

_BY_ID = {a.source_id: a for a in ADAPTERS}


def get_adapter(source_id: str) -> SiteAdapter:
    if source_id not in _BY_ID:
        raise KeyError(f"unknown source_id: {source_id!r}. Known: {list(_BY_ID)}")
    return _BY_ID[source_id]


def all_adapters() -> list[SiteAdapter]:
    return list(ADAPTERS)
