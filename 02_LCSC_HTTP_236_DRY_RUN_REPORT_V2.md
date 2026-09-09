# 02 LCSC HTTP RAW 适配器 — 236 条隔离 Dry-run 报告 (V2)

- 生成时间: 2026-09-10T06:41:38
- 批次: `http236`  源: `data/raw/lcsc_http_scale500`  (01 V1 FROZEN 产出, 只读)
- 隔离 root: `data/raw/_clean_preview/http236/`  (未写 MASTER / CLEAN 生产 / 03/04)
- 本轮修正: ① 合法纯数字 MPN 放行  ② `_detect_by_en_attrs` 启用(强化三档指纹)

## 0. 本轮修正摘要

- **修正① 纯数字 MPN**: `looks_synthetic` 改为 source-aware。对 `lcsc_http_json` 来源的纯数字 MPN，仅在缺乏真实产品身份(品牌 + C-number + 产品名)时才判 synthetic；具备真实身份则放行。
- **修正② `_detect_by_en_attrs`**: 由「仅 canonical 键」扩展为三档高置信指纹 —— ① canonical spec 键(新增 `topology`→Voltage Regulator) ② unmapped 英文键(如 `Impedance @ Frequency`→Inductor) ③ 描述关键词(如 `ferrite bead`→Inductor, `dc dc switching`→Voltage Regulator)。每条命中记录 `classification_source` / `matched_keys` / `reason`。
- **原则**: 宁可 Uncategorized，也不要错误分类。低置信 / 无 11 家族映射的记录保持 Uncategorized（人工复核），不强行分类。

## 1. V1 -> V2 对比表

| 指标 | V1 | V2 | 变化 |
|---|---:|---:|---:|
| Input | 236 | 236 | 0 |
| Cleaned | 234 | 236 | +2 |
| Candidates | 198 | 200 | +2 |
| Rejected | 2 | 0 | -2 |
| Duplicates | 36 | 36 | 0 |
| Uncategorized | 47 | 46 | -1 |
| Uncategorized after EN detection | 47 | 46 | -1 |
| Synthetic MPN rejected | 2 | 0 | -2 |
| Synthetic MPN accepted | 0 | 2 | +2 |
| Canonical spec (含>=1) | 138 | 141 | +3 |
| Unmapped attributes (保留) | 163 | 165 | +2 |
| Alternative parts | 142 | 144 | +2 |
| Related parts | 142 | 144 | +2 |
| Applications | 151 | 154 | +3 |
| FAQ | 85 | 85 | 0 |
| CJK leak | 0 | 0 | 0 |
| Internal commercial leak | 0 | 0 | 0 |

## 2. 流水线结果 (Quality Gate)

| 指标 | 值 |
|---|---|
| intake 输入 | 236 |
| intake 写入 | 236 |
| intake 源内自重复 | 0 |
| normalize 输入 | 236 |
| 净清洗 | 236 |
| 候选 | 200 |
| 拒绝 | 0 |
| 与 MASTER 重复 | 36 |
| **CJK_LEAK (STOP)** | **0** |
| 流水线 stop | False |

**断言**: `CJK_LEAK == 0` ✅ 通过。

## 3. 异常码分布

| code | count | severity |
|---|---|---|
| UNMAPPED_CATEGORY | 53 | WARNING |
| DUPLICATE_SKIP | 36 | AUTO_SKIP |
| BRAND_UNMAPPED | 34 | WARNING |
| SPEC_THIN | 25 | WARNING |

## 4. 分类分布

| category | candidates |
|---|---|
| Interface IC | 50 |
| Uncategorized | 46 |
| Voltage Regulator | 23 |
| Microcontroller | 19 |
| Capacitor | 18 |
| Diode | 11 |
| Transistor | 9 |
| Inductor | 7 |
| MOSFET | 6 |
| Resistor | 5 |
| Logic IC | 4 |
| Operational Amplifier | 2 |

## 5. 规范属性键覆盖

- 含 ≥1 规范 spec 的候选: 141/200
| canonical spec key | 出现次数 |
|---|---|
| working_voltage_v | 56 |
| iq_a | 45 |
| tolerance | 31 |
| frequency_hz | 19 |
| output_voltage_v | 19 |
| output_current_a | 19 |
| capacitance | 18 |
| rated_voltage_v | 18 |
| output_type | 17 |
| channels | 15 |
| interface | 12 |
| data_rate | 12 |
| temp_coef | 10 |
| switching_freq_hz | 9 |
| vreverse_v | 9 |
| io_count | 8 |
| function | 8 |
| config | 8 |
| core | 7 |
| core_bits | 7 |
| flash_bytes | 7 |
| flash_type | 7 |
| adc_bits | 7 |
| oscillator_type | 7 |
| dcr_ohm | 7 |
| forward_current_a | 7 |
| vf_v | 7 |
| vdss_v | 6 |
| id_a | 6 |
| chan_type | 6 |
| pd_w | 6 |
| rds_on_ohm | 6 |
| gate_charge_c | 6 |
| resistance_ohm | 5 |
| max_voltage_v | 5 |
| power_w | 5 |
| ripple_current_a | 5 |
| inductance_h | 5 |
| supply_v | 4 |
| tpd_ns | 4 |

