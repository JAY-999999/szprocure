"""Product Data — intake, clean, normalise, de-duplicate (Phase P1-A).

This module is the Factory's product-data line. It **never writes to
production MASTER**; it produces a local candidate pool that a later release
step may append. Production MASTER is read (read-only) only to seed the
duplicate guard. Grandfathering applies: history is never rewritten.

Reused proven code
------------------
extract() / clean_core() / subcat() / build_row() are ported verbatim from the
validated 2026-08-30 batch script (`_add_20_mcu_0830.py`), including its three
hard-won fixes:
  * TI C2000 reports ``CPU内核`` as the Chinese string "其他" -> normalised to
    "C28x" (otherwise CJK leaks into attributes_json).
  * Any non-ASCII attribute value is dropped rather than written (CJK guard).
  * MPNs containing "/" are mapped to an R2-safe asset key for disk writes.

Synthetic-data guards mirror the frozen gen_parts.py definitions; a sandbox
test asserts the two copies stay identical.
"""
import csv
import json
import os
import re
from datetime import datetime

from . import MASTER_COLS, REQUIRED_FIELDS
from . import dedup, gate, pool, category
from .category import UNKNOWN_CATEGORY

# RAW attribute keys (source CSV is Chinese-keyed)
ATTR_CORE = "CPU内核"
ATTR_BITS = "CPU位数"
ATTR_FREQ = "CPU最大主频"

# --- mirrors of the frozen gen_parts.py guards (kept in sync by selftest) ---
SYNTHETIC_MPN_PATTERNS = [
    re.compile(r'^(MCU|MOS|RES|CAP|IND|DIO|CON|XTAL|MEM|WIFI|MOD|REG|AMP|OP|LED|PWR|IC)\d{6}', re.I),
    re.compile(r'100000\d{3}'),
    re.compile(r'^\d{6,}$'),
    re.compile(r'PLACEHOLDER', re.I),
    re.compile(r'XXX$', re.I),
    re.compile(r'_(TEST|SAMPLE|MOCK)$', re.I),
]
FAKE_BRAND_TOKENS = re.compile(
    r'(Acme|Nova|Placeholder|Synthetic|Mock|Fake|TestCorp|DemoSemi|Injected)', re.I)

SOURCE_KINDS = ("lcsc_api_csv",)

DEFAULT_RAW_SOURCE = (r"C:\Users\Administrator.SC-202105071542\Desktop"
                      r"\szprocure-site\data\raw\lcsc_api_FULL_20260827.csv")
DEFAULT_MFR_MAP = (r"C:\Users\Administrator.SC-202105071542\Desktop"
                   r"\szprocure-site\data\production\mfr_canonical.csv")

# pool-only fields (never written to MASTER)
F_DATASHEET_SRC = "_source_datasheet_url"
F_ASSET_KEY = "_asset_key"
F_SPEC_KEYS = "_spec_key_count"
F_NEEDS_REVIEW = "_needs_review"
F_DETECT = "_detect_signals"


class ProductDataError(Exception):
    pass


# =====================================================================
# proven helpers (ported verbatim from the validated batch script)
# =====================================================================
def num(s):
    m = re.search(r"(\d+(?:\.\d+)?)", (s or "").strip())
    return int(float(m.group(1))) if m else None


def clean_core(core, mpn):
    """Drop or repair non-ASCII (Chinese) core strings. TI C2000 -> C28x."""
    c = (core or "").strip()
    if not c:
        return ""
    if c.isascii():
        return c
    return "C28x" if mpn.upper().startswith("TMS320") else ""


