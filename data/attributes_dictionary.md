# SZ Procure — Attributes Dictionary (属性字典) v1

> **状态：FROZEN（冻结）** ｜ 日期：2026-08-26 ｜ 配套：`mfr_canonical.csv` + `Product_DB_Schema_v1_Final.md`
> 目的：保证未来 20 万 SKU 的结构化参数**统一、可机器筛选、可 AI 检索**。
> 作用域：`attributes_json` 字段内所有 key 必须来自本字典，未知 key 禁止静默入库。

---

## 1. 设计原则（铁律）

1. **一个物理概念 = 唯一 key**。禁止同一概念出现多个字段名。
2. **key 命名**：全小写 `snake_case`，**必须带单位后缀**（见 §3）。
3. **数值统一用基准单位**（Hz / V / A / bytes / Ω / pF / µH / ns / mΩ / µA / nC / dBm / nm / mm / W / bps / dB / %）。
4. **未知 key 禁止静默入库** → 标记 `needs_review`，人工周清（对应 P0-3）。
5. **文本类**（package / interface / modulation / core / mounting 等）`type=string`，无单位。

---

## 2. 禁止的同概念多字段（必须统一为 canonical key）

| 禁止的杂乱写法 | 统一 canonical key |
|----------------|-------------------|
| `frequency` / `freq` / `clock` / `speed` | `frequency_hz` |
| `64KB` / `64K Flash` / `65536 Bytes` / `64K` | `flash_bytes` |
| `RAM 64K` / `65536` / `64KB` | `ram_bytes` |
| `resistance` / `ohm` / `R` / `res` | `resistance_ohm` |
| `capacitance` / `cap` / `100n` / `0.1u` | `capacitance_pf` |
| `inductance` / `ind` | `inductance_uh` |
| `voltage` / `vcc` / `vdd` / `vin` / `vout`（泛指） | 用具体 `voltage_v` / `voltage_in_*` / `voltage_out_v` |
| `current` / `i` / `amp`（泛指） | 用具体 `*_a` / `*_ma` / `*_ua` |
| `gain` / `av` | `gain_db` |
| `temp` / `temperature` | `operating_temp_c`（string 范围，如 `-40~85`） |

---

## 3. 单位后缀命名约定

| 后缀 | 单位 | 后缀 | 单位 |
|------|------|------|------|
| `_hz` | Hz（频率） | `_a` | A（安培） |
| `_v` | V（伏） | `_ma` | mA（毫安） |
| `_bytes` | bytes（字节） | `_ua` | µA（微安） |
| `_ohm` | Ω（欧姆） | `_mohm` | mΩ（毫欧） |
| `_pf` | pF（皮法） | `_nc` | nC（纳库） |
| `_uh` | µH（微亨） | `_ns` | ns（纳秒） |
| `_db` | dB（分贝） | `_dbm` | dBm |
| `_nm` | nm（纳米） | `_mm` | mm（毫米） |
| `_w` | W（瓦） | `_bps` | bps（比特/秒） |
| `_percent` | %（百分比） | — | — |

---

## 4. 属性字典（key 清单）

> `category` 列标注适用 L2（或"通用"）。新增 key 须先加入本字典再使用。

