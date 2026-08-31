"""Category-Aware Product Data Factory — Category Adapter layer (Phase P1-E).

This module is NOT a frozen layer. It is the heart of P1-E:

  * `detect_category(record)` runs a 4-level detection pipeline
    (catalogName -> category column -> description keywords ->
    attributes_json key fingerprint) and NEVER falls back to Microcontroller.
    Anything it cannot confidently classify becomes ``UNKNOWN_CATEGORY``.
  * Each supported component family has its own ``CategoryAdapter`` that
    extracts the *right* English spec fields from the real Chinese-keyed
    RAW ``attributes_json`` (the old ``build_row`` only knew MCU fields).
  * ``build_category_row(record, mpn, brand)`` returns the category-shaped
    fields (category / subcategory / description / applications / keywords /
    attributes_json / faq) plus a meta dict carrying the detected signals
    and a ``needs_review`` flag for unmapped rows.

Design constraints (approved P1-E decisions):
  - Coverage-first wave (A): 11 adapters
    (MCU, Voltage Regulator, Diode, Capacitor, Interface IC, OpAmp,
    MOSFET, Transistor, Logic IC, Resistor, Inductor).
  - Unmapped rows are KEPT in the candidate pool but flagged
    UNMAPPED_CATEGORY (a WARNING, never auto-released).
  - Numeric values are normalised to SI base units; non-ASCII values are
    dropped (CJK guard) — identical discipline to the frozen pipeline.
"""
import json
import re
from abc import ABC, abstractmethod

# --------------------------------------------------------------------------
# shared constants
# --------------------------------------------------------------------------
UNKNOWN_CATEGORY = "Uncategorized"

_MICRO_U = "\u00b5"   # µ micro sign
_MICRO_G = "\u03bc"   # μ greek mu


# --------------------------------------------------------------------------
# numeric normalisation helpers (SI base units)
# --------------------------------------------------------------------------
def _norm(s, table):
    """Generic unit parser. Returns averaged base value or None.

    Handles a leading number directly followed by a unit, AND bare-number
    ranges where the unit appears only at the end (e.g. '0.8-5V').
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    alt = "|".join(table.keys())
    pairs = [(float(n), table[u.lower()])
             for n, u in re.findall(r"([\d.]+)\s*(" + alt + r")", s, re.I)]
    if pairs:
        return sum(x * f for x, f in pairs) / len(pairs)
    if re.search(alt, s, re.I):
        nums = [float(x) for x in re.findall(r"[\d.]+", s)]
        if nums:
            return sum(nums) / len(nums)
    return None


_V = {"mv": 1e-3, "v": 1.0}
_A = {"pa": 1e-12, "na": 1e-9, _MICRO_U + "a": 1e-6, "ua": 1e-6,
      _MICRO_G + "a": 1e-6, "ma": 1e-3, "a": 1.0}
_F = {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0}
_BPS = {"gbps": 1e9, "mbps": 1e6, "kbps": 1e3, "bps": 1.0}
_RES = {"m\u03c9": 1e-3, "k\u03c9": 1e3, "\u03c9": 1.0}
_CAP = {"pf": 1e-12, "nf": 1e-9, _MICRO_U + "f": 1e-6, "uf": 1e-6,
        _MICRO_G + "f": 1e-6, "mf": 1e-3, "f": 1.0}
_IND = {_MICRO_U + "h": 1e-6, "uh": 1e-6, _MICRO_G + "h": 1e-6,
        "mh": 1e-3, "h": 1.0}
_PWR = {"mw": 1e-3, "kw": 1e3, "w": 1.0}


def _norm_voltage(s):
    return _norm(s, _V)


def _norm_current(s):
    return _norm(s, _A)


def _norm_freq(s):
    return _norm(s, _F)


def _norm_data_rate(s):
    return _norm(s, _BPS)


def _norm_resistance(s):
    if not s:
        return None
    s = str(s).replace("\u2126", "\u03c9").replace("\u03a9", "\u03c9")
    return _norm(s, _RES)


def _norm_capacitance(s):
    return _norm(s, _CAP)


def _norm_inductance(s):
    if not s:
        return None
    s = str(s).replace("\u2126", "")
    return _norm(s, _IND)


def _norm_power(s):
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*W", s, re.I)
    if m:
        return int(m.group(1)) / int(m.group(2))
    return _norm(s, _PWR)


def _num_first(s):
    """First numeric token in a string (handles '90dB' -> 90, '50ns' -> 50)."""
    if not s:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None


# Chinese-enum -> English translation (only applied to string enum values).
_ENUM = {
    "正": "Positive", "负": "Negative",
    "共阴": "Common Cathode", "共阳": "Common Anode",
    "单": "Single", "双": "Dual", "是": "Yes", "否": "No",
    "轨到轨": "Rail-to-Rail", "逻辑低": "Logic Low", "逻辑高": "Logic High",
    "增强型": "Enhancement", "耗尽型": "Depletion",
    "n沟道": "N-Channel", "p沟道": "P-Channel",
    "n-channel": "N-Channel", "p-channel": "P-Channel",
    "npn": "NPN", "pnp": "PNP",
}


def _enum(v):
    """Translate/clean a string enum value; drop non-ASCII we can't map."""
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    if v in _ENUM:
        return _ENUM[v]
    if v.isascii():
        return v
    return None


