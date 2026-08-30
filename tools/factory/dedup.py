"""Duplicate Guard.

A candidate MPN already present in MASTER is an AUTO_SKIP (recorded, batch
continues). A batch whose duplicate rate exceeds MASS_DUPLICATE_RATE (20%) is
aborted with MASS_DUPLICATE, because that signals the candidate pool or the
de-duplication input is broken rather than merely overlapping.

The guard also catches duplicates *inside* the candidate list itself, which
would otherwise silently produce duplicate rows in MASTER.
"""
from . import MASS_DUPLICATE_RATE, MASS_DUPLICATE_MIN_COUNT
from .gate import (DUPLICATE_SKIP, BATCH_SELF_DUPLICATE, MASS_DUPLICATE,
                   AUTO_SKIP, STOP)


class DedupResult:
    def __init__(self, new, duplicates, self_duplicates, rate):
        self.new = new                      # candidate MPNs safe to add
        self.duplicates = duplicates        # already in MASTER -> AUTO_SKIP
        self.self_duplicates = self_duplicates
        self.rate = rate
        self.stop = False
        self.exceptions = []

    def as_manifest_counts(self):
        return {"new_sku_count": len(self.new),
                "duplicate_count": len(self.duplicates)}


def norm_mpn(mpn):
    return (mpn or "").strip().upper()


def guard(candidates, master_mpns, mass_rate=None, min_count=None, skip_existing=True):
    """Split candidates into new / duplicate and decide whether to STOP.

    candidates   : iterable of MPN strings
    master_mpns  : set/list of MPNs already in MASTER
    mass_rate    : duplicate-rate threshold (default MASS_DUPLICATE_RATE = 0.20)
    min_count    : absolute floor for the MASS_DUPLICATE gate
                   (default MASS_DUPLICATE_MIN_COUNT = 5)
    skip_existing: when False, duplicates are still reported but the caller is
                   expected to abort (used by the 'abort on collision' mode of
                   the legacy scripts).

    Per-SKU behaviour is unchanged: a candidate already in MASTER is always an
    AUTO_SKIP (DUPLICATE_SKIP). Only the BATCH-LEVEL MASS_DUPLICATE gate is
    rate+count based, and only it can stop the batch.
    """
    mass_rate = MASS_DUPLICATE_RATE if mass_rate is None else mass_rate
    min_count = MASS_DUPLICATE_MIN_COUNT if min_count is None else min_count
    master = {norm_mpn(m) for m in master_mpns}

    new, dups, self_dups, seen = [], [], [], set()
    for m in candidates:
        nm = norm_mpn(m)
        if nm in seen:
            self_dups.append(m)
            continue
        seen.add(nm)
        if nm in master:
            dups.append(m)
        else:
            new.append(m)

    total = max(len(candidates), 1)
    rate = len(dups) / total

    res = DedupResult(new, dups, self_dups, rate)

    for m in dups:
        res.exceptions.append({"code": DUPLICATE_SKIP, "severity": AUTO_SKIP,
                               "mpn": m,
                               "message": "MPN already present in MASTER; skipped"})
    for m in self_dups:
        res.exceptions.append({"code": BATCH_SELF_DUPLICATE, "severity": STOP,
                               "mpn": m,
                               "message": "MPN appears more than once inside the batch"})

    if self_dups:
        res.stop = True
    elif len(dups) >= min_count and rate > mass_rate:
        # BOTH an absolute floor and the rate threshold must be met, so that a
        # tiny/trial batch is not aborted by one or two stray duplicates.
        res.stop = True
        res.exceptions.append({
            "code": MASS_DUPLICATE, "severity": STOP, "mpn": None,
            "message": (f"duplicate_count {len(dups)} >= {min_count} AND "
                        f"duplicate_rate {rate:.0%} > {mass_rate:.0%} "
                        f"({len(dups)}/{len(candidates)})")})

    return res


def load_master_mpns(path):
    """Read just the MPN column of a master CSV (cheap for large files)."""
    import csv
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m = (r.get("mpn") or "").strip()
            if m:
                out.append(m)
    return out