def subcat(core, bits, mpn=""):
    c = (core or "").lower()
    pairs = [("risc-v", "32-bit RISC-V MCU"), ("cortex-m7", "32-bit ARM Cortex-M7 MCU"),
             ("cortex-m4", "32-bit ARM Cortex-M4 MCU"), ("cortex-m3", "32-bit ARM Cortex-M3 MCU"),
             ("cortex-m0", "32-bit ARM Cortex-M0+ MCU"), ("cortex-m23", "32-bit ARM Cortex-M23 MCU"),
             ("cortex-m33", "32-bit ARM Cortex-M33 MCU")]
    for k, v in pairs:
        if k in c:
            return v
    if "avr" in c:
        return f"{bits or 8}-bit AVR MCU"
    if "stm8" in c:
        return "8-bit STM8 MCU"
    if "msp430" in c:
        return "16-bit MSP430 MCU"
    if "c28x" in c or "c2000" in c:
        return "32-bit C28x DSC"
    if "pic" in c:
        return f"{bits or 8}-bit PIC MCU"
    if "8051" in c:
        return "8-bit 8051 MCU"
    if mpn.upper().startswith("TMS320"):
        return "32-bit C28x DSC"
    if "arm926" in c:
        return "32-bit ARM926EJ-S MPU"
    if "arm7tdmi" in c:
        return "32-bit ARM7TDMI MCU"
    return f"{bits}-bit MCU" if bits else "Microcontroller"


def extract(record, mpn):
    """Pull structured specs out of one RAW record."""
    try:
        p = json.loads(record.get("attributes_json") or "{}")
    except Exception:
        p = {}
    core = clean_core((p.get(ATTR_CORE) or "").strip(), mpn)
    bits = num(p.get(ATTR_BITS))
    freq = num(p.get(ATTR_FREQ))
    desc = (record.get("description") or "").strip()
    fm = re.search(r"(\d+(?:\.\d+)?)\s*[kK][bB]\s*Flash", desc, re.I)
    flash = int(float(fm.group(1))) if fm else None
    rm = re.search(r"(\d+)\s*\+?\s*\d*\s*[kK][bB]\s*(?:SRAM|RAM)", desc, re.I)
    ram = int(rm.group(1)) if rm else None
    io_m = re.search(r"(\d+)\s*I/O", desc)
    io = num(io_m.group(1)) if io_m else None
    pk = re.search(r"(LQFP|QFP|QFN|TQFP|TFBGA|UQFN|VQFN|WLCSP|LGA|BGA|SOIC|SSOP|TSOP|SOP|MSOP|DFN|TSSOP)[-\s]?(\d+)", desc, re.I)
    package = (pk.group(1).upper() + "-" + pk.group(2)) if pk else None
    vm = (re.search(r"operating voltage\s*(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s*V", desc, re.I)
          or re.search(r"(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s*V", desc))
    vmin = vmax = None
    if vm:
        vmin, vmax = float(vm.group(1)), float(vm.group(2))
    voltage = round((vmin + vmax) / 2, 2) if (vmin and vmax) else None
    return dict(core=core, bits=bits, freq=freq, flash=flash, ram=ram, io=io,
                package=package, vmin=vmin, vmax=vmax, voltage=voltage, desc=desc)


def asset_key(mpn):
    """R2 / filesystem safe key. MPNs containing '/' break Windows paths."""
    return re.sub(r'[^a-z0-9._-]', '-', (mpn or "").lower())


def load_brand_map(path=None):
    path = path or DEFAULT_MFR_MAP
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\t" in line:
                a, b = line.rstrip("\n").split("\t", 1)
                out[a.strip().lower()] = b.strip()
    return out


