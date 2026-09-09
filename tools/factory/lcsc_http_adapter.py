"""LCSC HTTP RAW Adapter — 02 Processing intake for LCSC English HTTP JSON.

This NEW module lets the 02 product-data line consume the 236 LCSC English
HTTP JSON envelopes produced by the FROZEN 01 acquirer (batch ``http236``,
source ``data/raw/lcsc_http_scale500/``).

Design basis: ``02_LCSC_HTTP_ADAPTER_DESIGN.md`` (§4). It is a NON-FROZEN
layer. It reuses proven helpers from ``factory.category`` (``_norm_*``,
``_assemble``, ``detect_category``, ``_unknown_fields``) and is wired into
``product_data.build_row`` via the ``_source_kind == "lcsc_http_json"`` branch.

Key principles (from the design, do NOT violate):
  * ``normalize_text`` runs ``fold_units`` (legal technical symbols -> ASCII)
    BEFORE any CJK/ASCII gate. It NEVER uses a crude ``ascii_only`` delete that
    would drop legitimate technical information.
  * ``paramVOList`` is processed in three tiers (A/B/C). Mapped specs go to
    ``attributes_json`` (canonical keys); UNMAPPED specs are preserved in full
    in ``attributes_json_unmapped`` (never silently dropped).
  * The 11 English-key family adapters mirror ``category.REGISTRY`` and emit the
    SAME canonical spec keys, so downstream ``publish_normalizer`` /
    ``attribute_dictionary`` alignment is guaranteed by construction.
  * ``real_time_snapshot`` / ``internal_raw`` are never read (01 isolated them).
"""

import json
import os
import re

from . import category, product_data, pool, gate
from .category import (
    UNKNOWN_CATEGORY,
    _norm_voltage, _norm_current, _norm_freq, _norm_data_rate,
    _norm_resistance, _norm_inductance, _norm_capacitance, _norm_power, _num_first,
    _enum, _attrs, _assemble, _faq, _unknown_fields, detect_category,
)
from .product_data import ProductDataError, IntakeResult

DEFAULT_HTTP_RAW = "data/raw/lcsc_http_scale500"

# --------------------------------------------------------------------------
# text normalisation: legal-symbol fold -> fullwidth fold -> ASCII gate
# --------------------------------------------------------------------------
# Known legal technical symbols are EQUIVALENT-normalised to ASCII (meaning
# preserved), NOT deleted. Only genuinely un-normalisable residues (real CJK /
# mojibake) are dropped by the final ASCII gate -- which is exactly the CJK
# guard the pipeline requires (``has_cjk`` hard-stops on ord > 127).
_UNIT_FOLD = {
    "\u2103": "degC",   # ℃  degree C
    "\u00b5": "u",       # µ  micro sign
    "\u03bc": "u",       # μ  greek mu
    "\u03a9": "Ohm",     # Ω  ohm
    "\u2126": "Ohm",     # Ω  ohm sign
    "\u00d7": "x",       # ×  multiplication
    "\u00b1": "+/-",     # ±  plus-minus
    "\u00b0": "deg",     # °  degree
    "\u221a": "sqrt",    # √  square root
    "\u00b2": "2",       # ²  superscript two
    "\u00b3": "3",       # ³  superscript three
    "\u00b7": "-",       # ·  middle dot
    "\u2009": " ",       # thin space
    "\u202f": " ",       # narrow no-break space
}


def fold_units(s):
    """Replace known legal technical symbols with ASCII equivalents."""
    if not s:
        return s
    s = str(s)
    for k, v in _UNIT_FOLD.items():
        if k in s:
            s = s.replace(k, v)
    return s