| key | type | unit | category（适用 L2） | example | 说明 |
|-----|------|------|---------------------|---------|------|
| `frequency_hz` | integer | Hz | Microcontrollers, RF & Wireless | `72000000` | 主时钟 / 射频频率 |
| `flash_bytes` | integer | bytes | Microcontrollers, Memory | `131072` | 片内 Flash（128KB） |
| `ram_bytes` | integer | bytes | Microcontrollers | `20480` | SRAM |
| `core` | string | — | Microcontrollers | `ARM Cortex-M4` | 内核架构 |
| `io_count` | integer | pins | Microcontrollers, Logic IC | `48` | GPIO 数 |
| `voltage_v` | number | V | 通用 | `3.3` | 工作电压（通用） |
| `voltage_in_min_v` | number | V | Power Management | `4.5` | 输入最低 |
| `voltage_in_max_v` | number | V | Power Management | `28` | 输入最高 |
| `voltage_out_v` | number | V | Power Management | `5` | 输出 |
| `output_current_a` | number | A | Power Management | `3` | 输出电流 |
| `quiescent_current_ua` | integer | µA | Power Management | `50` | 静态电流 |
| `dropout_v` | number | V | Power Management | `0.3` | LDO 压降 |
| `efficiency_percent` | integer | % | Power Management | `92` | 转换效率 |
| `gain_db` | number | dB | Analog IC | `100` | 开环增益 |
| `bandwidth_hz` | number | Hz | Analog IC | `1000000` | 带宽 |
| `offset_v` | number | V | Analog IC | `0.001` | 输入失调 |
| `supply_current_ua` | integer | µA | Analog IC | `200` | 供电电流 |
| `slew_rate_vus` | number | V/µs | Analog IC | `0.5` | 压摆率 |
| `memory_bytes` | integer | bytes | Memory | `16777216` | 容量（16MB） |
| `interface` | string | — | Memory, Sensors, Modules, RF & Wireless | `SPI` | 总线接口 |
| `speed_hz` | number | Hz | Memory | `104000000` | 存储时钟 |
| `organization` | string | — | Memory | `x8` | 位宽组织 |
| `vds_v` | number | V | MOSFET | `55` | 漏源耐压 |
| `rds_on_mohm` | number | mΩ | MOSFET | `8` | 导通电阻 |
| `id_a` | number | A | MOSFET, Connectors, Electromechanical | `30` | 连续电流 |
| `vgs_th_v` | number | V | MOSFET | `2.5` | 栅阈电压 |
| `qg_nc` | number | nC | MOSFET | `45` | 栅电荷 |
| `vrrm_v` | number | V | Diode/Transistor | `100` | 反向耐压（二极管） |
| `if_a` | number | A | Diode/Transistor | `1` | 正向电流 |
| `vf_v` | number | V | Diode/Transistor | `0.7` | 正向压降 |
| `hfe` | number | — | Diode/Transistor | `120` | BJT 放大倍数 |
| `vceo_v` | number | V | Diode/Transistor | `40` | 集射耐压 |
| `sensitivity` | string | — | Sensors | `260 LSB/g` | 灵敏度（按型号） |
| `resolution_bits` | integer | bits | Sensors | `12` | 分辨率 |
| `range` | string | — | Sensors | `0-40` | 量程 |
| `accuracy` | string | — | Sensors | `±0.5` | 精度 |
| `pitch_mm` | number | mm | Connectors | `2.54` | 间距 |
| `positions` | integer | pins | Connectors | `40` | 针位数 |
| `current_rating_a` | number | A | Connectors | `3` | 额定电流 |
| `voltage_rating_v` | number | V | Connectors, Passive, Optoelectronics | `50` | 额定电压 |
| `mounting` | string | — | Connectors | `SMD` | 安装方式 |
| `resistance_ohm` | number | Ω | Passive(Resistor) | `10000` | 阻值（10kΩ） |
| `capacitance_pf` | integer | pF | Passive(Capacitor) | `100000` | 容值（100nF） |
| `inductance_uh` | number | µH | Passive(Inductor) | `4.7` | 感值 |
| `tolerance` | string | % | Passive | `±1%` | 容差 |
| `power_rating_w` | number | W | Passive(Resistor) | `0.25` | 功率 |
| `temperature_coeff` | string | — | Passive | `X7R` | 温漂 / 材质 |
| `data_rate_bps` | number | bps | RF & Wireless, Modules | `2000000` | 数据速率 |
| `output_power_dbm` | number | dBm | RF & Wireless, Modules | `20` | 发射功率 |
| `sensitivity_dbm` | number | dBm | RF & Wireless | `-95` | 接收灵敏度 |
| `modulation` | string | — | RF & Wireless | `GFSK` | 调制方式 |
| `wavelength_nm` | integer | nm | Optoelectronics(LED) | `650` | 波长 |
| `forward_voltage_v` | number | V | Optoelectronics | `2.1` | LED 正向压降 |
| `forward_current_ma` | integer | mA | Optoelectronics | `20` | LED 正向电流 |
| `ctr_percent` | integer | % | Optoelectronics(Optocoupler) | `50` | 电流传输比 |
| `load_capacitance_pf` | number | pF | Electromechanical(Crystal) | `18` | 负载电容 |
| `contact_rating_a` | number | A | Electromechanical(Relay) | `10` | 触点电流 |
| `coil_voltage_v` | number | V | Electromechanical(Relay) | `5` | 线圈电压 |
| `actuation_force_n` | number | N | Electromechanical(Switch) | `1.6` | 触发力 |
| `package` | string | — | 通用 | `LQFP-48` | 封装（通用） |

---

## 5. 入库校验规则（采集管线 / gen_parts.py 必须实现）

1. 每个 `attributes_json` 的 key 必须 ∈ 本字典；不在字典 → 整体标 `needs_review`，不静默写入。
2. 数值必须可转换到声明 type（integer / number）；失败 → `needs_review`。
3. 单位必须已归一（如电容一律 `capacitance_pf`，禁止混用 `100n` / `0.1u`）。
4. 同 SKU 内禁止重复 key；冲突时保留更精确者并告警。

---

*本字典与 `mfr_canonical.csv`、`Product_DB_Schema_v1_Final.md`、`category_taxonomy_v1.md` 构成 Data Factory v1 冻结包。新增 key 须先提评审加入本字典。*