def _attrs(record):
    try:
        return json.loads(record.get("attributes_json") or "{}")
    except Exception:
        return {}


def _g(a, key):
    v = a.get(key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v)


# --------------------------------------------------------------------------
# assembly helper
# --------------------------------------------------------------------------
def _desc(parts):
    parts = [p for p in parts if p]
    d = " - ".join(parts).rstrip() + "."
    d = "".join(ch if ord(ch) < 128 else " " for ch in d).strip()
    d = d.rstrip(".").strip()
    return d


def _assemble(brand, mpn, category, subcategory, specs, desc_parts,
              applications, faq):
    aj = {}
    for k, v in specs.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.isascii():
            continue
        aj[k] = v
    description = _desc(desc_parts) or f"{brand} {mpn}"
    kw = "; ".join(str(p) for p in [mpn, subcategory, category] if p)
    kw = "".join(ch if ord(ch) < 128 else " " for ch in kw).strip()
    return {
        "category": category,
        "subcategory": subcategory or category,
        "description": description,
        "applications": applications,
        "keywords": kw,
        "attributes_json": json.dumps(aj, ensure_ascii=False),
        "faq": faq or "",
    }


def _faq(mpn, question, answer):
    if not answer:
        return ""
    return f"Q: {question}?A: {answer}"