## 6. 未映射属性完整保留

- 保留未映射项的候选: 165/200
- 未映射键总数: 750
| paramNameEn | 出现次数 |
|---|---|
| Ib - Input Bias Current | 14 |
| Current - Output High(IOH) | 12 |
| Current Rating | 12 |
| Input Offset Current(Ios) | 12 |
| Current - Output Low(IOL) | 11 |
| Number | 9 |
| Output Configuration | 9 |
| Vce Saturation(VCE(sat)) | 8 |
| Memory Size | 8 |
| Quiescent Current (Iq) | 7 |
| Flame Retardant Rating | 7 |
| Plastic Material | 7 |
| Current - Collector(Ic) | 6 |
| type | 6 |
| Input Logic Level - High | 6 |
| Input Logic Level - Low | 6 |
| Quiescent Current(Iq) | 6 |
| output type | 6 |
| Current - Supply | 6 |
| Voltage - Forward(Vf) | 6 |
| number of channels | 6 |
| Data Retention - TDR (Year) | 6 |
| Write Cycle Time(tWC) | 6 |
| Standby Supply Current | 6 |
| Connector Type | 6 |
| Switch tube (built-in/external) | 6 |
| Gate Threshold Voltage (Vgs(th)) | 6 |
| Reverse Transfer Capacitance (Crss@Vds) | 6 |
| Input Capacitance(Ciss) | 6 |
| Configuration | 5 |
| Integral non - linearity | 5 |
| Lifetime | 5 |
| Height - Seated (Max) | 5 |
| Diameter | 5 |
| Voltage Rating (Max) | 5 |
| Number of PINs | 5 |
| Pin Structure | 5 |
| Mounting Type | 5 |
| Collector - Emitter Voltage VCEO | 4 |
| Standby Current | 4 |

## 7. 关系 / 应用 / FAQ 保留

| 字段 | 有内容的候选数 |
|---|---|
| alternative_parts | 144 |
| related_parts_raw | 144 |
| applications | 154 |
| faq | 85 |

## 8. 内部商业字段泄漏扫描

✅ 0 处 — 候选池不含 warehouseCode/productBatchCode/cost/purchasePrice/profit/grossProfit/supplierCost/activityPO/szlcscActivityPO/dollarLadderPrice/foreignWeight/real_time_snapshot/internal_raw/authenticationList/edaSvgInfo/flashSaleProductPO/productWeight。

## 9. 修正① 专项 — 纯数字 MPN 验证

| MPN | 状态 | 依据 |
|---|---|---|
| `5023520200` | **ACCEPTED** | category=Uncategorized; specs=0 |
| `1054500101` | **ACCEPTED** | category=Uncategorized; specs=0 |

结论: 两个纯数字 MPN 均具备真实产品身份(LCSC C-number + 品牌 + 产品名)，已从 synthetic 护栏误杀中恢复并进入候选。

## 10. 修正② 专项 — Uncategorized 重分类审计

- V1 Uncategorized: **47**
- V2 Uncategorized: **46**
- 被 `_detect_by_en_attrs` 合理重分类: **3**
- 仍保持 Uncategorized(人工复核): **44**

### 10a. 重分类清单 (原 Uncategorized -> 新分类 -> 依据)

| MPN | 新分类 | classification_source | matched_keys | reason |
|---|---|---|---|---|
| `MPZ1608S101ATAH0` | Inductor | en_attr_fingerprint | ['impedance @ frequency'] | unmapped attr 'impedance @ frequency' |
| `MPZ2012S601AT000` | Inductor | en_attr_fingerprint | ['impedance @ frequency'] | unmapped attr 'impedance @ frequency' |
| `UC2845BD1013TR` | Voltage Regulator | en_attr_fingerprint | ['topology'] | canonical attr 'topology' |

### 10b. 仍 Uncategorized 清单 (保留, 不强行分类)