# =====================================================================
# row construction
# =====================================================================
def build_mcu_fields(record, mpn, brand):
    """MCU category fields — verbatim from the validated 540-row logic.

    Kept byte-identical to the pre-P1-E build_row MCU branch so the 540
    production MCUs behave exactly as before. Called by the MCU adapter.
    """
    e = extract(record, mpn)
    aj = {}
    if e["core"]:
        aj["core"] = e["core"]
    if e["freq"]:
        aj["frequency_hz"] = e["freq"] * 1_000_000
    if e["flash"]:
        aj["flash_bytes"] = e["flash"] * 1024
    if e["ram"]:
        aj["ram_bytes"] = e["ram"] * 1024
    if e["io"] is not None:
        aj["io_count"] = e["io"]
    if e["voltage"] is not None:
        aj["voltage_v"] = e["voltage"]
    if e["package"]:
        aj["package"] = e["package"]
    aj = {k: v for k, v in aj.items()
          if not (isinstance(v, str) and any(ord(ch) > 127 for ch in v))}

    parts = [f"{brand} {mpn}"]
    if e["core"]:
        parts.append(e["core"])
    if e["freq"]:
        parts.append(f"{e['freq']} MHz")
    if e["flash"]:
        parts.append(f"{e['flash']} KB Flash")
    if e["ram"]:
        parts.append(f"{e['ram']} KB SRAM")
    if e["io"] is not None:
        parts.append(f"{e['io']} I/O")
    if e["package"]:
        parts.append(e["package"])
    if e["vmin"] and e["vmax"]:
        parts.append(f"operating voltage {e['vmin']}-{e['vmax']} V")
    description = " - ".join(parts) + "."
    if len(parts) <= 1:
        description = e["desc"] or f"{brand} {mpn}"
    description = "".join(ch if ord(ch) < 128 else " " for ch in description).strip()

    sub = subcat(e["core"], e["bits"], mpn)
    kw = f"{mpn}; {e['core']}; {e['bits'] or ''}-bit MCU; microcontroller".replace(" ;", ";").strip("; ")
    if e["package"]:
        faq = f"Q: What package does {mpn} use?A: {mpn} is supplied in a {e['package']} surface-mount package."
    elif e["core"]:
        faq = f"Q: What core does {mpn} use?A: {mpn} is based on a {e['core']} core."
    else:
        faq = ""
    return {
        "category": "Microcontroller", "subcategory": sub,
        "description": description,
        "applications": ("Embedded control; IoT devices; Industrial automation; "
                         "Consumer electronics; Motor control"),
        "keywords": kw, "attributes_json": json.dumps(aj, ensure_ascii=False),
        "faq": faq,
    }


def build_row(record, mpn, brand, mfr_map=None):
    """Build one MASTER-shaped row from a RAW record.

    Category-aware since P1-E: delegates the 7 category-shaped fields to
    ``category.build_category_row`` (which runs the detection pipeline and the
    right per-family adapter). Retains the CJK guard, asset key and pool-only
    fields. Returns (row, meta) where ``meta`` is category detection metadata
    (carries it instead of the old extract result).
    """
    fields, meta = category.build_category_row(record, mpn, brand)
    row = {
        "mpn": mpn, "clean_mpn": "", "manufacturer": brand, "brand": brand,
        "url_slug": "",
        "category": fields["category"],
        "subcategory": fields["subcategory"],
        "description": fields["description"],
        "applications": fields["applications"],
        "keywords": fields["keywords"],
        "attributes_json": fields["attributes_json"],
        "availability": "active", "alternative_parts": "", "datasheet_url": "",
        "faq": fields["faq"], "image": "", "source": "", "source_url": "LCSC",
        "supplier_reference": (record.get("supplier_sku") or "").strip(),
    }
    # final CJK guard on attribute values (defence in depth)
    aj = json.loads(fields["attributes_json"] or "{}")
    aj = {k: v for k, v in aj.items()
          if not (isinstance(v, str) and any(ord(ch) > 127 for ch in v))}
    row["attributes_json"] = json.dumps(aj, ensure_ascii=False)
    row[F_DATASHEET_SRC] = (record.get("source_datasheet_url") or "").strip()
    row[F_ASSET_KEY] = asset_key(mpn)
    row[F_SPEC_KEYS] = len(aj)
    row[F_NEEDS_REVIEW] = bool(meta.get("needs_review", False))
    row[F_DETECT] = json.dumps(meta.get("signals", {}), ensure_ascii=False)
    return row, meta


# =====================================================================
# qualification
# =====================================================================
def looks_synthetic(mpn, brand):
    for pat in SYNTHETIC_MPN_PATTERNS:
        if pat.search(mpn or ""):
            return f"synthetic MPN pattern '{pat.pattern}'"
    if FAKE_BRAND_TOKENS.search(brand or ""):
        return f"synthetic brand '{brand}'"
    return None


