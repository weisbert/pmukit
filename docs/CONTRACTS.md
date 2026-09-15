# pmukit 五份契约（2026-09-15）

> 代码围绕这四份契约长。改契约要改这个文件，先于改代码。
> 格式一律 JSON + numpy `.npy` + SQLite，全是标准库或 numpy，盒子上不需要新 wheel。

```
项目配置 ──> 模型规格 ──> 测量计划 ──> 运行台账 ──> 数据集 ──> 拟合 ──> 交付物
 (输入)      (契约1)     (推导)      (契约3)     (契约2)            (契约4)
                                          └──────── 摘要（契约5）：盒子 → 桌面的气隙回程 ────────┘
```

## 0. 项目配置：两层，用户只碰第一层

用户是**用 LDO 的子模块设计师**，LDO 对他是黑箱。他知道的只有三件事：自己的台子、自己的模块吃多少电流、自己在乎到多高的频率。其余全部由程序从台子里读或自己决定。

### 0a. 用户交什么（intake）

**skillbridge / ADE 会话自动读取延后。** 用户交**一份**按约定搭好的 Spectre 网表（在标称角导出即可），程序自己改 PDK include 的 `section=` 生成各工艺角、用 `options temp=` 设温度、改 `VSET` 参数设档位。用户只补：跑哪些角和温度、VSET 档、模块负载、在乎到多高频率。

```json
{
  "project": "demo_pmu",
  "netlist": "tb/input.scs",
  "pmu_inst": "PMU_TOP",
  "corners": ["tt", "ss", "ff"],
  "temps_c": [-40, 25, 125],
  "vset_codes": [3],
  "state_note": "RX 模式，寄存器 0x12=0x03",
  "ports": {"VDD0P8_A": "model", "VDD0P8_B": "model", "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model", "TESTMODE": "ignore"},
  "my_load": {
    "VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": true}
  },
  "care_up_to_hz": 2e10
}
```

**哪些端口建模，用户在 New 屏的 Model 列决定**，落成 `ports`。三种归宿：`model` = 表征 + 拟合 + 发射；`stub` = 引脚保留，发射成该脚直流值的理想源（轨 → 电压源，偏置 → 电流源），零仿真，报告和 `.va` 头写明 "stub, not modeled"；`ignore` = 无角色引脚原样接线。有角色源的引脚默认 `model`；没有角色源的引脚要先右键指定角色，工具往网表插一个约定源。Plan 只为 `model` 端口出 run，这就是最小仿真集的实际落点。

`corners` 的高级写法：复合角按 include 文件分别指定 section，例如 `{"MOSff_RCss": {"toplevel.scs": "ff", "rc.scs": "ss"}}`。简单写法 `["tt","ss"]` 时所有带 `section=` 的 include 行统一替换。

**网表约定（这就是用户和工具之间的接口）**：

| 网表里要有 | 命名 | 程序从它认出 |
|---|---|---|
| PMU 实例 | 名字 = `pmu_inst` | 引脚清单、每个引脚接的网 |
| 每条轨引脚上一个电流源 | `IL_<引脚名>`，dc = 台子里的典型负载 | 这是电压轨；典型负载值 |
| 每个偏置引脚上一个电压源 | `VB_<引脚名>`，dc = 该脚工作电压 | 这是电流偏置；顺从电压 |
| 每个电源引脚上一个电压源 | `VS_<引脚名>`，dc = 标称电源 | 这是电源；标称值 |
| EN 引脚上一个电压源（若有） | `VEN_<引脚名>` | 有 EN；要表征上电 |
| 输出档位 | 设计变量 `parameters VSET=<n>` | 程序按 `vset_codes` 逐档改写 |
| PDK include 行 | 带 `section=<角>` | 程序按 `corners` 改写生成各角网表 |
| 地 | 每个引脚接的地网直接从连线读 | 分地 |
| 分析语句 | 可有可无 | 程序全部剥掉，自己写 |

规则：
- 角色**只靠源的名字前缀**认（`IL_`/`VB_`/`VS_`/`VEN_`），没有第二套 manifest。前缀对不上就报"此引脚无法归类"，不猜。
- 没在 `my_load` 里出现的轨：典型负载取 `IL_` 源的 dc，不表征负载 EN 事件，报告写"未跑"。
- 老仓 `netlist_augment.py` 的网表扫描、源探测、改 mag/dc/pwl、剥分析、加 save 全部复用，只把"按 manifest 找源"换成"按前缀找源"，再加 `section=` 和 `VSET` 两处改写。
- **配套一个 schematic 模板**方便用户套：一个 SKILL 脚本，给定 PMU 的 cell，生成带上述命名源的测试台 schematic（老仓 `pmu_top_symbol.il` / `ldo_cellview.il` 是先例）；另附一份文本网表样例。这是阶段 0 的交付之一。

### 0b. 程序推导的表征配置（internal，"高级"里可看可改，默认不展示）

由 0a + 模型规格（契约 1）+ 站点配置推出，落成一份 JSON 存进 `$PMUKIT_DATA/<project>/derived.json`，进溯源。