| MPN | 推断类型(仅报告用) | 描述片段 |
|---|---|---|
| `170325-1` | Connector (no supported 11-family mapping) | Positive Lock Mark II Rec - Quick Connects, Quick Disconnect Connector |
| `172337-1` | Connector (no supported 11-family mapping) | CONN HOUSING 3POS 4.2mm SINGLE ROW - Connector Housing 3 Position 4.2m |
| `177914-1` | Connector (no supported 11-family mapping) | CONN TERM CRIMP TIN - Connector Terminal Tin Crimp |
| `177916-1` | Connector (no supported 11-family mapping) | 0.14~0.34 22~26 Tin Copper alloy Rectangular Connector Contacts RoHS |
| `1827572-2` | Connector (no supported 11-family mapping) | CONN TERM CRIMP GOLD - Connector Terminal Gold Crimp |
| `2177526-4` | Connector (no supported 11-family mapping) | Rectangular Connector Accessories RoHS |
| `2298494-1` | Connector (no supported 11-family mapping) | Coaxial Connector (RF) Contacts |
| `2312110-1` | Connector (no supported 11-family mapping) | Locking Rectangular Connector Housings |
| `2356607-1` | Connector (no supported 11-family mapping) | Rectangular Connector Housings RoHS |
| `2404653-1` | Connector (no supported 11-family mapping) | 2 Automotive Connector Housing 4P Rectangular Connector Housings |
| `3-640443-2` | unclear / no high-confidence 11-family signal | 1x2P 250V 1 2 2.54mm Free Hanging 5A P=2.54mm Free Hanging, Panel Moun |
| `ADG508FBRNZ-REEL7` | Switch / tactile (no supported 11-family mapping) | Fault-Protected Analog Multiplexers ADG508F/ADG509F - 1 -15V~15V 390Oh |
| `AT42QT2120-XUR` | Capacitive-touch IC (no supported 11-family mapping) | Capacitive touch key solution supporting communication and independent |
| `AXK680337YG` | Connector (no supported 11-family mapping) | Narrow pitch connectors [For board-to-board] - nan |
| `B5B-PH-K-S(LF)(SN)` | Connector (no supported 11-family mapping) | CONN HEADER TH 5POS 2mm - Connector Header 5 position 2mm Pitch 2A Thr |
| `DS3231MZ+TRL` | Real-Time Clock (no supported 11-family mapping) | +/-5ppm, I2C Real-Time Clock - Built-in Yes SOIC-8 Real Time Clocks Ro |
| `HDGC2001WR-S-2P` | Connector (no supported 11-family mapping) | CONN HEADER SMD R/A 2POS 2mm - Connector Header 2 position 2mm Pitch S |
| `HFCG-2500+` | RF filter (no supported 11-family mapping) | LTCC SMT High Pass Filter - 50Ohm 1.5dB SMD-6P,2x1.3mm RF Filters RoHS |
| `ICM-45686` | MEMS sensor / IMU (no supported 11-family mapping) | High Performance Dual-Interface (UI + AUX) 6-Axis MEMS MotionTracking  |
| `ICSRC6508SFR` | Connector (no supported 11-family mapping) | RFI Shield Clip 6.5mm x 1.21mm - RFI and EMI - Contacts, Fingerstock a |
| `ISO7241CDWR` | Digital Isolator (no supported 11-family mapping) | High-Speed, Quad-Channel Digital Isolators - Digital Isolator 2500Vrms |
| `IXBX25N250` | IGBT (no supported 11-family mapping) | IGBT 2.5kV 55A PLUS-247 - IGBT 2.5kV 55A 300W PLUS-247 |
| `IXYT25N250CHV` | IGBT (no supported 11-family mapping) | IGBT 2.5kV 95A TO-268 - IGBT 2.5kV 95A 937W Surface Mount TO-268 |
| `JST24A-800BW` | TRIAC / Thyristor (no supported 11-family mapping) | 25A TRIACS - 75mA 50mA 800V TO-220A TRIACs RoHS |
| `KH-6X6X4.3H-STM` | Switch / tactile (no supported 11-family mapping) | SWITCH TACTILE SPST SMD - Tactile Switch SPST 4.3mm Gull Wing 6mm x 6m |
| `LM5069MM-2/NOPB` | Hot-swap / power controller (ambiguous -> kept Uncategorized) | Positive High-Voltage Hot Swap and In-Rush Current Controller With Pow |
| `LTC1668IG#PBF` | DAC / data converter (no supported 11-family mapping) | 12-Bit,14-Bit,16-Bit,50Msps DACs - 20ns 4.75V~5.25V 8LSB Parallel SSOP |
| `MASW-007107-TR3000` | Switch / tactile (no supported 11-family mapping) | GaAsBroadbandSPDTSwitch - 8GHz DFN-8-EP(2x2) RF Switches RoHS |
| `MAX1968EUI+T` | Power-management IC (ambiguous -> kept Uncategorized) | HTSSOP-28-EP-4.5mm Power Management - Specialized RoHS |
| `MCP4726A0T-E/CH` | DAC / data converter (no supported 11-family mapping) | 8/10/12-Bit Voltage Output Digital-to-Analog Converter with EEPROM and |
| `MF-MSMF010-2` | Fuse / PTC (no supported 11-family mapping) | PTC RESET FUSE 60V 1812 - 60V 100mA 40A 300mA 700mOhm 1812 PTC Resetta |
| `MIC2774N-29YM5-TR` | Voltage supervisor (ambiguous -> kept Uncategorized) | 140ms Manual reset input Active Low 1.5V~5.5V 2 SOT-23-5 Supervisors R |
| `PHR-3` | Connector (no supported 11-family mapping) | CONN HOUSING 3POS 2mm SINGLE ROW PH - Connector Housing 3 Position 2mm |
| `PHR-4` | Connector (no supported 11-family mapping) | CONN HOUSING 4POS 2mm SINGLE ROW PH - Connector Housing 4 Position 2mm |
| `QCN-25+` | RF power splitter (no supported 11-family mapping) | Ultra-Small Ceramic 2-Way 90deg Power Splitter/Combiner - 1.35GHz~2.45 |
| `S4B-PH-SM4-TB(LF)(SN)` | Connector (no supported 11-family mapping) | CONN HEADER SMD R/A 4POS 2mm - Connector Header 4 position 2mm Pitch 2 |
| `SDINBDG4-8G-ZAT` | Memory (no supported 11-family mapping) | Memory RoHS |
| `SKRKAEE020` | Switch / tactile (no supported 11-family mapping) | SWITCH TACTILE SPST SMD - Tactile Switch SPST 2mm 3.9mm x 2.9mm Surfac |
| `SM06B-GHS-TB(LF)(SN)` | Connector (no supported 11-family mapping) | CONN HEADER SMD R/A 6POS 1.25mm - Connector Header 6 position 1.25mm P |
| `TMP302ADRLR` | Thermostat / sensor (no supported 11-family mapping) | 65degC SOT-563 Thermostats - Solid State RoHS |
| `TMS320C6748EZWTD4` | DSP (no supported 11-family mapping) | Fixed- and Floating-Point VLIW DSP - Fixed-point / floating-point 456M |
| `TPS3897ADRYR` | Voltage supervisor (ambiguous -> kept Uncategorized) | Ultra-Small Single-Channel Adjustable Voltage Monitor - 40us Active Hi |
| `TSA016A2518C` | Switch / tactile (no supported 11-family mapping) | SWITCH TACTILE 180gf SMD - Tactile Switch 180gf 2.5mm 4.2mm x 3.35mm S |
| `W25Q40EWUXIE` | Memory (no supported 11-family mapping) | USON-8(2x3) Memory |