def has_cjk(row):
    blob = "".join(str(row.get(f) or "") for f in
                   ("description", "subcategory", "attributes_json", "keywords",
                    "manufacturer", "brand"))
    return any(ord(ch) > 127 for ch in blob)


def qualify(row, mfr_map=None):
    """Return (verdict, code, message).

    verdict: 'ok' | 'reject' | 'warn'
    Rejected rows never reach the candidate pool.
    """
    mpn, brand = row.get("mpn", ""), row.get("brand", "")
    syn = looks_synthetic(mpn, brand)
    if syn:
        return ("reject", gate.SYNTHETIC_MPN, syn)
    for f in REQUIRED_FIELDS:
        if not (row.get(f) or "").strip():
            return ("reject", gate.SPEC_THIN, f"missing required field '{f}'")
    if has_cjk(row):
        # A leak here means the normaliser is broken -> data pollution.
        return ("reject", gate.CJK_LEAK, "non-ASCII survived normalisation")
    # P1-E: unmapped category -> review, never auto-release.
    if row.get("category") == UNKNOWN_CATEGORY or row.get(F_NEEDS_REVIEW):
        return ("warn", gate.UNMAPPED_CATEGORY,
                f"category '{row.get('category')}' not mapped to an adapter; "
                f"held for review")
    # P1-E: per-adapter minimum spec threshold (replaces the global SPEC_THIN<2).
    cat_name = row.get("category", "")
    adapter = category.REGISTRY.get(cat_name)
    min_specs = adapter.min_specs if adapter else 2
    if (row.get(F_SPEC_KEYS) or 0) < min_specs:
        return ("warn", gate.SPEC_THIN,
                f"only {row.get(F_SPEC_KEYS) or 0} structured specs "
                f"(min {min_specs}) for {cat_name}")
    if mfr_map is not None:
        raw_brand = (row.get("manufacturer") or "").strip()
        if raw_brand.lower() not in mfr_map:
            return ("warn", gate.BRAND_UNMAPPED,
                    f"brand '{raw_brand}' not in mfr_canonical.csv")
    return ("ok", None, "")


# =====================================================================
# INTAKE — large-batch, streaming, never edits the source
# =====================================================================
class IntakeResult:
    def __init__(self):
        self.batch_id = None
        self.source_kind = None
        self.source_path = None
        self.input_count = 0        # matched the filter in the source
        self.written = 0            # records actually persisted to the raw pool
        self.skipped_no_datasheet = 0
        self.self_duplicate_count = 0   # same MPN appears twice in the SOURCE
        self.mpns = []
        self.exceptions = []
        self.stop = False


