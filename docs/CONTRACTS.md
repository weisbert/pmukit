# pmukit 四份契约（草稿，2026-09-15）

> 代码围绕这四份契约长。改契约要改这个文件，先于改代码。
> 格式一律 JSON + numpy `.npy` + SQLite，全是标准库或 numpy，盒子上不需要新 wheel。

```
项目配置 ──> 模型规格 ──> 测量计划 ──> 运行台账 ──> 数据集 ──> 拟合 ──> 交付物
 (输入)      (契约1)     (推导)      (契约3)     (契约2)            (契约4)
```

## 0. 项目配置（输入，用户写）

用户唯一要写的文件。所有"按项目变"的东西都在这里，代码里一个数都不写死。

```json
{
  "project": "demo_pmu",
  "dut": {"lib": "<lib>", "cell": "<cell>", "tb_lib": "<tb_lib>", "tb_cell": "<tb_cell>", "tb_inst": "PMU_TOP"},
  "state_note": "表征时 TB 处于的状态（模式、寄存器），原样印进交付件溯源头",
  "axes": {
    "process": ["tt", "ss", "ff"],
    "temp_c":  {"points": [-40, 25, 85, 125], "dc_sweep": [-40, 125, 5]},
    "vset":    {"codes": [3], "note": "档位号→输出电压由 TB 决定"},
    "supplies": {"AVDD1P0": {"net": "AVDD1P0", "dc": 1.0, "range": [0.9, 1.1]}}
  },
  "rails": {
    "pll": {"pin": "VDD0P8_PLL", "gnd": "VSS_PLL",
            "loads_a":  [1e-4, 5e-4, 1e-3],
            "load_en":  {"off_a": 2e-6, "on_a": 5e-4, "edge_s": 2e-9},
            "hf_stop_hz": 2e10}
  },
  "biases": {
    "iptat": {"pin": "IBP_PTAT_1P5U", "gnd": "AGND", "compliance_v": 0.667, "iv_sweep_v": [0, 1.8, 19]}
  },
  "en": {"pin": "EN", "characterize": true},
  "backend": {"engine": "alps", "queue": "…", "cpu": 8}
}
```

规则：
- 缺 `load_en` 的轨不表征负载 EN 事件，报告里写"未跑"，不猜默认值。
- `axes.temp_c.points` 是 AC/noise/tran 的离散温度；`dc_sweep` 是 DC 量的连续温度扫描。
- `hf_stop_hz` 是 Zout/PSRR 的扫频上限，应覆盖消费者 HB 的最高谐波；低于它模型静默外推是禁止的（见契约 4 包络）。

## 1. 模型规格（每个端口类型有哪些块、每个参数需要什么）

固定在代码里的"物理清单"，不按项目变。每个参数写明：来自哪个观测量、依赖哪些轴、属于哪一档。

| 端口类型 | 块 | 参数 | 观测量 | 依赖轴 | 档 |
|---|---|---|---|---|---|
| 电压轨 | dc | vout(load, T, vset) 表 + dropout + 限流 | `dc_load`, `dc_temp` | process, T(连续), vset, load | hb |
| 电压轨 | zout | RLC 梯 + 有源段 | `ac_zout` | process, T, vset, load | hb |
| 电压轨 | psrr | 实极点段 + 复极点段（gm-C 实现）| `ac_psrr` | process, T, vset, load | hb |
| 电压轨 | noise | 白 + 1/f + Lorentzian 组 | `noise_v` | process, T, load | hb |
| 电压轨 | load_en | 跌落/过冲的非线性辅助项 | `tran_load_on`, `tran_load_off` | process, T | ls（逐项开关 + HB 体检）|
| 电压轨 | no_sink | 单向导通约束 | 无（发射器常量）| 无 | hb |
| 电流偏置 | idc | I(Vpin, T) 表，PTAT 斜率连续 | `dc_iv`, `dc_temp` | process, T(连续), vset | hb |
| 电流偏置 | yout | gds + Cout | `ac_yout` | process, T | hb |
| 电流偏置 | noise | 电流噪声 白 + 1/f | `noise_i` | process, T | hb |
| 电流偏置 | psrr | 电源→电流传递 | `ac_psrr`（同一次电源注入）| process, T | hb |
| EN | ramp | 轨/偏置上升时间、过冲 | `tran_en` | process, T | en（能用档）|

档的含义：`hb` 默认开，进 RF/HB 交付；`ls` 每项单独开关，必须过 HB 首步残差体检才允许默认开；`en` 只保证消费者台子切 EN 不炸，不签核。

**测量计划由这张表 × 项目配置的轴自动推出**，按 AC 叠加去重（一次电源注入读全部端口）、按网表分组。每条 run 都能回答"我为哪个参数而存在"。