| 项 | 怎么定 |
|---|---|
| 工艺轴 | `corners`；逐角改写 include 的 `section=` |
| 离散温度点 | `temps_c`；DC 量另加连续扫温 |
| VSET 档 | `vset_codes`；逐档改写 `parameters VSET=` |
| 电源 | `VS_` 源的 dc；默认只在标称点，扫范围是高级选项 |
| 轨/偏置引脚、地 | 按 `IL_`/`VB_` 前缀 + 连线 |
| 负载表征网格 | `[off, 0.2·on, on, 2·on]` 裁到 PMU 的限流以内 |
| 负载 EN 事件 | `off→on`、`on→off`，边沿取量得的或 1 ns |
| 偏置顺从电压、I-V 扫描 | `VB_` 源的 dc；扫 0 到电源电压 |
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
  recipe TEXT,                    -- 人可读的配方：网表改动 + 分析 + 提交命令（Plan/Run 屏展开显示）
  status TEXT,                    -- planned | submitted | running | done | failed | skipped_cached | imported
  source_path TEXT,               -- imported 时：外部结果目录（ADE psf 目录或 CSV），其余为空
  submitted_at TEXT, finished_at TEXT, cpu_seconds REAL, peak_mem_mb REAL,
  error TEXT
);
CREATE TABLE consumes (           -- 哪个参数吃了哪次 run
  run_id TEXT, port TEXT, block TEXT, param TEXT
);
```

规则：
- `run_id` 由内容哈希决定 → 同样的网表和角再提交一次直接 `skipped_cached`，这就是 resume。
- **已有仿真结果可以直接用，不重跑。** 两条路：(a) pmukit 自己跑过的走哈希缓存；(b) 用户在 ADE 里跑过的结果目录（含当时的 `input.scs` + psf）由导入器读取：从网表认出角、温度、VSET、哪个源被激励，对上计划的格子就填进数据集，台账记 `imported` + `source_path`；对不上的逐条列出，缺的格子照常跑。CSV 也可以，但要用户指明每个文件是哪个观测量和格子。老仓 `import_cadence.py` 搬来接新契约。
- 界面的 Run 屏和 Plan 屏都只读这张表；Plan 屏的"为什么跑"来自 `consumes` 反查。
- **每条 run 存一份"配方"文本**（`recipe` 列）：网表里改了哪几行（`~` 原地改、`+` 新增、`-` 剥掉，原值写在注释里）、分析语句、save、提交命令。Plan 屏的 Runs 页签和 Run 屏的详情都显示它；默认折叠，调试时展开。
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

## 5. 摘要（Copy for desk：盒子到桌面的气隙回程）

盒子上没有 agent、没有网络。桌面要调试或本地复现，只能靠用户粘贴纯文本（relay）。老仓的 `[MPD1]` 摘要机制原样继承，规则如下。

```
[pmukit-digest v1] project=… created=… budget=64KB parts=2
[D0 provenance]  config sha · dataset sha · pmukit 版本 · 表征时 TB 状态
[D1 ledger]      每条 run 一行：状态、cell、分析、CPU；失败的带一句错误
[D2 params]      拟合参数，全部 cell，无损 JSON —— 桌面凭这一块就能重新发射 .va
[D3 grades]      Model 屏的绿黄红和"能不能信"四格，文本版
[D4 curves]      GT 和模型在**同一组重采样频点**上并排（zout/psrr/noise/idc/inoise），每十倍频 12 点
[D5 transients]  抽稀但极值点原样保留（跌落、过冲、稳定点）
[D6 faillog]     失败 run 的日志尾 40 行 + 网表里 pmukit 改动过的那几行
[D9 trailer]     保留了几块、多少 KB、**按预算丢掉的块点名列出**、sha256
```

规则：
- **预算按优先级裁**：provenance › ledger › params › 失败日志 › 偏置曲线 › 轨曲线 › 瞬态。超预算从低优先级丢，丢掉的必须在 trailer 点名，永远不静默截断。
- 预算档 32 / 64 / 128 KB；超过 32 KB 自动分段，每段带 `part i/N` 头，relay 一段一贴。
- 曲线块同时带 GT 和模型：桌面不必重拟合就能做对比；重采样会改变拟合结果，这一点老仓验证过，所以模型数字要随摘要走，不靠桌面重算。
- 入口：Model 屏"Copy for desk"（默认带选中 cell 的曲线）、Run 屏"Copy failure bundle"（默认只带 D0/D1/D6）。
- 桌面侧三条命令：`pmukit digest import <txt>` 重建契约 2 格式的数据集子集（缺的块登记在 `missing`，不猜）；`pmukit reproduce --from-digest` 在桌面重拟合并逐数字对比盒子报告；`pmukit report` 两边输出同一份文本。
- 摘要存 `$PMUKIT_DATA/<project>/digest/`，两台机器各一份，不进 git。

## 已确认（2026-09-15）

1. 交付形式：corner 设置靠模型文件的 `section` 切工艺角 → 契约 4 的 `.scs` 库形式成立。
2. `vset` 用档位号；网表里由设计变量 `VSET` 控制输出电压，程序直接改写。
3. 源前缀 `IL_`/`VB_`/`VS_`/`VEN_` 接受；配 schematic 模板。
4. 工艺角由工具改 PDK include 行生成；用户只导一份网表。