## 11. 错误分类检查 (§十四 Q3)

- 重分类 3 条均基于 UNMISTAKABLE 高置信信号: `UC2845BD1013TR`(topology=switching -> Voltage Regulator, 且含 output_current_a/working_voltage_v 实际规格); `MPZ1608S101ATAH0` / `MPZ2012S601AT000`(ferrite bead -> Inductor, 含 dcr_ohm/lines/tolerance 实际规格)。
- 其余 44 条均为 Connector / Switch / Fuse / Memory / RF / Sensor / IGBT / TRIAC / DSP / DAC / Isolator / RTC 等, 不在 11 个受支持家族内, 或强制分类会 触发 SPEC_THIN 拒绝(比 held-for-review 更差)。按「宁可 Uncategorized」原则保留。
- **未出现错误分类。**

## 12. 非预期行为检查 (§十四 Q9)

- candidate 变化: 198 -> 200 (+2, 完全来自 2 个纯数字 MPN 由 rejected 转为 candidate)。3 条重分类不改变 candidate 总数, 仅把 Uncategorized 中的 3 条移到具体家族(V1 47 -> V2 46 Uncategorized)。与预期一致。
- SPEC_THIN 变化: V1=25 (异常码见 §3, 无新增异常类型)。
- CJK leak / 内部泄漏 仍为 0; canonical spec / unmapped 保留完整; 分类分布仅因 3 条重分类发生预期内微调, 无其它行为变化。

## 13. 最终状态块

```
02 ADAPTER:  UPDATED
02 DRY-RUN:  V2 COMPLETE

INPUT:  236
SOURCE: data/raw/lcsc_http_scale500

01:          FROZEN
RAW:         UNCHANGED
CLEAN PRODUCTION: UNCHANGED
MASTER:      UNCHANGED
03:          UNCHANGED
04:          UNCHANGED
WEBSITE:     UNCHANGED

COMMIT:      NO
PUSH:        NO
DEPLOY:      NO
```

_报告由 `tools/_http236_dryrun_v2.py` 自动生成; 候选池落点: `data/raw/_clean_preview/http236\products/candidates\http236.json`_