def fullwidth_to_halfwidth(s):
    """Zenkaku (fullwidth) -> Hankaku (halfwidth) for ASCII-range chars."""
    if not s:
        return s
    out = []
    for ch in str(s):
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def ascii_gate(s):
    """Drop any residue that is still non-ASCII (real CJK / mojibake)."""
    s = "".join(ch if ord(ch) < 128 else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_text(s):
    """fold_units -> fullwidth -> ASCII gate. Output is pure ASCII or ''."""
    if not s:
        return ""
    s = fold_units(s)
    s = fullwidth_to_halfwidth(s)
    s = ascii_gate(s)
    return s


# Custom memory-size normaliser (KB/MB/GB -> bytes) for MCU flash / EEPROM.
def _norm_memory(s):
    s = normalize_text(s or "")
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*(KB|MB|GB)", s, re.I)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[m.group(2).upper()]
    return int(n * mult)


# --------------------------------------------------------------------------
# paramNameEn -> (canonical_spec_key, normalizer)
# --------------------------------------------------------------------------
# Coverage is data-driven from the 236-envelope audit (442 distinct English
# keys). Keys chosen to land on the SAME canonical spec names the 11 Chinese
# adapters emit, so downstream allow-list alignment holds.
#
# Resistance / inductance normalisers must receive the RAW value (with Omega),
# because ``_norm_resistance`` / ``_norm_inductance`` consume Omega internally.
# ``build_en_attributes`` special-cases those two normalisers (passes the raw
# value, only fullwidth-folded) so they still parse correctly.
HTTP_ATTR_MAP = {
    # --- Voltage Regulator ------------------------------------------------
    "Output Type": ("output_type", None),
    "Output Voltage": ("output_voltage_v", _norm_voltage),
    "Output Current": ("output_current_a", _norm_current),
    "Voltage - Supply": ("working_voltage_v", _norm_voltage),
    "Operating Voltage": ("working_voltage_v", _norm_voltage),
    "Frequency - Switching": ("switching_freq_hz", _norm_freq),
    "Number of Channels": ("channels", _num_first),
    "Number of Outputs": ("channels", _num_first),
    "Tolerance": ("tolerance", _num_first),
    "Quiescent Current": ("iq_a", _norm_current),
    "Quiescent Supply Current": ("iq_a", _norm_current),
    "Supply Current (Iq)": ("iq_a", _norm_current),
    "Polarity": ("polarity", None),
    "Function": ("function", None),
    # --- Diode -----------------------------------------------------------
    "Diode Configuration": ("config", None),
    "Current - Rectified": ("forward_current_a", _norm_current),
    "Voltage - Forward(Vf@If)": ("vf_v", _norm_voltage),
    "Reverse Leakage Current (Ir)": ("vreverse_v", _norm_voltage),
    "Non-Repetitive Peak Forward Surge Current": ("forward_current_a", _norm_current),
    "Clamp Voltage": ("clamp_v", _norm_voltage),
    "Zener Voltage": ("vzener_v", _norm_voltage),
    "Reverse Recovery Time (trr)": ("trr_ns", _num_first),
    "Reverse Recovery Time(trr)": ("trr_ns", _num_first),
    # --- Capacitor -------------------------------------------------------
    "Capacitance": ("capacitance", _norm_capacitance),
    "Temperature Coefficient": ("temp_coef", None),
    "Equivalent Series Resistance(ESR)": ("esr_ohm", _norm_resistance),
    "Ripple Current": ("ripple_current_a", _norm_current),
    # --- Interface IC ----------------------------------------------------
    "Data Rate": ("data_rate", _norm_data_rate),
    "Interface": ("interface", None),
    "Type": ("type", None),
    "Number of Elements": ("elem_count", _num_first),
    "Bits per Element": ("bits_per_elem", _num_first),
    "Input Type": ("input_type", None),
    "I/O Count": ("io_count", _num_first),
    "Number of Nodes": ("nodes", _num_first),
    "CMTI(kV/us)": ("cmti_kvus", _num_first),
    "Isolation Voltage(Vrms)": ("isolation_vrms_v", _norm_voltage),
    # --- OpAmp -----------------------------------------------------------
    "Number of Amplifiers": ("num_amps", _num_first),
    "Input Bias Current(Ib)": ("ibias_a", _norm_current),
    "Common Mode Rejection Ratio(CMRR)": ("cmrr_db", _num_first),
    "Gain Bandwidth Product": ("gbw_hz", _norm_freq),
    "Gain Bandwidth Product(GBW)": ("gbw_hz", _norm_freq),
    "Vos - Input Offset Voltage": ("voffset_v", _norm_voltage),
    "Input Offset Voltage Drift(Vos TC)": ("voffset_drift_uv", _num_first),
    "Slew Rate": ("slew_rate_vus", _num_first),
    "Input Voltage Noise Density": ("noise_nv", _num_first),
    "Gain": ("gain_db", _num_first),
    "Frequency": ("frequency_hz", _norm_freq),
    "Single Supply": ("single_supply", None),
    "Dual Supply": ("dual_supply", None),
    "Rail to Rail": ("rail_to_rail", None),
    # --- MOSFET ----------------------------------------------------------
    "Drain to Source Voltage": ("vdss_v", _norm_voltage),
    "Drain-Source Voltage": ("vdss_v", _norm_voltage),
    "Current - Continuous Drain(Id)": ("id_a", _norm_current),
    "Pd - Power Dissipation": ("pd_w", _norm_power),
    "Gate Threshold Voltage": ("vgs_th_v", _norm_voltage),
    "RDS(on)": ("rds_on_ohm", _norm_resistance),
    "Gate Charge(Qg)": ("gate_charge_c", _num_first),
    "Output Capacitance(Coss)": ("output_cap_coss", _norm_capacitance),
    # --- Transistor ------------------------------------------------------
    "Collector Current(Ic)": ("ic_a", _norm_current),
    "Transistor Type": ("tran_type", None),
    "Collector-Emitter Voltage(Vceo)": ("vceo_v", _norm_voltage),
    "Vce Saturation": ("vce_sat_v", _norm_voltage),
    "Gate Trigger Current(Igt)": ("igt_ma", _num_first),
    "SCR Type": ("scr_type", None),
    # --- Logic IC --------------------------------------------------------
    "Sink Current(IOL)": ("iol_a", _norm_current),
    "Source Current(IOH)": ("ioh_a", _norm_current),
    "Propagation Delay(tpd)": ("tpd_ns", _num_first),
    "Propagation Delay": ("tpd_ns", _num_first),
    "Number of Gates": ("gates", _num_first),
    "Number of Logic Units": ("gates", _num_first),
    # --- Resistor --------------------------------------------------------
    "Resistance": ("resistance_ohm", _norm_resistance),
    "Resistor Type": ("rtype", None),
    "Power(Watts)": ("power_w", _norm_power),
    "Power Dissipation": ("power_w", _norm_power),
    # --- Inductor --------------------------------------------------------
    "Inductance": ("inductance_h", _norm_inductance),
    "Rated Current": ("rated_current_a", _norm_current),
    "Impedance": ("impedance_ohm", _norm_resistance),
    "Number of Lines": ("lines", _num_first),
    "Saturation Current(Isat)": ("isat_a", _norm_current),
    "DC Resistance(DCR)": ("dcr_ohm", _norm_resistance),
    # --- Microcontroller -------------------------------------------------
    "CPU Core": ("core", None),
    "Core Size": ("core_bits", _num_first),
    "CPU Maximum Speed": ("frequency_hz", _norm_freq),
    "Number of I/O": ("io_count", _num_first),
    "Program Storage Size": ("flash_bytes", _norm_memory),
    "Program Memory Type": ("flash_type", None),
    "ADC (Bit)": ("adc_bits", _num_first),
    "Oscillator Type": ("oscillator_type", None),
    "EEPROM": ("eeprom_bytes", _norm_memory),
    # --- generic / mechanical (preserved, family-agnostic) ---------------
    "Voltage Rating": ("rated_voltage_v", _norm_voltage),
    "Pitch": ("pitch_mm", _num_first),
    "Height": ("height_mm", _num_first),
    "Length": ("length_mm", _num_first),
    "Width": ("width_mm", _num_first),
    "Row Spacing": ("row_spacing_mm", _num_first),
    "Number of Rows": ("num_rows", _num_first),
    "Number of PINs Per Row": ("pins_per_row", _num_first),
    "Color": ("color", None),
    "Contact Plating": ("contact_plating", None),
    "Contact Material": ("contact_material", None),
    "Operating Temperature": ("operating_temp", None),
    "Operating Junction Temperature Range": ("operating_junction_temp", None),
    "Features": ("features", None),
    "Topology": ("topology", None),
    "Reference Series": ("reference_series", None),
    "Switch tube": ("switch_tube", None),
    "Clock Frequency": ("frequency_hz", _norm_freq),
}

# paramName (Chinese) fallbacks for envelopes where paramNameEn is empty.
_CN_FALLBACK = {
    "输出电压": ("output_voltage_v", _norm_voltage),
    "输出电流": ("output_current_a", _norm_current),
    "容值": ("capacitance", _norm_capacitance),
    "阻值": ("resistance_ohm", _norm_resistance),
    "电感值": ("inductance_h", _norm_inductance),
    "漏源电压(Vdss)": ("vdss_v", _norm_voltage),
    "集电极电流(Ic)": ("ic_a", _norm_current),
    "增益带宽积(GBW)": ("gbw_hz", _norm_freq),
}


def build_en_attributes(mp):
    """Return (attrs_canon, attrs_unmapped).

    Tier A: paramNameEn (or paramName) in HTTP_ATTR_MAP -> canonical key
            (value normalised). Resistance / inductance normalisers receive the
            RAW value (Omega preserved) so they still parse.
    Tier B: unmapped specs -> preserved verbatim (folded to ASCII) in
            attributes_json_unmapped. NEVER dropped.
    Tier C: only empty / fully-un-normalisable values are skipped.
    """
    attrs_canon, attrs_unmapped = {}, {}
    for item in (mp.get("paramVOList") or []):
        name_en = (item.get("paramNameEn") or "").strip()
        if not name_en:
            name_cn = (item.get("paramName") or "").strip()
            if name_cn in _CN_FALLBACK:
                name_en, (canon_key, normalizer) = name_cn, _CN_FALLBACK[name_cn]
            else:
                name_en = name_cn
        raw_val = item.get("paramValueEn") or item.get("paramValue") or ""
        folded_val = normalize_text(raw_val)
        if not name_en or not folded_val:
            continue
        if name_en in HTTP_ATTR_MAP:
            canon_key, normalizer = HTTP_ATTR_MAP[name_en]
            if normalizer is _norm_resistance or normalizer is _norm_inductance:
                # Omega must reach the normaliser untouched.
                nval = normalizer(fullwidth_to_halfwidth(raw_val))
            elif normalizer is not None:
                nval = normalizer(normalize_text(raw_val))
            else:
                nval = folded_val  # string spec, already ASCII
            if nval is None or nval == "":
                continue
            attrs_canon[canon_key] = nval
        else:
            attrs_unmapped[name_en] = folded_val
    return attrs_canon, attrs_unmapped


# --------------------------------------------------------------------------
# envelope flattening -> record dict consumed by build_row
# --------------------------------------------------------------------------
def flatten_envelope(env):
    sr = env.get("source_raw", {}) or {}
    mp = sr.get("main_product", {}) or {}
    od = sr.get("overviewData") or {}
    al = sr.get("alternatePartList") or []

    attrs_canon, attrs_unmapped = build_en_attributes(mp)

    alts = "; ".join(a["productModel"] for a in al
                     if (a.get("productModel") or "").strip())
    # Richer relationship structure preserved for a future parts network.
    related_raw = [{
        "productModel": a.get("productModel"),
        "brandNameEn": a.get("brandNameEn"),
        "encapStandard": a.get("encapStandard"),
        "productCode": a.get("productCode"),
    } for a in al if (a.get("productModel") or "").strip()]

    desc = (mp.get("productNameEn") or "").strip()
    intro = (mp.get("productIntroEn") or (od.get("productIntroEn") or "")).strip()
    desc_full = (desc + " - " + intro).strip(" -") if intro else desc
    desc_norm = normalize_text(desc_full)

    apps_en = normalize_text(od.get("pdfApplicationAreasEn") or "")

    return {
        "mpn": (mp.get("productModel") or "").strip(),
        "manufacturer_raw": (mp.get("brandNameEn") or "").strip(),
        "catalogName": (mp.get("wmCatalogNameEn") or "").strip(),
        "category": "",
        "description": desc_norm,
        "attributes_json": json.dumps(attrs_canon, ensure_ascii=False),
        "attributes_json_unmapped": json.dumps(attrs_unmapped, ensure_ascii=False),
        "source_datasheet_url": "",
        "supplier_sku": (mp.get("productCode") or "").strip(),
        "alternative_parts": alts,
        "related_parts_raw": related_raw,
        "_applications_en": apps_en,
        "_source_kind": "lcsc_http_json",
    }


# --------------------------------------------------------------------------
# English-key family adapters (mirror category.REGISTRY, emit same canonical keys)
# --------------------------------------------------------------------------
class HTTPCategoryAdapter:
    canonical = ""
    min_specs = 1

    def build(self, record, mpn, brand):
        raise NotImplementedError


class HTTPMCUAdapter(HTTPCategoryAdapter):
    canonical = "Microcontroller"
    min_specs = 2
    APPS = ("Embedded control; IoT devices; Industrial automation; "
            "Consumer electronics; Motor control")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        core = _enum(a.get("core") or "")
        if core:
            specs["core"] = core
        cb = a.get("core_bits")
        if isinstance(cb, (int, float)):
            specs["core_bits"] = int(cb)
        freq = a.get("frequency_hz")
        if isinstance(freq, (int, float)):
            specs["frequency_hz"] = int(freq)
        flash = a.get("flash_bytes")
        if isinstance(flash, (int, float)):
            specs["flash_bytes"] = int(flash)
        ram = a.get("ram_bytes")
        if isinstance(ram, (int, float)):
            specs["ram_bytes"] = int(ram)
        io = a.get("io_count")
        if isinstance(io, (int, float)):
            specs["io_count"] = int(io)
        volt = a.get("voltage_v")
        if isinstance(volt, (int, float)):
            specs["voltage_v"] = volt
        if a.get("flash_type") is not None:
            specs["flash_type"] = a["flash_type"]
        if a.get("adc_bits") is not None:
            specs["adc_bits"] = a["adc_bits"]
        if a.get("oscillator_type") is not None:
            specs["oscillator_type"] = a["oscillator_type"]
        if a.get("eeprom_bytes") is not None:
            specs["eeprom_bytes"] = a["eeprom_bytes"]

        parts = [f"{brand} {mpn}"]
        if core:
            parts.append(core)
        if isinstance(freq, (int, float)):
            parts.append(f"{freq / 1e6:.0f} MHz")
        if isinstance(flash, (int, float)):
            parts.append(f"{flash / 1024:.0f} KB Flash")
        if isinstance(ram, (int, float)):
            parts.append(f"{ram / 1024:.0f} KB SRAM")
        if isinstance(io, (int, float)):
            parts.append(f"{int(io)} I/O")
        if isinstance(volt, (int, float)):
            parts.append(f"operating voltage {volt} V")
        description = normalize_text(" - ".join(parts)) + "."
        if len(parts) <= 1:
            description = normalize_text((record.get("description") or f"{brand} {mpn}"))
        sub = (f"{int(cb)}-bit MCU" if isinstance(cb, (int, float))
               else (f"{core} MCU" if core else "Microcontroller"))
        kw = "; ".join(str(p) for p in [mpn, sub, self.canonical] if p)
        faq = ""
        if isinstance(io, (int, float)):
            faq = _faq(mpn, f"How many I/O pins does {mpn} have",
                       f"{mpn} provides {int(io)} I/O pins")
        elif core:
            faq = _faq(mpn, f"What core does {mpn} use",
                       f"{mpn} is based on a {core} core")
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPVoltageRegulatorAdapter(HTTPCategoryAdapter):
    canonical = "Voltage Regulator"
    min_specs = 1
    APPS = ("Power management; DC-DC conversion; LDO regulation; "
            "Battery-powered devices; Embedded systems")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        ot = _enum(a.get("output_type") or "")
        if ot:
            specs["output_type"] = ot
        ov = a.get("output_voltage_v")
        if isinstance(ov, (int, float)):
            specs["output_voltage_v"] = round(ov, 4)
        pol = _enum(a.get("polarity") or "")
        if pol:
            specs["polarity"] = pol
        oc = a.get("output_current_a")
        if isinstance(oc, (int, float)):
            specs["output_current_a"] = round(oc, 5)
        fn = _enum(a.get("function") or "")
        if fn:
            specs["function"] = fn
        wv = a.get("working_voltage_v")
        if isinstance(wv, (int, float)):
            specs["working_voltage_v"] = round(wv, 4)
        sf = a.get("switching_freq_hz")
        if isinstance(sf, (int, float)):
            specs["switching_freq_hz"] = int(sf)
        ch = a.get("channels")
        if isinstance(ch, (int, float)):
            specs["channels"] = int(ch)
        tol = a.get("tolerance")
        if isinstance(tol, (int, float)):
            specs["tolerance"] = tol
        iq = a.get("iq_a")
        if isinstance(iq, (int, float)):
            specs["iq_a"] = round(iq, 7)
        sub = ot or "Voltage Regulator"
        parts = [f"{brand} {mpn}", ot,
                 (f"{ov:.2f} V" if isinstance(ov, (int, float)) else ""),
                 (f"{oc:.3f} A" if isinstance(oc, (int, float)) else "")]
        faq = _faq(mpn, f"What is the output voltage of {mpn}",
                   (f"{mpn} delivers {ov:.2f} V" if isinstance(ov, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPDiodeAdapter(HTTPCategoryAdapter):
    canonical = "Diode"
    min_specs = 1
    APPS = ("Reverse protection; Rectification; Voltage clamping; "
            "ESD suppression; Power supplies")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        cfg = _enum(a.get("config") or "")
        if cfg:
            specs["config"] = cfg
        fc = a.get("forward_current_a")
        if isinstance(fc, (int, float)):
            specs["forward_current_a"] = round(fc, 5)
        vf = a.get("vf_v")
        if isinstance(vf, (int, float)):
            specs["vf_v"] = round(vf, 4)
        vr = a.get("vreverse_v")
        if isinstance(vr, (int, float)):
            specs["vreverse_v"] = round(vr, 3)
        clamp = a.get("clamp_v")
        if isinstance(clamp, (int, float)):
            specs["clamp_v"] = round(clamp, 3)
        pol = _enum(a.get("polarity") or "")
        if pol:
            specs["polarity"] = pol
        ppp = a.get("ppp_w")
        if isinstance(ppp, (int, float)):
            specs["ppp_w"] = int(ppp)
        vz = a.get("vzener_v")
        if isinstance(vz, (int, float)):
            specs["vzener_v"] = round(vz, 3)
        trr = a.get("trr_ns")
        if isinstance(trr, (int, float)):
            specs["trr_ns"] = int(trr)
        sub = cfg or "Diode"
        parts = [f"{brand} {mpn}", cfg,
                 (f"{vf:.2f} V Vf" if isinstance(vf, (int, float)) else ""),
                 (f"{vr:.1f} V reverse" if isinstance(vr, (int, float)) else "")]
        faq = _faq(mpn, f"What is the reverse voltage rating of {mpn}",
                   (f"{mpn} is rated for {vr:.1f} V reverse"
                    if isinstance(vr, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPCapacitorAdapter(HTTPCategoryAdapter):
    canonical = "Capacitor"
    min_specs = 1
    APPS = ("Decoupling; Filtering; Energy storage; Timing; Signal conditioning")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        cap = a.get("capacitance")
        if isinstance(cap, (int, float)):
            specs["capacitance"] = cap
        tol = a.get("tolerance")
        if isinstance(tol, (int, float)):
            specs["tolerance"] = tol
        rv = a.get("rated_voltage_v")
        if isinstance(rv, (int, float)):
            specs["rated_voltage_v"] = round(rv, 3)
        tc = _enum(a.get("temp_coef") or "")
        if tc:
            specs["temp_coef"] = tc
        esr = a.get("esr_ohm")
        if isinstance(esr, (int, float)):
            specs["esr_ohm"] = round(esr, 6)
        ripple = a.get("ripple_current_a")
        if isinstance(ripple, (int, float)):
            specs["ripple_current_a"] = round(ripple, 5)
        sub = (tc + " Capacitor") if tc else "Ceramic Capacitor"
        parts = [f"{brand} {mpn}",
                 (f"{cap} F" if isinstance(cap, (int, float)) else ""),
                 (f"{rv:.1f} V" if isinstance(rv, (int, float)) else ""),
                 (tc if tc else "")]
        faq = _faq(mpn, f"What is the capacitance of {mpn}",
                   (f"{mpn} is a {cap} F capacitor"
                    if isinstance(cap, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPInterfaceICAdapter(HTTPCategoryAdapter):
    canonical = "Interface IC"
    min_specs = 1
    APPS = ("Signal translation; Bus interfacing; Level shifting; "
            "Industrial communication; Embedded I/O")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        wv = a.get("working_voltage_v")
        if isinstance(wv, (int, float)):
            specs["working_voltage_v"] = round(wv, 4)
        dr = a.get("data_rate")
        if isinstance(dr, (int, float)):
            specs["data_rate"] = int(dr)
        t = _enum(a.get("interface") or "")
        if t:
            specs["interface"] = t
        ec = a.get("elem_count")
        if isinstance(ec, (int, float)):
            specs["elem_count"] = int(ec)
        bpe = a.get("bits_per_elem")
        if isinstance(bpe, (int, float)):
            specs["bits_per_elem"] = int(bpe)
        it = _enum(a.get("input_type") or "")
        if it:
            specs["input_type"] = it
        ioc = a.get("io_count")
        if isinstance(ioc, (int, float)):
            specs["io_count"] = int(ioc)
        iq = a.get("iq_a")
        if isinstance(iq, (int, float)):
            specs["iq_a"] = round(iq, 7)
        nodes = a.get("nodes")
        if isinstance(nodes, (int, float)):
            specs["nodes"] = int(nodes)
        cmti = a.get("cmti_kvus")
        if isinstance(cmti, (int, float)):
            specs["cmti_kvus"] = cmti
        vrms = a.get("isolation_vrms_v")
        if isinstance(vrms, (int, float)):
            specs["isolation_vrms_v"] = round(vrms, 3)
        sub = t or "Interface IC"
        parts = [f"{brand} {mpn}", t,
                 (f"{dr} bps" if isinstance(dr, (int, float)) else ""),
                 (f"{wv:.2f} V" if isinstance(wv, (int, float)) else "")]
        faq = _faq(mpn, f"What interface does {mpn} support",
                   (f"{mpn} is a {t} interface device" if t else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPOpAmpAdapter(HTTPCategoryAdapter):
    canonical = "Operational Amplifier"
    min_specs = 1
    APPS = ("Signal conditioning; Sensor amplification; Active filtering; "
            "Instrumentation; Control loops")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        na = a.get("num_amps")
        if isinstance(na, (int, float)):
            specs["num_amps"] = int(na)
        ib = a.get("ibias_a")
        if isinstance(ib, (int, float)):
            specs["ibias_a"] = round(ib, 9)
        cmrr = a.get("cmrr_db")
        if isinstance(cmrr, (int, float)):
            specs["cmrr_db"] = cmrr
        gbw = a.get("gbw_hz")
        if isinstance(gbw, (int, float)):
            specs["gbw_hz"] = int(gbw)
        vos = a.get("voffset_v")
        if isinstance(vos, (int, float)):
            specs["voffset_v"] = round(vos, 6)
        iq = a.get("iq_a")
        if isinstance(iq, (int, float)):
            specs["iq_a"] = round(iq, 7)
        gain = a.get("gain_db")
        if isinstance(gain, (int, float)):
            specs["gain_db"] = gain
        freq = a.get("frequency_hz")
        if isinstance(freq, (int, float)):
            specs["frequency_hz"] = int(freq)
        # op-amp supply voltage arrives via either supply_v or working_voltage_v
        sv = a.get("supply_v")
        if sv is None:
            sv = a.get("working_voltage_v")
        if isinstance(sv, (int, float)):
            specs["supply_v"] = round(sv, 4)
        icur = a.get("current_a")
        if isinstance(icur, (int, float)):
            specs["current_a"] = round(icur, 6)
        ocur = a.get("output_current_a")
        if isinstance(ocur, (int, float)):
            specs["output_current_a"] = round(ocur, 6)
        r2r = _enum(a.get("rail_to_rail") or "")
        if r2r:
            specs["rail_to_rail"] = r2r
        sub = (f"{int(na)}-Channel Op Amp" if isinstance(na, (int, float))
               else "Operational Amplifier")
        parts = [f"{brand} {mpn}",
                 (f"{int(na)}-channel" if isinstance(na, (int, float)) else ""),
                 (f"GBW {gbw} Hz" if isinstance(gbw, (int, float)) else "")]
        faq = _faq(mpn, f"What is the gain bandwidth product of {mpn}",
                   (f"{mpn} has a GBW of {gbw} Hz" if isinstance(gbw, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPMOSFETAdapter(HTTPCategoryAdapter):
    canonical = "MOSFET"
    min_specs = 1
    APPS = ("Power switching; Motor drive; DC-DC; Load switching; "
            "Power management")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        vdss = a.get("vdss_v")
        if isinstance(vdss, (int, float)):
            specs["vdss_v"] = round(vdss, 3)
        idc = a.get("id_a")
        if isinstance(idc, (int, float)):
            specs["id_a"] = round(idc, 4)
        ct = _enum(a.get("type") or "")
        if ct:
            specs["chan_type"] = ct
        pd = a.get("pd_w")
        if isinstance(pd, (int, float)):
            specs["pd_w"] = round(pd, 5)
        vgsth = a.get("vgs_th_v")
        if isinstance(vgsth, (int, float)):
            specs["vgs_th_v"] = round(vgsth, 4)
        rds = a.get("rds_on_ohm")
        if isinstance(rds, (int, float)):
            specs["rds_on_ohm"] = round(rds, 6)
        qg = a.get("gate_charge_c")
        if isinstance(qg, (int, float)):
            specs["gate_charge_c"] = qg
        sub = (ct or "MOS") + " MOSFET"
        parts = [f"{brand} {mpn}", ct,
                 (f"{vdss:.1f} V" if isinstance(vdss, (int, float)) else ""),
                 (f"{idc:.2f} A" if isinstance(idc, (int, float)) else "")]
        faq = _faq(mpn, f"What is the drain-source voltage of {mpn}",
                   (f"{mpn} is rated for {vdss:.1f} V DS"
                    if isinstance(vdss, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPTransistorAdapter(HTTPCategoryAdapter):
    canonical = "Transistor"
    min_specs = 1
    APPS = ("Amplification; Switching; Signal buffering; Linear regulation; "
            "Driver stages")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        ic = a.get("ic_a")
        if isinstance(ic, (int, float)):
            specs["ic_a"] = round(ic, 4)
        tt = _enum(a.get("tran_type") or "")
        if tt:
            specs["tran_type"] = tt
        vceo = a.get("vceo_v")
        if isinstance(vceo, (int, float)):
            specs["vceo_v"] = round(vceo, 3)
        st = _enum(a.get("scr_type") or "")
        if st:
            specs["scr_type"] = st
        igt = a.get("igt_ma")
        if isinstance(igt, (int, float)):
            specs["igt_ma"] = igt
        sub = (tt or "BJT") + " Transistor"
        parts = [f"{brand} {mpn}", tt,
                 (f"{vceo:.1f} V" if isinstance(vceo, (int, float)) else ""),
                 (f"{ic:.2f} A" if isinstance(ic, (int, float)) else "")]
        faq = _faq(mpn, f"What is the collector current of {mpn}",
                   (f"{mpn} handles {ic:.2f} A collector"
                    if isinstance(ic, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPLogicICAdapter(HTTPCategoryAdapter):
    canonical = "Logic IC"
    min_specs = 1
    APPS = ("Digital logic; Glue logic; Signal buffering; Level translation; "
            "Combinational logic")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        sv = a.get("supply_v")
        if sv is None:
            sv = a.get("working_voltage_v")
        if isinstance(sv, (int, float)):
            specs["supply_v"] = round(sv, 4)
        iol = a.get("iol_a")
        if isinstance(iol, (int, float)):
            specs["iol_a"] = round(iol, 5)
        tpd = a.get("tpd_ns")
        if isinstance(tpd, (int, float)):
            specs["tpd_ns"] = tpd
        ioh = a.get("ioh_a")
        if isinstance(ioh, (int, float)):
            specs["ioh_a"] = round(ioh, 5)
        iq = a.get("iq_a")
        if isinstance(iq, (int, float)):
            specs["iq_a"] = round(iq, 7)
        fn = _enum(a.get("function") or "")
        if fn:
            specs["function"] = fn
        gates = a.get("gates")
        if isinstance(gates, (int, float)):
            specs["gates"] = int(gates)
        sub = fn or "Logic IC"
        parts = [f"{brand} {mpn}", fn,
                 (f"{sv:.2f} V" if isinstance(sv, (int, float)) else ""),
                 (f"{tpd} ns propagation" if isinstance(tpd, (int, float)) else "")]
        faq = _faq(mpn, f"What supply voltage does {mpn} use",
                   (f"{mpn} operates at {sv:.2f} V" if isinstance(sv, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPResistorAdapter(HTTPCategoryAdapter):
    canonical = "Resistor"
    min_specs = 1
    APPS = ("Current limiting; Voltage division; Pull-up/down; "
            "Termination; Sensing")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        r = a.get("resistance_ohm")
        if isinstance(r, (int, float)):
            specs["resistance_ohm"] = r
        tol = a.get("tolerance")
        if isinstance(tol, (int, float)):
            specs["tolerance"] = tol
        rt = _enum(a.get("rtype") or "")
        if rt:
            specs["rtype"] = rt
        mv = a.get("max_voltage_v")
        if mv is None:
            mv = a.get("rated_voltage_v")
        if isinstance(mv, (int, float)):
            specs["max_voltage_v"] = round(mv, 3)
        p = a.get("power_w")
        if isinstance(p, (int, float)):
            specs["power_w"] = round(p, 5)
        sub = (rt or "Chip") + " Resistor"
        parts = [f"{brand} {mpn}",
                 (f"{r} Ohm" if isinstance(r, (int, float)) else ""),
                 (f"{mv:.1f} V" if isinstance(mv, (int, float)) else ""),
                 (f"{p} W" if isinstance(p, (int, float)) else "")]
        faq = _faq(mpn, f"What is the resistance of {mpn}",
                   (f"{mpn} is a {r} ohm resistor" if isinstance(r, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


class HTTPInductorAdapter(HTTPCategoryAdapter):
    canonical = "Inductor"
    min_specs = 1
    APPS = ("Power filtering; Energy storage; EMI suppression; "
            "DC-DC; RF chokes")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        L = a.get("inductance_h")
        if isinstance(L, (int, float)):
            specs["inductance_h"] = L
        tol = a.get("tolerance")
        if isinstance(tol, (int, float)):
            specs["tolerance"] = tol
        rc = a.get("rated_current_a")
        if isinstance(rc, (int, float)):
            specs["rated_current_a"] = round(rc, 4)
        z = a.get("impedance_ohm")
        if isinstance(z, (int, float)):
            specs["impedance_ohm"] = round(z, 3)
        lines = a.get("lines")
        if isinstance(lines, (int, float)):
            specs["lines"] = int(lines)
        isat = a.get("isat_a")
        if isinstance(isat, (int, float)):
            specs["isat_a"] = round(isat, 4)
        dcr = a.get("dcr_ohm")
        if isinstance(dcr, (int, float)):
            specs["dcr_ohm"] = round(dcr, 6)
        sub = "Power Inductor"
        parts = [f"{brand} {mpn}",
                 (f"{L} H" if isinstance(L, (int, float)) else ""),
                 (f"{rc:.3f} A rated" if isinstance(rc, (int, float)) else "")]
        faq = _faq(mpn, f"What is the inductance of {mpn}",
                   (f"{mpn} is a {L} H inductor" if isinstance(L, (int, float)) else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


HTTP_REGISTRY = {
    a.canonical: a for a in (
        HTTPMCUAdapter(), HTTPVoltageRegulatorAdapter(), HTTPDiodeAdapter(),
        HTTPCapacitorAdapter(), HTTPInterfaceICAdapter(), HTTPOpAmpAdapter(),
        HTTPMOSFETAdapter(), HTTPTransistorAdapter(), HTTPLogicICAdapter(),
        HTTPResistorAdapter(), HTTPInductorAdapter(),
    )
}


# --------------------------------------------------------------------------
# L4' English-attribute fingerprint (fallback when detect_category returns UNKNOWN)
# --------------------------------------------------------------------------
_HTTP_ATTR_FINGERPRINT = [
    ("core", "Microcontroller"),
    ("frequency_hz", "Microcontroller"),
    ("io_count", "Microcontroller"),
    ("flash_bytes", "Microcontroller"),
    ("capacitance", "Capacitor"),
    ("vdss_v", "MOSFET"),
    ("rds_on_ohm", "MOSFET"),
    ("id_a", "MOSFET"),
    ("forward_current_a", "Diode"),
    ("vf_v", "Diode"),
    ("config", "Diode"),
    ("gbw_hz", "Operational Amplifier"),
    ("ibias_a", "Operational Amplifier"),
    ("num_amps", "Operational Amplifier"),
    ("inductance_h", "Inductor"),
    ("resistance_ohm", "Resistor"),
    ("ic_a", "Transistor"),
    ("tran_type", "Transistor"),
    ("data_rate", "Interface IC"),
    ("interface", "Interface IC"),
    ("output_voltage_v", "Voltage Regulator"),
    ("output_type", "Voltage Regulator"),
    ("topology", "Voltage Regulator"),          # switching-regulator controller
    ("tpd_ns", "Logic IC"),
]

# High-confidence UNMAPPED English-attribute fingerprint (L4'). Only keys that
# UNIQUELY identify a single family. Matched case-insensitively.
_HTTP_UNMAPPED_FINGERPRINT = [
    ("impedance @ frequency", "Inductor"),      # ferrite bead
    ("forward voltage", "Diode"),
    ("drain-source voltage", "MOSFET"),
    ("gate charge", "MOSFET"),
    ("collector-emitter breakdown voltage", "Transistor"),
]

# UNMISTAKABLE description/subtitle keywords (L4'). Matched against the
# normalized (ASCII) English description. Conservative: only terms that can ONLY
# mean one of the 11 supported families.
_HTTP_DESC_FINGERPRINT = [
    ("ferrite bead", "Inductor"),
    ("dc dc switching", "Voltage Regulator"),
    ("switching regulator", "Voltage Regulator"),
    ("buck converter", "Voltage Regulator"),
    ("boost converter", "Voltage Regulator"),
]


def _detect_by_en_attrs(record):
    """L4' English-attribute fingerprint fallback (high confidence only).

    Returns (canon, meta) or (None, None). Policy: prefer Uncategorized over a
    wrong classification -- only UNMISTAKABLE signals are accepted.
    """
    # 1) canonical spec keys
    try:
        canon_attrs = json.loads(record.get("attributes_json") or "{}")
    except Exception:
        canon_attrs = {}
    for key, fam in _HTTP_ATTR_FINGERPRINT:
        if key in canon_attrs:
            return fam, {"level": "L4_en_attrs",
                         "classification_source": "en_attr_fingerprint",
                         "matched_keys": [key],
                         "reason": f"canonical attr '{key}'"}
    # 2) unmapped English keys (case-insensitive)
    try:
        unmap = {k.lower(): v for k, v in
                 json.loads(record.get("attributes_json_unmapped") or "{}").items()}
    except Exception:
        unmap = {}
    for key, fam in _HTTP_UNMAPPED_FINGERPRINT:
        if key in unmap:
            return fam, {"level": "L4_en_attrs_unmapped",
                         "classification_source": "en_attr_fingerprint",
                         "matched_keys": [key],
                         "reason": f"unmapped attr '{key}'"}
    # 3) description keywords (UNMISTAKABLE only)
    desc = (record.get("description") or "").lower()
    for kw, fam in _HTTP_DESC_FINGERPRINT:
        if kw in desc:
            return fam, {"level": "L4_desc",
                         "classification_source": "en_attr_fingerprint",
                         "matched_keys": [kw],
                         "reason": f"description keyword '{kw}'"}
    return None, None


def http_build_category_row(record, mpn, brand):
    """English-key mirror of category.build_category_row."""
    canon, signals, confidence = detect_category(record)
    if canon == UNKNOWN_CATEGORY:
        canon2, det_meta = _detect_by_en_attrs(record)
        if canon2:
            canon = canon2
            confidence = "medium"
            signals = {"level": det_meta["level"],
                       "value": canon2,
                       "classification_source": "en_attr_fingerprint",
                       "matched_keys": det_meta["matched_keys"],
                       "reason": det_meta["reason"]}
    meta = {"category": canon, "confidence": confidence,
            "signals": signals,
            "needs_review": canon == UNKNOWN_CATEGORY}
    if canon == UNKNOWN_CATEGORY or canon not in HTTP_REGISTRY:
        return _unknown_fields(record, mpn, brand), meta
    adapter = HTTP_REGISTRY[canon]
    meta["min_specs"] = adapter.min_specs
    fields = adapter.build(record, mpn, brand)
    apps = (record.get("_applications_en") or "").strip()
    if apps:
        fields["applications"] = apps
    return fields, meta


# --------------------------------------------------------------------------
# intake: JSON envelope directory -> raw pool (mirror product_data.intake)
# --------------------------------------------------------------------------
def intake_http_json(batch_id, source_path=DEFAULT_HTTP_RAW, selector=None,
                     limit=None, root=None, manifest=None):
    """Stream LCSC HTTP JSON envelopes into the local raw pool.

    ``source_path`` MUST be passed explicitly; it never falls back to the empty
    default ``data/raw/lcsc_http/`` directory (which is a DIFFERENT batch).
    """
    if not os.path.exists(source_path):
        raise ProductDataError(f"HTTP RAW source not found: {source_path}")

    res = IntakeResult()
    res.batch_id = batch_id
    res.source_kind = "lcsc_http_json"
    res.source_path = source_path

    sel = {m.strip().upper() for m in selector} if selector else None

    pool.ensure(root)
    path = pool.raw_path(batch_id, root)
    if os.path.exists(path):
        try:
            os.unlink(path)       # a fresh intake replaces the previous snapshot
        except OSError:
            pass                  # tolerate non-deletable snapshot (idempotent re-run)

    def envelopes():
        for fn in sorted(os.listdir(source_path)):
            if fn.startswith("C") and fn.endswith(".json"):
                yield os.path.join(source_path, fn)

    def records():
        for fp in envelopes():
            try:
                with open(fp, encoding="utf-8") as f:
                    env = json.load(f)
                rec = flatten_envelope(env)
            except Exception as e:
                res.exceptions.append({
                    "code": "HTTP_ENVELOPE_PARSE", "severity": gate.WARNING,
                    "mpn": None, "message": f"{fp}: {e}"})
                continue
            mpn = (rec.get("mpn") or "").strip()
            if not mpn:
                continue
            if sel and mpn.upper() not in sel:
                continue
            yield rec

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
        res.exceptions.append({
            "code": gate.DUPLICATE_SKIP, "severity": gate.AUTO_SKIP, "mpn": None,
            "message": (f"{res.self_duplicate_count} MPN(s) repeated inside the "
                        f"source; first occurrence kept")})

    if res.written != res.input_count:
        res.exceptions.append({
            "code": gate.POOL_WRITE_FAIL, "severity": gate.STOP, "mpn": None,
            "message": (f"intake wrote {res.written} of {res.input_count} "
                        f"records to the raw pool")})
        res.stop = True

    if manifest is not None:
        manifest.set_counts(input_count=res.input_count)
        manifest.data.setdefault("source", {}).update({
            "kind": "lcsc_http_json",
            "path": source_path,
            "selector_count": len(sel) if sel else None,
            "limit": limit,
            "fetched_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "raw_pool": path,
            "records_written": res.written,
            "source_self_duplicates": res.self_duplicate_count,
        })
        manifest.record_stage("INTAKE_HTTP", ok=not res.stop,
                              note=(f"{res.written} records -> raw pool "
                                    f"({res.self_duplicate_count} source duplicates skipped)"))
        for e in res.exceptions:
            manifest.add_exception(e["code"], e["severity"], e.get("mpn"), e["message"])
        manifest.save()
    return res