# ==========================================================================
# adapter base
# ==========================================================================
class CategoryAdapter(ABC):
    canonical = ""        # overridden
    min_specs = 1         # per-adapter threshold replacing global SPEC_THIN<2

    @abstractmethod
    def build(self, record, mpn, brand):
        """Return the 7 category-shaped fields (as from _assemble)."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# MCU — reuses the validated 540-row logic verbatim (no behaviour change)
# --------------------------------------------------------------------------
class MCUAdapter(CategoryAdapter):
    canonical = "Microcontroller"
    min_specs = 2

    def build(self, record, mpn, brand):
        # Imported lazily to avoid any import cycle with product_data.
        from . import product_data as pd
        return pd.build_mcu_fields(record, mpn, brand)


# --------------------------------------------------------------------------
# Voltage Regulator
# --------------------------------------------------------------------------
class VoltageRegulatorAdapter(CategoryAdapter):
    canonical = "Voltage Regulator"
    min_specs = 1
    APPS = ("Power management; DC-DC conversion; LDO regulation; "
            "Battery-powered devices; Embedded systems")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        ot = _enum(_g(a, "输出类型"))
        if ot:
            specs["output_type"] = ot
        ov = _norm_voltage(_g(a, "输出电压"))
        if ov is not None:
            specs["output_voltage_v"] = round(ov, 4)
        pol = _enum(_g(a, "输出极性"))
        if pol:
            specs["polarity"] = pol
        oc = _norm_current(_g(a, "输出电流"))
        if oc is not None:
            specs["output_current_a"] = round(oc, 5)
        fn = _enum(_g(a, "功能类型"))
        if fn:
            specs["function"] = fn
        wv = _norm_voltage(_g(a, "工作电压"))
        if wv is not None:
            specs["working_voltage_v"] = round(wv, 4)
        sf = _norm_freq(_g(a, "开关频率"))
        if sf is not None:
            specs["switching_freq_hz"] = int(sf)
        ch = _num_first(_g(a, "通道数"))
        if ch is not None:
            specs["channels"] = int(ch)
        tol = _num_first(_g(a, "精度"))
        if tol is not None:
            specs["tolerance"] = tol
        iq = _norm_current(_g(a, "静态电流(Iq)"))
        if iq is not None:
            specs["iq_a"] = round(iq, 7)
        sub = ot or "Voltage Regulator"
        parts = [f"{brand} {mpn}", ot,
                 (f"{ov:.2f} V" if ov is not None else ""),
                 (f"{oc:.3f} A" if oc is not None else "")]
        faq = _faq(mpn, f"What is the output voltage of {mpn}",
                   (f"{mpn} delivers {ov:.2f} V" if ov is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Diode
# --------------------------------------------------------------------------
class DiodeAdapter(CategoryAdapter):
    canonical = "Diode"
    min_specs = 1
    APPS = ("Reverse protection; Rectification; Voltage clamping; "
            "ESD suppression; Power supplies")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        cfg = _enum(_g(a, "二极管配置"))
        if cfg:
            specs["config"] = cfg
        fc = _norm_current(_g(a, "整流电流"))
        if fc is not None:
            specs["forward_current_a"] = round(fc, 5)
        vf = _norm_voltage(_g(a, "正向压降(Vf)"))
        if vf is not None:
            specs["vf_v"] = round(vf, 4)
        vrwm = _norm_voltage(_g(a, "反向截止电压(Vrwm)"))
        if vrwm is not None:
            specs["vreverse_v"] = round(vrwm, 3)
        else:
            vr = (_norm_voltage(_g(a, "直流反向耐压（Vr）"))
                  or _norm_voltage(_g(a, "直流反向耐压(Vr)")))
            if vr is not None:
                specs["vreverse_v"] = round(vr, 3)
        clamp = _norm_voltage(_g(a, "钳位电压"))
        if clamp is not None:
            specs["clamp_v"] = round(clamp, 3)
        pol = _enum(_g(a, "极性"))
        if pol:
            specs["polarity"] = pol
        ppp = _num_first(_g(a, "峰值脉冲功率(Ppp)"))
        if ppp is not None:
            specs["ppp_w"] = int(ppp)
        vz = (_norm_voltage(_g(a, "稳压值(范围值)"))
              or _norm_voltage(_g(a, "稳压值(标称值)")))
        if vz is not None:
            specs["vzener_v"] = round(vz, 3)
        trr = (_num_first(_g(a, "反向恢复时间（trr）"))
               or _num_first(_g(a, "反向恢复时间(trr)")))
        if trr is not None:
            specs["trr_ns"] = int(trr)
        sub = cfg or "Diode"
        parts = [f"{brand} {mpn}", cfg,
                 (f"{vf:.2f} V Vf" if vf is not None else ""),
                 (f"{vrwm:.1f} V reverse" if vrwm is not None else "")]
        faq = _faq(mpn, f"What is the reverse voltage rating of {mpn}",
                   (f"{mpn} is rated for {vrwm:.1f} V reverse"
                    if vrwm is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Capacitor
# --------------------------------------------------------------------------
class CapacitorAdapter(CategoryAdapter):
    canonical = "Capacitor"
    min_specs = 1
    APPS = ("Decoupling; Filtering; Energy storage; Timing; Signal conditioning")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        cap = _norm_capacitance(_g(a, "容值"))
        if cap is not None:
            specs["capacitance"] = cap
        tol = _num_first(_g(a, "精度"))
        if tol is not None:
            specs["tolerance"] = tol
        rv = _norm_voltage(_g(a, "额定电压"))
        if rv is not None:
            specs["rated_voltage_v"] = round(rv, 3)
        tc = _enum(_g(a, "温度系数"))
        if tc:
            specs["temp_coef"] = tc
        esr = _norm_resistance(_g(a, "等效串联电阻(ESR)"))
        if esr is not None:
            specs["esr_ohm"] = round(esr, 6)
        ripple = _norm_current(_g(a, "纹波电流"))
        if ripple is not None:
            specs["ripple_current_a"] = round(ripple, 5)
        sub = (tc + " Capacitor") if tc else "Ceramic Capacitor"
        parts = [f"{brand} {mpn}",
                 (f"{cap} F" if cap is not None else ""),
                 (f"{rv:.1f} V" if rv is not None else ""),
                 (tc if tc else "")]
        faq = _faq(mpn, f"What is the capacitance of {mpn}",
                   (f"{mpn} is a {cap} F capacitor" if cap is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Interface IC
# --------------------------------------------------------------------------
class InterfaceICAdapter(CategoryAdapter):
    canonical = "Interface IC"
    min_specs = 1
    APPS = ("Signal translation; Bus interfacing; Level shifting; "
            "Industrial communication; Embedded I/O")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        wv = _norm_voltage(_g(a, "工作电压"))
        if wv is not None:
            specs["working_voltage_v"] = round(wv, 4)
        dr = _norm_data_rate(_g(a, "数据速率"))
        if dr is not None:
            specs["data_rate"] = int(dr)
        t = _enum(_g(a, "类型"))
        if t:
            specs["interface"] = t
        ec = _num_first(_g(a, "元件数"))
        if ec is not None:
            specs["elem_count"] = int(ec)
        bpe = _num_first(_g(a, "每个元件位数"))
        if bpe is not None:
            specs["bits_per_elem"] = int(bpe)
        it = _enum(_g(a, "输入类型"))
        if it:
            specs["input_type"] = it
        # I/O expanders etc. carry interface type + pin count under these keys
        itype = _enum(_g(a, "接口类型"))
        if itype:
            specs["interface"] = itype
        ioc = _num_first(_g(a, "I/O 数量"))
        if ioc is not None:
            specs["io_count"] = int(ioc)
        iq = _norm_current(_g(a, "静态电流(Iq)"))
        if iq is not None:
            specs["iq_a"] = round(iq, 7)
        nodes = _num_first(_g(a, "节点数"))
        if nodes is not None:
            specs["nodes"] = int(nodes)
        cmti = _num_first(_g(a, "CMTI(kV/us)"))
        if cmti is not None:
            specs["cmti_kvus"] = cmti
        sub = t or "Interface IC"
        parts = [f"{brand} {mpn}", t,
                 (f"{dr} bps" if dr is not None else ""),
                 (f"{wv:.2f} V" if wv is not None else "")]
        faq = _faq(mpn, f"What interface does {mpn} support",
                   (f"{mpn} is a {t} interface device" if t else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Operational Amplifier
# --------------------------------------------------------------------------
class OpAmpAdapter(CategoryAdapter):
    canonical = "Operational Amplifier"
    min_specs = 1
    APPS = ("Signal conditioning; Sensor amplification; Active filtering; "
            "Instrumentation; Control loops")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        na = _num_first(_g(a, "放大器数"))
        if na is not None:
            specs["num_amps"] = int(na)
        ib = _norm_current(_g(a, "输入偏置电流(Ib)"))
        if ib is not None:
            specs["ibias_a"] = round(ib, 9)
        cmrr = _num_first(_g(a, "共模抑制比(CMRR)"))
        if cmrr is not None:
            specs["cmrr_db"] = cmrr
        gbw = _norm_freq(_g(a, "增益带宽积(GBW)"))
        if gbw is not None:
            specs["gbw_hz"] = int(gbw)
        vos = _norm_voltage(_g(a, "输入失调电压(Vos)"))
        if vos is not None:
            specs["voffset_v"] = round(vos, 6)
        iq = _norm_current(_g(a, "静态电流(Iq)"))
        if iq is not None:
            specs["iq_a"] = round(iq, 7)
        # LNA / current-sense / generic amplifiers expose these under raw CN keys
        gain = _num_first(_g(a, "增益"))
        if gain is not None:
            specs["gain_db"] = gain
        freq = _norm_freq(_g(a, "频率"))
        if freq is not None:
            specs["frequency_hz"] = int(freq)
        sv = _norm_voltage(_g(a, "工作电压"))
        if sv is not None:
            specs["supply_v"] = round(sv, 4)
        icur = _norm_current(_g(a, "工作电流"))
        if icur is not None:
            specs["current_a"] = round(icur, 6)
        ocur = _norm_current(_g(a, "输出电流"))
        if ocur is not None:
            specs["output_current_a"] = round(ocur, 6)
        r2r = _enum(_g(a, "轨到轨"))
        if r2r:
            specs["rail_to_rail"] = r2r
        sub = (f"{int(na)}-Channel Op Amp" if na is not None else "Operational Amplifier")
        parts = [f"{brand} {mpn}",
                 (f"{int(na)}-channel" if na is not None else ""),
                 (f"GBW {gbw} Hz" if gbw is not None else "")]
        faq = _faq(mpn, f"What is the gain bandwidth product of {mpn}",
                   (f"{mpn} has a GBW of {gbw} Hz" if gbw is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# MOSFET
# --------------------------------------------------------------------------
class MOSFETAdapter(CategoryAdapter):
    canonical = "MOSFET"
    min_specs = 1
    APPS = ("Power switching; Motor drive; DC-DC; Load switching; "
            "Power management")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        vdss = _norm_voltage(_g(a, "漏源电压(Vdss)"))
        if vdss is not None:
            specs["vdss_v"] = round(vdss, 3)
        idc = _norm_current(_g(a, "连续漏极电流(Id)"))
        if idc is not None:
            specs["id_a"] = round(idc, 4)
        ct = _enum(_g(a, "类型"))
        if ct:
            specs["chan_type"] = ct
        pd = _norm_power(_g(a, "耗散功率(Pd)"))
        if pd is not None:
            specs["pd_w"] = round(pd, 5)
        vgsth = _norm_voltage(_g(a, "阈值电压(Vgs(th))"))
        if vgsth is not None:
            specs["vgs_th_v"] = round(vgsth, 4)
        rds = (_norm_resistance(_g(a, "导通电阻(RDS(on))"))
               or _norm_resistance(_g(a, "导通电阻")))
        if rds is not None:
            specs["rds_on_ohm"] = round(rds, 6)
        sub = (ct or "MOS") + " MOSFET"
        parts = [f"{brand} {mpn}", ct,
                 (f"{vdss:.1f} V" if vdss is not None else ""),
                 (f"{idc:.2f} A" if idc is not None else "")]
        faq = _faq(mpn, f"What is the drain-source voltage of {mpn}",
                   (f"{mpn} is rated for {vdss:.1f} V DS" if vdss is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Transistor (BJT / SCR)
# --------------------------------------------------------------------------
class TransistorAdapter(CategoryAdapter):
    canonical = "Transistor"
    min_specs = 1
    APPS = ("Amplification; Switching; Signal buffering; Linear regulation; "
            "Driver stages")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        ic = _norm_current(_g(a, "集电极电流(Ic)"))
        if ic is not None:
            specs["ic_a"] = round(ic, 4)
        tt = _enum(_g(a, "晶体管类型"))
        if tt:
            specs["tran_type"] = tt
        vceo = _norm_voltage(_g(a, "集射极击穿电压(Vceo)"))
        if vceo is not None:
            specs["vceo_v"] = round(vceo, 3)
        st = _enum(_g(a, "可控硅类型"))
        if st:
            specs["scr_type"] = st
        igt = _num_first(_g(a, "门极触发电流(Igt)"))
        if igt is not None:
            specs["igt_ma"] = igt
        sub = (tt or "BJT") + " Transistor"
        parts = [f"{brand} {mpn}", tt,
                 (f"{vceo:.1f} V" if vceo is not None else ""),
                 (f"{ic:.2f} A" if ic is not None else "")]
        faq = _faq(mpn, f"What is the collector current of {mpn}",
                   (f"{mpn} handles {ic:.2f} A collector" if ic is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Logic IC
# --------------------------------------------------------------------------
class LogicICAdapter(CategoryAdapter):
    canonical = "Logic IC"
    min_specs = 1
    APPS = ("Digital logic; Glue logic; Signal buffering; Level translation; "
            "Combinational logic")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        sv = _norm_voltage(_g(a, "工作电压"))
        if sv is not None:
            specs["supply_v"] = round(sv, 4)
        iol = _norm_current(_g(a, "灌电流(IOL)"))
        if iol is not None:
            specs["iol_a"] = round(iol, 5)
        tpd = _num_first(_g(a, "传播延迟(tpd)"))
        if tpd is not None:
            specs["tpd_ns"] = tpd
        ioh = _norm_current(_g(a, "拉电流(IOH)"))
        if ioh is not None:
            specs["ioh_a"] = round(ioh, 5)
        iq = _norm_current(_g(a, "静态电流(Iq)"))
        if iq is not None:
            specs["iq_a"] = round(iq, 7)
        fn = _enum(_g(a, "功能"))
        if fn:
            specs["function"] = fn
        gates = _num_first(_g(a, "逻辑单元数"))
        if gates is not None:
            specs["gates"] = int(gates)
        sub = fn or "Logic IC"
        parts = [f"{brand} {mpn}", fn,
                 (f"{sv:.2f} V" if sv is not None else ""),
                 (f"{tpd} ns propagation" if tpd is not None else "")]
        faq = _faq(mpn, f"What supply voltage does {mpn} use",
                   (f"{mpn} operates at {sv:.2f} V" if sv is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Resistor
# --------------------------------------------------------------------------
class ResistorAdapter(CategoryAdapter):
    canonical = "Resistor"
    min_specs = 1
    APPS = ("Current limiting; Voltage division; Pull-up/down; "
            "Termination; Sensing")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        r = _norm_resistance(_g(a, "阻值"))
        if r is not None:
            specs["resistance_ohm"] = r
        tol = _num_first(_g(a, "精度"))
        if tol is not None:
            specs["tolerance"] = tol
        rt = _enum(_g(a, "电阻类型"))
        if rt:
            specs["rtype"] = rt
        mv = _norm_voltage(_g(a, "最大工作电压"))
        if mv is not None:
            specs["max_voltage_v"] = round(mv, 3)
        p = _norm_power(_g(a, "功率"))
        if p is not None:
            specs["power_w"] = round(p, 5)
        sub = (rt or "Chip") + " Resistor"
        parts = [f"{brand} {mpn}",
                 (f"{r} \u03a9" if r is not None else ""),
                 (f"{mv:.1f} V" if mv is not None else ""),
                 (f"{p} W" if p is not None else "")]
        faq = _faq(mpn, f"What is the resistance of {mpn}",
                   (f"{mpn} is a {r} ohm resistor" if r is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# --------------------------------------------------------------------------
# Inductor
# --------------------------------------------------------------------------
class InductorAdapter(CategoryAdapter):
    canonical = "Inductor"
    min_specs = 1
    APPS = ("Power filtering; Energy storage; EMI suppression; "
            "DC-DC; RF chokes")

    def build(self, record, mpn, brand):
        a = _attrs(record)
        specs = {}
        L = _norm_inductance(_g(a, "电感值"))
        if L is not None:
            specs["inductance_h"] = L
        tol = _num_first(_g(a, "精度"))
        if tol is not None:
            specs["tolerance"] = tol
        rc = _norm_current(_g(a, "额定电流"))
        if rc is not None:
            specs["rated_current_a"] = round(rc, 4)
        # Common-mode / EMI filters expose impedance + line count, not L
        z = _norm_resistance(_g(a, "阻抗@频率"))
        if z is not None:
            specs["impedance_ohm"] = round(z, 3)
        lines = _num_first(_g(a, "线路数"))
        if lines is not None:
            specs["lines"] = int(lines)
        isat = _norm_current(_g(a, "饱和电流(Isat)"))
        if isat is not None:
            specs["isat_a"] = round(isat, 4)
        dcr = _norm_resistance(_g(a, "直流电阻(DCR)"))
        if dcr is not None:
            specs["dcr_ohm"] = round(dcr, 6)
        sub = "Power Inductor"
        parts = [f"{brand} {mpn}",
                 (f"{L} H" if L is not None else ""),
                 (f"{rc:.3f} A rated" if rc is not None else "")]
        faq = _faq(mpn, f"What is the inductance of {mpn}",
                   (f"{mpn} is a {L} H inductor" if L is not None else ""))
        return _assemble(brand, mpn, self.canonical, sub, specs, parts,
                         self.APPS, faq)


# ==========================================================================
# registry
# ==========================================================================
REGISTRY = {
    a.canonical: a for a in (
        MCUAdapter(), VoltageRegulatorAdapter(), DiodeAdapter(),
        CapacitorAdapter(), InterfaceICAdapter(), OpAmpAdapter(),
        MOSFETAdapter(), TransistorAdapter(), LogicICAdapter(),
        ResistorAdapter(), InductorAdapter(),
    )
}


# ==========================================================================
# detection pipeline (4 levels; never falls back to MCU)
# ==========================================================================
# L2: RAW `category` column -> canonical (only the 11 we implement)
_CATEGORY_TO_CANON = {
    "microcontroller": "Microcontroller",
    "voltage regulator": "Voltage Regulator",
    "diode": "Diode",
    "capacitor": "Capacitor",
    "interface ic": "Interface IC",
    "operational amplifier": "Operational Amplifier",
    "mosfet": "MOSFET",
    "transistor": "Transistor",
    "logic ic": "Logic IC",
    "resistor": "Resistor",
    "inductor": "Inductor",
}

# L1: catalogName substring patterns -> canonical (ordered specific -> general)
_CATALOG_PATTERNS = [
    (r"microcontroller", "Microcontroller"),
    (r"ldo|low drop|linear regulator|voltage regulator|dc-?dc|buck|boost|"
     r"step-?down|step-?up|voltage reference|power mux|power multiplexer",
     "Voltage Regulator"),
    (r"schottky|zener|tvs|esd|rectifier|diode|oring|ideal diode", "Diode"),
    (r"mlcc|ceramic capacitor|tantalum|aluminum capacitor|capacitor",
     "Capacitor"),
    (r"operational amplifier|op-?amp|comparator|precision op",
     "Operational Amplifier"),
    (r"power inductor|inductor|choke|common mode", "Inductor"),
    (r"chip resistor|resistor|resistance", "Resistor"),
    (r"mosfet", "MOSFET"),
    (r"bipolar|bjt|transistor|scr|thyristor", "Transistor"),
    (r"logic gate|logic ic", "Logic IC"),
    (r"buffer|transceiver|translator|level shift|"
     r"i/?o expander|rs-?232|rs-?485|rs-?422|can transceiver|uart|"
     r"driver|expander|interface",
     "Interface IC"),
]

# L3: description keyword patterns -> canonical
_DESC_PATTERNS = [
    (r"ideal diode|diode controller|schottky|zener|tvs", "Diode"),
    (r"microcontroller|cortex|arm m|avr|pic|\b8051\b", "Microcontroller"),
    (r"ldo|voltage regulator|dc-?dc|buck|boost|linear regulator",
     "Voltage Regulator"),
    (r"ceramic capacitor|mlcc|capacitor", "Capacitor"),
    (r"operational amplifier|op-?amp|comparator", "Operational Amplifier"),
    (r"inductor|choke|common mode", "Inductor"),
    (r"resistor", "Resistor"),
    (r"transistor|bjt|\bnpn\b|\bpnp\b", "Transistor"),
    (r"mosfet", "MOSFET"),
    (r"level shifter|translator|i/?o expander|rs-?232|rs-?485|rs-?422|"
     r"can transceiver|uart", "Interface IC"),
    (r"logic gate|buffer|transceiver", "Logic IC"),
]

# L4: attributes_json key fingerprint -> canonical
_ATTR_FINGERPRINT = [
    ("CPU内核", "Microcontroller"),
    ("容值", "Capacitor"),
    ("漏源电压(Vdss)", "MOSFET"),
    ("整流电流", "Diode"),
    ("二极管配置", "Diode"),
    ("增益带宽积(GBW)", "Operational Amplifier"),
    ("放大器数", "Operational Amplifier"),
    ("电感值", "Inductor"),
    ("阻值", "Resistor"),
    ("集电极电流(Ic)", "Transistor"),
    ("晶体管类型", "Transistor"),
    ("灌电流(IOL)", "Logic IC"),
    ("传播延迟(tpd)", "Logic IC"),
    ("数据速率", "Interface IC"),
    ("输出类型", "Voltage Regulator"),
]


def detect_category(record):
    """Return (canonical, signals_dict, confidence).

    canonical is one of REGISTRY keys, or UNKNOWN_CATEGORY when nothing
    confident matched. NEVER Microcontroller-as-default.
    """
    catname = (record.get("catalogName") or "").strip().lower()
    cat = (record.get("category") or "").strip().lower()
    desc = (record.get("description") or "").strip().lower()

    # L1 — catalogName substring
    for pat, canon in _CATALOG_PATTERNS:
        if re.search(pat, catname):
            return canon, {"level": "L1_catalogName",
                           "value": record.get("catalogName") or ""}, "high"
    # L2 — category column
    if cat in _CATEGORY_TO_CANON:
        return _CATEGORY_TO_CANON[cat], {"level": "L2_category",
                                         "value": record.get("category") or ""}, "high"
    # L3 — description keywords
    for pat, canon in _DESC_PATTERNS:
        if re.search(pat, desc):
            return canon, {"level": "L3_description",
                           "value": (record.get("description") or "")[:60]}, "medium"
    # L4 — attributes_json key fingerprint
    a = _attrs(record)
    for key, canon in _ATTR_FINGERPRINT:
        if key in a:
            return canon, {"level": "L4_attributes", "value": key}, "medium"
    # nothing matched
    return UNKNOWN_CATEGORY, {"level": "none",
                              "value": (record.get("catalogName") or "")
                              or (record.get("category") or "")}, "none"


# ==========================================================================
# public entry point
# ==========================================================================
def _unknown_fields(record, mpn, brand):
    desc = (record.get("description") or "").strip()
    desc = "".join(ch if ord(ch) < 128 else " " for ch in desc).strip()
    if not desc:
        desc = f"{brand} {mpn}"
    return {
        "category": UNKNOWN_CATEGORY,
        "subcategory": UNKNOWN_CATEGORY,
        "description": desc,
        "applications": "",
        "keywords": mpn,
        "attributes_json": "{}",
        "faq": "",
    }


def build_category_row(record, mpn, brand):
    """Return (fields_dict, meta_dict).

    fields_dict has the 7 category-shaped keys. meta carries the detected
    category, confidence, signals and a ``needs_review`` flag for unmapped
    rows (so a later release gate can hold them).
    """
    canon, signals, confidence = detect_category(record)
    meta = {"category": canon, "confidence": confidence,
            "signals": signals, "needs_review": False}
    if canon == UNKNOWN_CATEGORY or canon not in REGISTRY:
        meta["needs_review"] = True
        return _unknown_fields(record, mpn, brand), meta
    adapter = REGISTRY[canon]
    meta["min_specs"] = adapter.min_specs
    return adapter.build(record, mpn, brand), meta


def supported_canonicals():
    return list(REGISTRY.keys())
