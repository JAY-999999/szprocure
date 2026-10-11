"""
hub_v24_guard — Components V2.4 minimal anti-rollback guard (2026-10-11).

WHY
    components/index.html is a HAND-DESIGNED page (V2.4, commits 440c9f73 +
    95de9119). Data and automation flows may ONLY rewrite the region BETWEEN
    the HUB-INJECT anchors (SKU counts, categories, links). Anything that would
    write the WHOLE page — a template rebuild, a legacy generator, a backup /
    quarantine restore, a whole-tree copy — must STOP and ask a human.

SCOPE (deliberately tiny)
    * No SHA / fingerprint database.
    * No change to any generation or classification logic.
    * Call sites opt in with ONE line each; everything else keeps working.

USAGE
    Allowed path (data injection into the existing shell):

        from hub_v24_guard import assert_anchor_injection
        assert_anchor_injection(hub_path)          # raises if anchors are gone

    Blocked path (whole page / whole tree):

        from hub_v24_guard import refuse_full_page_write, refuse_live_hub_tree_overwrite
        refuse_full_page_write(path)               # before open(path, "w")
        refuse_live_hub_tree_overwrite(out_root)   # before rmtree()/copytree()

    On refusal it raises HubProtectedError with:
        "Components V2.4 protected. Manual approval required."
"""
from __future__ import annotations

import os

# The exact string any blocking path must surface. Do not paraphrase it.
MESSAGE = "Components V2.4 protected. Manual approval required."

_HUB_DIR = "components"
_HUB_FILE = "index.html"

# V2.4 injection contract: each anchor must exist EXACTLY once. This is the same
# contract _replace_hub_anchor() already relies on — nothing new is invented here.
_ANCHORS = (
    "<!-- HUB-INJECT:NAV-START -->",
    "<!-- HUB-INJECT:NAV-END -->",
    "<!-- HUB-INJECT:SECTIONS-START -->",
    "<!-- HUB-INJECT:SECTIONS-END -->",
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_HUB = os.path.join(REPO, _HUB_DIR, _HUB_FILE)


class HubProtectedError(RuntimeError):
    """Raised when an operation would overwrite the V2.4 components hub page."""


def _real(path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def is_hub_page(path) -> bool:
    """True only for <dir>/components/index.html — the protected V2.4 page."""
    p = os.path.abspath(str(path))
    return (os.path.basename(p) == _HUB_FILE
            and os.path.basename(os.path.dirname(p)) == _HUB_DIR)


def hub_anchors_ok(path):
    """Return (ok, reason). ok=True when every HUB-INJECT anchor exists once."""
    if not os.path.isfile(path):
        return False, "file does not exist"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:  # pragma: no cover
        return False, "unreadable: %s" % e
    bad = [m for m in _ANCHORS if src.count(m) != 1]
    if bad:
        return False, "HUB-INJECT anchor missing/duplicated: " + ", ".join(bad)
    return True, ""


def assert_anchor_injection(hub_path) -> None:
    """ALLOW path. Caller rewrites ONLY the anchor regions (counts / categories /
    links). If the anchors are gone the file is no longer the V2.4 shell, so a
    'partial injection' would in fact be a silent whole-page rebuild -> refuse."""
    if not is_hub_page(hub_path):
        return
    ok, why = hub_anchors_ok(hub_path)
    if not ok:
        raise HubProtectedError("%s (%s)" % (MESSAGE, why))


def refuse_full_page_write(path) -> None:
    """BLOCK path. Call immediately before writing the WHOLE hub file."""
    if is_hub_page(path):
        raise HubProtectedError(
            "%s Refusing whole-page write to components/index.html "
            "(only HUB-INJECT anchor injection is allowed)." % MESSAGE)


def is_live_hub(path) -> bool:
    """True when `path` is this repository's LIVE V2.4 hub page."""
    return is_hub_page(path) and _real(path) == _real(LIVE_HUB)


def _is_ancestor(maybe_parent, child) -> bool:
    a, b = _real(maybe_parent), _real(child)
    if a == b:
        return False
    try:
        return os.path.commonpath([a, b]) == a
    except ValueError:  # pragma: no cover  (different drives on Windows)
        return False


def refuse_live_hub_tree_overwrite(out_root) -> None:
    """BLOCK path. Call BEFORE rmtree()/copytree() when out_root could resolve to
    the live site tree — a whole-tree overwrite would silently replace the
    hand-designed components/index.html with whatever base snapshot is copied."""
    if _is_ancestor(out_root, REPO):
        raise HubProtectedError(
            "%s Refusing whole-tree overwrite: out_root is an ancestor of the "
            "live site (%s)." % (MESSAGE, REPO))
    if is_live_hub(os.path.join(out_root, _HUB_DIR, _HUB_FILE)):
        raise HubProtectedError(
            "%s Refusing whole-tree overwrite that targets the live "
            "components/index.html (out_root resolves to the live site root)."
            % MESSAGE)
