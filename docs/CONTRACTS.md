# pmukit 四份契约（草稿，2026-09-15）

> 代码围绕这四份契约长。改契约要改这个文件，先于改代码。
> 格式一律 JSON + numpy `.npy` + SQLite，全是标准库或 numpy，盒子上不需要新 wheel。

```
项目配置 ──> 模型规格 ──> 测量计划 ──> 运行台账 ──> 数据集 ──> 拟合 ──> 交付物
 (输入)      (契约1)     (推导)      (契约3)     (契约2)            (契约4)
```

## 0. 项目配置：两层，用户只碰第一层

用户是**用 LDO 的子模块设计师**，LDO 对他是黑箱。他知道的只有三件事：自己的台子、自己的模块吃多少电流、自己在乎到多高的频率。其余全部由程序从台子里读或自己决定。

### 0a. 用户面对的三问（intake，大多预填只需确认）

```json
{
  "project": "demo_pmu",
  "testbench": {
    "_auto": "从打开的 ADE 会话读：台子、PMU 实例、连了哪些引脚、corner 设置、温度、设计变量/寄存器",
    "tb": "<lib>/<cell>", "pmu_inst": "PMU_TOP",
    "corners_seen": ["tt", "ss", "ff"], "temps_seen_c": [-40, 25, 125],
    "registers_seen": {"VSET": 3}, "en_pin_toggled": false,
    "confirm": true
  },
  "my_load": {
    "_note": "每条轨：你的模块开态/关态吃多少；或按“从我的台子量”自动探 PMU 引脚电流",
    "VDD0P8_PLL": {"on_a": 5e-4, "off_a": 2e-6, "switches": true, "captured_from_tb": false}
  },
  "care_up_to_hz": {"_auto": "默认 = 你 HB 设置的 fund × maxharms", "value": 2e10}
}
```

- 没给 `my_load` 的轨：按台子里现有负载源的 DC 值当"开态"，不表征负载 EN 事件，报告写"未跑"。
- `captured_from_tb=true` 时程序在用户台子上跑一次短 tran，探 PMU 引脚电流，自动得到开态/关态/边沿。

### 0b. 程序推导的表征配置（internal，"高级"里可看可改，默认不展示）

由 0a + 模型规格（契约 1）+ 站点配置推出，落成一份 JSON 存进 `$PMUKIT_DATA/<project>/derived.json`，进溯源。

| 项 | 怎么定 |
|---|---|
| 工艺轴 | `corners_seen` 去重成 PDK 工艺 section 名 |
| 离散温度点 | `temps_seen_c` ∪ 端点；DC 量另加连续扫温 |
| VSET 档 | `registers_seen` |
| 电源 | 从台子找供电源；默认只在标称点，`range` 是高级选项 |
| 轨/偏置引脚、地 | 从台子连线解析 |
| 负载表征网格 | `[off, 0.2·on, on, 2·on]` 裁到 PMU 的限流以内 |
| 负载 EN 事件 | `off→on`、`on→off`，边沿取量得的或 1 ns |
| 偏置顺从电压、I-V 扫描 | 台子 DC 工作点读脚电压；扫 0 到电源电压 |
| 扫频范围、点密度 | 10 Hz 到 `care_up_to_hz`，每十倍频 20 点 |
| 噪声频段 | 10 Hz 到 100 MHz |
| tran 时长、步长 | 由拟合出的 Zout 峰频推恢复时间，取 8 倍 |
| 分组合并 | AC 叠加，一次注入读全部端口 |
| 站点配置 | 引擎、队列、CPU 数：安装时配一次，不在项目里 |

### 0c. 输出也用用户的话说

用户只问一句"这个模型在我的仿真里能不能信"。所以 `report.md` 第一段固定是：

- 有效范围：负载 A 到 B、温度 −40 到 125、频率到 X GHz、角 tt/ss/ff、VSET=3。
- 能用不签核：EN 上电过程。
- 没跑：列出。
- 每个角每条轨一行绿/黄/红，不出现内部分数。

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