## 2. 数据集（表征结果，有维度）

一个目录，`index.json` 声明维度和变量，每个变量一个 `.npy`。不再有 `tr_pll_2m_tt_25c` 这种字符串键。

```json
{
  "project": "demo_pmu", "config_sha": "…", "created": "2026-09-15T12:00:00",
  "dims": {
    "process": ["tt", "ss", "ff"], "temp_c": [-40, 25, 85, 125], "vset": [3],
    "load_a": {"pll": [1e-4, 5e-4, 1e-3]},
    "freq_hz": "per-variable coordinate", "time_s": "per-variable coordinate"
  },
  "variables": {
    "ac_zout.pll":   {"dims": ["process", "temp_c", "vset", "load_a", "freq_hz"], "dtype": "complex128", "file": "ac_zout.pll.npy", "coord": "freq_ac.npy"},
    "noise_v.pll":   {"dims": ["process", "temp_c", "vset", "load_a", "freq_hz"], "dtype": "float64", "unit": "V^2/Hz", "file": "…"},
    "dc_iv.iptat":   {"dims": ["process", "temp_c", "vset", "vpin_v"], "dtype": "float64", "unit": "A", "file": "…"},
    "tran_load_on.pll": {"dims": ["process", "temp_c", "vset", "time_s"], "dtype": "float64", "file": "…", "coord": "t_load_on.pll.npy"}
  },
  "missing": [["tran_load_off.pll", "ss", 125, 3, "run failed: …"]]
}
```

规则：
- 变量名 = `观测量.端口`，维度顺序固定为 `process, temp_c, vset, [load_a], [扫描坐标]`。
- 缺格子用 NaN 填并在 `missing` 里登记原因，拟合器看得见"没跑"和"跑坏了"的区别。
- 数据集在 `$PMUKIT_DATA/<project>/dataset/`，不进 git。

## 3. 运行台账（每次仿真一行，SQLite）

`$PMUKIT_DATA/<project>/runs.sqlite`，两张表。

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,        -- sha 前 12 位：网表 + 角 + 分析
  process TEXT, temp_c REAL, vset INTEGER, load_key TEXT,
  analysis TEXT,                  -- dc_load | dc_temp | dc_iv | ac | noise | tran_load_on | tran_load_off | tran_en
  stimulus TEXT,                  -- 一热点：哪个源被激励
  reads TEXT,                     -- JSON 数组：这次读了哪些 观测量.端口
  netlist_sha TEXT, netlist_path TEXT, psf_path TEXT,
  engine TEXT, job_id TEXT,
  status TEXT,                    -- planned | submitted | running | done | failed | skipped_cached
  submitted_at TEXT, finished_at TEXT, cpu_seconds REAL, peak_mem_mb REAL,
  error TEXT
);
CREATE TABLE consumes (           -- 哪个参数吃了哪次 run
  run_id TEXT, port TEXT, block TEXT, param TEXT
);
```

规则：
- `run_id` 由内容哈希决定 → 同样的网表和角再提交一次直接 `skipped_cached`，这就是 resume。
- 界面的 Run 屏和 Plan 屏都只读这张表；Plan 屏的"为什么跑"来自 `consumes` 反查。
- 成本账 = `sum(cpu_seconds)` 按分析类型分组。

## 4. 交付物（用户拿走的东西）

```
$PMUKIT_DATA/<project>/deliver/<stamp>/
  PMU_<project>.scs          Spectre 库：每个工艺角一个 section，include 对应 .va
  PMU_<project>_tt.va        每角一份 Verilog-A；温度在内部连续，vset/load 是实例参数
  PMU_<project>_ss.va
  PMU_<project>_ff.va
  envelope.json              有效包络：频率上限、负载范围、温度范围、VSET 档、哪些 ls 项默认开
  report.md                  每角分块评分、HB 体检结果、未跑项清单
  provenance.json            config_sha、dataset_sha、pmukit 版本、表征时 TB 状态、日期
```

规则：
- 每个 `.va` 文件头重复一遍 `provenance.json` 的内容，文件脱离目录也能追溯。
- `envelope.json` 里的任何一项超出，报告里必须出现红字；模型不静默外推。
- 交付目录不进 git；`report.md` 里只有数字，没有客户网名，才允许摘录进仓库文档。
- 工艺角选择靠 Spectre `section`，和 PDK 的角变量同名，消费者的 corner 设置里加一行就能切。

## 待你确认的两点

1. 你们的 Cadence corner 设置是靠模型文件的 `section` 切工艺角吗？是的话契约 4 的 `.scs` 库形式就对；不是的话告诉我怎么切，交付形式跟着改。
2. `vset` 在配置里是档位号，输出电压由 TB 决定。这样对吗，还是你更想直接写目标电压？
