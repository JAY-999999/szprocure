"""Batch ID: the single tracking key for the whole SKU Factory.

Format:  YYYYMMDD_STRATEGY_COUNT      e.g. 20260830_MCU_20

This replaces the old convention of encoding batch identity in script
filenames (`_add_20_mcu.py`, `_add_20_mcu_0830.py`), which was unqueryable
and unrecoverable.
"""
import os
import re
from datetime import datetime

from . import DEFAULT_BATCH_ROOT

BATCH_ID_RE = re.compile(r"^(?P<date>\d{8})_(?P<strategy>[A-Z0-9]+)_(?P<count>\d+)$")


class BatchIdError(ValueError):
    pass


def make_batch_id(strategy, count, when=None, exist_ok_check=True, root=None):
    """Build YYYYMMDD_STRATEGY_COUNT.

    strategy is upper-cased and must be [A-Z0-9]+ (e.g. MCU, POWER, PASSIVE).
    Refuses to hand out an id whose manifest already exists, so a batch can
    never silently overwrite an earlier one.
    """
    when = when or datetime.now()
    strat = str(strategy).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", strat):
        raise BatchIdError(f"strategy must be [A-Z0-9]+, got {strategy!r}")
    if int(count) < 0:
        raise BatchIdError("count must be >= 0")
    bid = f"{when:%Y%m%d}_{strat}_{int(count)}"
    if exist_ok_check:
        root = root or DEFAULT_BATCH_ROOT
        if os.path.exists(manifest_path(bid, root)):
            raise BatchIdError(f"batch id already exists: {bid} ({manifest_path(bid, root)})")
    return bid


def validate_batch_id(bid):
    m = BATCH_ID_RE.match(str(bid).strip())
    if not m:
        raise BatchIdError(
            f"invalid batch id {bid!r}; expected YYYYMMDD_STRATEGY_COUNT (e.g. 20260830_MCU_20)")
    return m.groupdict()


def parse_batch_id(bid):
    return validate_batch_id(bid)


def manifest_path(bid, root=None):
    validate_batch_id(bid)
    return os.path.join(root or DEFAULT_BATCH_ROOT, f"{bid}.json")


def batch_exists(bid, root=None):
    return os.path.exists(manifest_path(bid, root))