def intake(batch_id, source_kind="lcsc_api_csv", source_path=None,
           selector=None, category=None, limit=None, require_datasheet=False,
           exclude_mpns=None, mfr_csv=None, root=None, manifest=None):
    """Stream matching RAW records into the local raw pool.

    Designed for 1,000 / 5,000 / 10,000-SKU harvests: records are appended
    one at a time with fsync, so a crash keeps everything already read.
    The source CSV is opened read-only and never modified.
    """
    if source_kind not in SOURCE_KINDS:
        raise ProductDataError(f"unsupported source_kind {source_kind!r}")
    source_path = source_path or DEFAULT_RAW_SOURCE
    if not os.path.exists(source_path):
        raise ProductDataError(f"source not found: {source_path}")

    res = IntakeResult()
    res.batch_id = batch_id
    res.source_kind = source_kind
    res.source_path = source_path

    sel = {m.strip().upper() for m in selector} if selector else None
    excl = {m.strip().upper() for m in (exclude_mpns or set())}
    cat = (category or "").strip().lower() or None

    pool.ensure(root)
    path = pool.raw_path(batch_id, root)
    if os.path.exists(path):
        os.unlink(path)          # a fresh intake replaces the previous snapshot

    def records():
        with open(source_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                mpn = (r.get("mpn") or "").strip()
                if not mpn:
                    continue
                if sel is not None and mpn.upper() not in sel:
                    continue
                if excl and mpn.upper() in excl:
                    continue
                if cat and (r.get("category") or "").strip().lower() != cat:
                    continue
                if require_datasheet and not (r.get("source_datasheet_url") or "").strip():
                    res.skipped_no_datasheet += 1
                    continue
                yield {
                    "mpn": mpn,
                    "manufacturer_raw": (r.get("manufacturer_raw") or "").strip(),
                    "catalogName": (r.get("catalogName") or "").strip(),
                    "category": (r.get("category") or "").strip(),
                    "description": r.get("description") or "",
                    "attributes_json": r.get("attributes_json") or "",
                    "source_datasheet_url": (r.get("source_datasheet_url") or "").strip(),
                    "supplier_sku": (r.get("supplier_sku") or "").strip(),
                }

    # collect the MPN list while streaming (no second pass over a 10k file).
    # The source legitimately repeats MPNs (the 5,000-row RAW export contains
    # 66 MPNs more than once), so intake keeps the first occurrence and counts
    # the rest as an AUTO_SKIP. Without this, every large harvest would trip
    # BATCH_SELF_DUPLICATE (STOP) purely because of normal source noise.
    seen = set()

    def counting(gen):
        for rec in gen:
            key = rec["mpn"].upper()
            if key in seen:
                res.self_duplicate_count += 1
                continue
            seen.add(key)
            res.mpns.append(rec["mpn"])
            res.input_count += 1
            yield rec
            if limit and res.input_count >= limit:
                return

    res.written = pool.append_jsonl(path, counting(records()))

    if res.self_duplicate_count:
        # Recorded on the result itself, so the caller sees it whether or not a
        # manifest is attached; the manifest is only a persistence sink.
        res.exceptions.append({
            "code": gate.DUPLICATE_SKIP, "severity": gate.AUTO_SKIP, "mpn": None,
            "message": (f"{res.self_duplicate_count} MPN(s) repeated inside the source; "
                        f"first occurrence kept")})

    if res.written != res.input_count:
        res.exceptions.append({
            "code": gate.POOL_WRITE_FAIL, "severity": gate.STOP, "mpn": None,
            "message": (f"intake wrote {res.written} of {res.input_count} "
                        f"records to the raw pool")})
        res.stop = True

    if manifest is not None:
        manifest.set_counts(input_count=res.input_count)
        manifest.data.setdefault("source", {}).update({
            "kind": source_kind,
            "path": source_path,
            "category": category,
            "selector_count": len(sel) if sel else None,
            "limit": limit,
            "require_datasheet": bool(require_datasheet),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "raw_pool": path,
            "records_written": res.written,
            "source_self_duplicates": res.self_duplicate_count,
        })
        manifest.record_stage("INTAKE", ok=not res.stop,
                              note=(f"{res.written} records -> raw pool "
                                    f"({res.self_duplicate_count} source duplicates skipped)"))
        for e in res.exceptions:
            manifest.add_exception(e["code"], e["severity"], e.get("mpn"), e["message"])
        manifest.save()
    return res


# =====================================================================
# NORMALISE — clean + dedup + qualify -> candidate pool
# =====================================================================
class NormalizeResult:
    def __init__(self):
        self.input_count = 0
        self.cleaned_count = 0
        self.duplicate_count = 0
        self.self_duplicate_count = 0
        self.rejected_count = 0
        self.candidate_count = 0
        self.ready_count = 0
        self.rows = []
        self.exceptions = []
        self.stop = False
        self.path = None


def normalize(batch_id, master_csv=None, mfr_csv=None, root=None, manifest=None,
              skip_mass_duplicate_check=False):
    """Read the raw pool, build rows, drop duplicates/rejects, write candidates.

    Production MASTER is opened READ-ONLY (to seed the dup guard) and is never
    written by this function.
    """
    master_csv = master_csv or (r"C:\Users\Administrator.SC-202105071542\Desktop"
                                r"\szprocure-site\data\production\master_parts_v2.1.csv")
    mfr_map = load_brand_map(mfr_csv)

    raw_path = pool.raw_path(batch_id, root)
    records, torn = pool.read_jsonl(raw_path)
    res = NormalizeResult()
    res.input_count = len(records)
    if torn:
        res.exceptions.append({
            "code": gate.POOL_WRITE_FAIL, "severity": gate.WARNING, "mpn": None,
            "message": f"{torn} torn line(s) in the raw pool were skipped"})
    if not records:
        raise ProductDataError(f"raw pool is empty or missing: {raw_path}")

    # ---- build + qualify -------------------------------------------------
    built, rejects = {}, []
    for rec in records:
        mpn = rec["mpn"]
        syn = looks_synthetic(mpn, rec.get("manufacturer_raw", ""))
        if syn:
            rejects.append((mpn, gate.SYNTHETIC_MPN, syn))
            continue
        brand = mfr_map.get((rec.get("manufacturer_raw") or "").strip().lower(),
                            (rec.get("manufacturer_raw") or "").strip())
        row, _e = build_row(rec, mpn, brand)
        verdict, code, msg = qualify(row, mfr_map)
        if verdict == "reject":
            rejects.append((mpn, code, msg))
            continue
        if verdict == "warn":
            res.exceptions.append({"code": code, "severity": gate.WARNING,
                                   "mpn": mpn, "message": msg})
        built[mpn] = row

    res.cleaned_count = len(built)
    for mpn, code, msg in rejects:
        res.rejected_count += 1
        res.exceptions.append({"code": code, "severity": gate.severity_of(code),
                               "mpn": mpn, "message": msg})
        if code == gate.CJK_LEAK:
            res.stop = True     # data pollution -> hard stop

    # ---- duplicate guard (reuses the P0 module) --------------------------
    master_mpns = dedup.load_master_mpns(master_csv)
    d = dedup.guard(list(built.keys()), master_mpns)
    res.exceptions.extend(d.exceptions)
    if d.stop and not skip_mass_duplicate_check:
        res.stop = True
    res.duplicate_count = len(d.duplicates)
    res.self_duplicate_count = len(d.self_duplicates)

    rows = [built[m] for m in d.new]
    res.rows = rows
    res.candidate_count = len(rows)

    # ---- persist ---------------------------------------------------------
    pool.ensure(root)
    cpath = pool.candidates_path(batch_id, root)
    payload = {
        "batch_id": batch_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {"input_count": res.input_count, "cleaned_count": res.cleaned_count,
                   "duplicate_count": res.duplicate_count,
                   "self_duplicate_count": res.self_duplicate_count,
                   "rejected_count": res.rejected_count,
                   "candidate_count": res.candidate_count, "ready_count": 0},
        "rows": rows,
    }
    pool.atomic_write_json(cpath, payload)
    res.path = cpath

    pool.update_index(batch_id, {
        "status": "CANDIDATES",
        "candidate_count": res.candidate_count,
        "duplicate_count": res.duplicate_count,
        "rejected_count": res.rejected_count,
        "stop": res.stop,
    }, root)

    if manifest is not None:
        manifest.set_counts(input_count=res.input_count,
                            cleaned_count=res.cleaned_count,
                            duplicate_count=res.duplicate_count,
                            self_duplicate_count=res.self_duplicate_count,
                            rejected_count=res.rejected_count,
                            candidate_count=res.candidate_count,
                            ready_count=res.ready_count)
        manifest.data.setdefault("pool", {}).update({
            "raw": raw_path, "candidates": cpath,
            "candidate_count": res.candidate_count})
        manifest.record_stage("NORMALIZE", ok=not res.stop,
                              note=(f"{res.candidate_count} candidates / "
                                    f"{res.duplicate_count} dup / "
                                    f"{res.rejected_count} rejected"))
        for e in res.exceptions:
            manifest.add_exception(e["code"], e["severity"], e.get("mpn"), e["message"])
        manifest.save()
    return res


def master_row(row):
    """Project a candidate row onto MASTER_COLS (drops pool-only fields)."""
    return {c: row.get(c, "") for c in MASTER_COLS}
