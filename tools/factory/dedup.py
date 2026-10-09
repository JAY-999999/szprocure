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


def _ident(cand):
    """Normalize a candidate to (norm_mpn, brand) identity.

    Accepts:
      * str                  -> (norm_mpn, "")
      * (mpn, brand)         -> (norm_mpn, brand)
      * (mpn, brand, cid)    -> (norm_mpn, brand)  (cid carried separately)
    """
    if isinstance(cand, str):
        return ((cand or "").strip().upper(), "")
    if isinstance(cand, (tuple, list)):
        m = cand[0] if len(cand) > 0 else ""
        b = cand[1] if len(cand) > 1 else ""
        return ((str(m).strip().upper()), str(b).strip())
    return ((str(cand).strip().upper()), "")


def _ident_set(master):
    """Coerce a master identity set that may hold MPN strings OR (mpn, brand)."""
    out = set()
    if not master:
        return out
    sample = next(iter(master))
    if isinstance(sample, (tuple, list)):
        for item in master:
            m = item[0] if len(item) > 0 else ""
            b = item[1] if len(item) > 1 else ""
            out.add(((str(m).strip().upper()), str(b).strip()))
    else:
        for m in master:
            out.add(((str(m).strip().upper()), ""))
    return out


def guard(candidates, master_identities, mass_rate=None, min_count=None,
          skip_existing=True, master_cids=None):
    """Split candidates into new / duplicate and decide whether to STOP.

    Identity is (normalized MPN, canonical brand):
      * same MPN across different brands        -> DISTINCT SKUs (allowed)
      * same (MPN, brand) already in MASTER    -> AUTO_SKIP (DUPLICATE_SKIP)
      * same (MPN, brand) >1 inside the batch  -> BATCH_SELF_DUPLICATE (STOP)

    candidates        : iterable of str (legacy MPN) OR (mpn, brand[, cid]) tuples.
    master_identities : set of (mpn, brand) tuples already in MASTER. A legacy
                        set of plain MPN strings is also accepted (brand = "").
    master_cids       : optional set of already-deployed C#s. A candidate whose
                        C# matches is reported as DUPLICATE_SKIP even if its
                        (MPN, brand) is fresh -- retains the "already-deployed C#
                        filter" capability at the release layer.

    Per-SKU behaviour is unchanged: a duplicate is always an AUTO_SKIP. Only the
    BATCH-LEVEL MASS_DUPLICATE gate is rate+count based, and only it can stop
    the batch.
    """
    mass_rate = MASS_DUPLICATE_RATE if mass_rate is None else mass_rate
    min_count = MASS_DUPLICATE_MIN_COUNT if min_count is None else min_count
    master = _ident_set(master_identities)
    cids = {(c or "").strip() for c in (master_cids or [])}

    new, dups, self_dups, seen = [], [], [], set()
    for cand in candidates:
        ident = _ident(cand)
        cid = cand[2] if isinstance(cand, (tuple, list)) and len(cand) > 2 else None
        if ident in seen:
            self_dups.append(cand)
            continue
        seen.add(ident)
        if ident in master or (cid and cid in cids):
            dups.append(cand)
        else:
            new.append(cand)

    total = max(len(candidates), 1)
    rate = len(dups) / total

    res = DedupResult(new, dups, self_dups, rate)

    for cand in dups:
        m, b = _ident(cand)
        res.exceptions.append({"code": DUPLICATE_SKIP, "severity": AUTO_SKIP,
                               "mpn": m, "brand": b,
                               "message": "SKU (MPN+brand) already present in MASTER; skipped"})
    for cand in self_dups:
        m, b = _ident(cand)
        res.exceptions.append({"code": BATCH_SELF_DUPLICATE, "severity": STOP,
                               "mpn": m, "brand": b,
                               "message": "SKU (MPN+brand) appears more than once inside the batch"})

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
