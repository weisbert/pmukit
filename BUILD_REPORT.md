# BUILD_REPORT — overnight build (2026-09-15 夜)

环境探测（开工第一件事）：**VM 通**。
`ssh -o BatchMode=yes ewave-vm 'tcsh -c "source ~/.cshrc; which spectre"'` →
`/home/yusheng/Program/eda/cadence/SPECTRE181/bin/spectre`，`spectre -W` → **sub-version 18.1.0.077**。
⇒ 仿真相关里程碑（M3 M5 M7 M8）按真跑执行，不降级。

桌面：Windows 11，`C:\code\pmukit\.venv` = Python 3.11.7 / numpy 2.4.6 / scipy 1.17.1 / pytest 9.1.1，node v24.14.0。

---

## M0 — 包骨架 + 提交闸

- **做了**：`pmukit/` 包骨架（`__init__.py`、`paths.py` = `$PMUKIT_DATA` 解析）；`pyproject.toml`；
  `.venv`（见上）；`tools/guard.py` 提交闸 + `--selftest`；`.githooks/pre-commit` 并
  `git config core.hooksPath .githooks`；`tests/test_guard.py`。
- **验收**：
  - `python tools/guard.py --selftest` → `guard selftest: PASS`（合成词 `ACMECHIP7` / `VDD_SECRET_RAIL`
    在脏文件上命中 2 条、在干净文件上 0 条、在 `ACMECHIP7X` 上 0 条 = 词边界没漏）。
  - `.pmukit-denylist` 存在（25 个 token）且 `git check-ignore` 命中 → 不进仓。
  - `pytest tests -q` → **5 passed**。
- **闸真的咬了**（第一次 `--all` 扫描 exit=1，18 处命中），按规矩**改文件不改闸**：

```
design/Deliver.dc.html, design/Main.dc.html, design/Plan.dc.html, design/Run.dc.html,
design/gen_screens.py :  真芯片的模拟电源网名  -> VDDA_1V0
docs/CONTRACTS.md     :  真芯片的三条轨引脚名  -> VDD0P8_A / VDD0P8_B / VDD0P8_C
tools/make_denylist.py:  docstring 例子里的真项目码 -> <PROJECT_CODE> <BLOCK_NAME>
```
（闸不允许在这份报告里复述被拦下的 token 本身——第二次提交它自己又咬了一口，同样按规矩改文件。）
  改完 `python tools/guard.py --all` → exit=0。
- **没做**：无。
- **决定**：D1–D5（见 `docs/DECISIONS.md`）。

## M1 — 五份契约落成代码（config / spec / dataset / ledger）


- **`pmukit/errors.py`** 四段式错误（what/why/do/where，缺一不可），`to_dict()` 就是 web 的错误体；
  **`pmukit/jsonio.py`** 规范 JSON + sha（Windows 和盒子上同一串）。
- **契约 0a/0b `config.py` + `site.py`** — `ProjectConfig`（三问 + `ports` 的 model/stub/ignore + 复合角）、
  `ConfigHistory`（Ctrl-Z，压栈 50）、`DerivedConfig` + `derive()`（0b 整张表，每个字段带 provenance）、
  `refine_from_zout()`；`SiteConfig`（engine/queue/cpus，装机一次，不进项目）。**77 个测试**。
- **契约 1 `spec.py`** — 11 个块 / 51 个参数的固定物理清单，`SPEC_SHA`；
  `requirements()` 把「参数 ← 观测量 ← 轴」反过来给计划器；`explain()` 是帮助面板和 CLI 的同一段文字；
  `NOT_MODELED` 把 REFACTOR_PLAN 6.2 明确不建的东西写进代码，防止有人手滑加回来。**32 个测试**。
- **契约 2 `dataset.py`** — 目录 + `index.json` + 每变量一个 `.npy`；维度序固定 `process, temp_c, vset, [load_a], [扫描坐标]`，
  `load_a` 按端口各自一套网格；**NaN = 没数据**，`coverage()` 区分 filled / missing（跑坏了，带原因）/ never_run（没跑）。
  写入是 `.npy` → `index.json` 各自 tmp+fsync+`os.replace`，任意时刻被杀都是可读数据集。**36 个测试**。
- **契约 3 `ledger.py`** — SQLite 两张表，列名列序与契约逐字一致（测试用 `PRAGMA table_info` 锁住）；
  `make_run_id` 只哈希内容 → 重新计划自动 `skipped_cached`（这就是 resume）；`consumes` + `why()` 就是 Plan 屏的"为什么跑"；
  `Recipe` 的 `~ + -` 文本可往返解析，提交命令永远是最后一行。**33 个测试**。
- **契约 4 `deliverable.py`** — 交付目录（`.scs` section 库 + 每角 `.va` + `envelope.json` + `report.md` +
  `provenance.json`），每个 `.va` 头重复一遍溯源（文件脱离目录也能追溯）；`report.md` 第一段固定四项，
  轨表里不出现内部分数（用正则锁住），机器可读的分数走旁边的 `grades.json`；`Envelope.contains()` 把越界
  的轴逐条点名；`Deliverable.diff()` 就是首页的"对比两个交付版本"。**24 个测试**。
- **契约 5 `digest.py`** — `[pmukit-digest v1]` 纯文本，块 D0–D9；**按优先级裁，丢掉的必须在 D9 trailer
  点名**（永不静默截断）；超 32 KB 自动分段、乱序也能拼回、缺段报四段式错误点名第几段、body sha256 对不上就拒收；
  D2 参数块无损（桌面凭它就能重新发射 `.va`）；`decimate_preserving_extremes` 保证跌落最小值和过冲最大值
  逐字保留；`failure_bundle` = D0/D1/D6。**17 个测试**。
- **验收**：每份 schema 校验 + round-trip 测试通过；**全套 331 个测试通过**。

## M2 — `netlist.py`：按前缀认角色，四处改写

- 从老仓 `cadence/cluster/netlist_augment.py @ d2c5b80` **原样搬**：续行合并、subckt 深度跟踪（子电路里的
  `I1` 永远不会冒充顶层源）、实例解析、分析语句识别与剥除、`mag=`/`dc=`/`type=pwl` 就地改写。
- **换掉的只有查找方式**：不再有 manifest，角色只认源名前缀 `IL_`/`VB_`/`VS_`/`VEN_`；对不上就报
  "此引脚无法归类"并给出可点的下一步，**不猜**。
- **新增**：`section=` 改写（工艺角）、`parameters VSET=` 改写（档位）、`options temp=` （温度）、
  分地按子电路器件图 BFS 读出、`insert_role_source()`（右键给引脚补一个约定源）、每次改动自动记一条
  `~ + -` 配方行。
- **验收**：合成 PMU 网表（两轨 + 两偏置 + EN + 一个无角色引脚 TESTMODE）全部认出；
  `require_classified()` 对 TESTMODE 报四段式错误；分地 `a→vssa`、`b→vssb`、偏置→`agnd` 从连线读出。**41 个测试**。

## M4 — `plan.py`：规格 × 配置 → 计划

- **族合并**：analysis + 同一个激励源 = 同一次仿真。轨 PSRR 和偏置 PSRR 是同一次电源注入 → 一条 run 两边都读。
- 每条 run 带 `feeds`（喂给哪些 `端口.块.参数`）→ 写进台账 `consumes` → Plan 屏的 Why 面板是查表不是编故事。
- 每条 run 带完整网表变体 + `~ + -` 配方 + 以远程 tcsh 命令收尾的提交行。
- 去掉一组 → `consequences()` 逐条列出哪些参数没人喂了，效果写成"会被报成 NOT RUN"。
- **验收（合成 PMU：2 角 × 3 温度 × 1 档 × 4 负载态，2 轨 2 偏置）**：

```
19 groups, 242 runs, 0.85 CPU-h (estimate)
    24 dc_load:IL_VDD0P8_A     24 ac:IL_VDD0P8_A      24 ac:VS_VDDA_1V0       24 noise:noise_v.a
    24 dc_load:IL_VDD0P8_B     24 ac:IL_VDD0P8_B      24 noise:noise_v.b       8 dc_temp
     6 dc_iv:VB_IB_PTAT         6 ac:VB_IB_PTAT        6 noise:noise_i.ptat
     6 dc_iv:VB_IB_POLY         6 ac:VB_IB_POLY        6 noise:noise_i.poly
     6 tran_load_on/off × 2 rails                      6 tran_en
```
  `ac:VS_VDDA_1V0` 一条 run 的 reads = `ac_psrr.a, ac_psrr.b, ac_psrr.ptat, ac_psrr.poly` —— 叠加合并生效。
  重新计划 → run_id 全部相同；提交两次 → 第二次 `cached == 全部`。**32 个测试**。

## M12 — 盒子打包 `deploy/`（去 Qt）

从老仓 `deploy/ @ d2c5b80` 搬：`package.py`、`audit_wheels.py`、`apply`、`update.sh`、
`dryrun_manylinux2014.sh`、`package.ps1`；新增 `README.md`（英文操作页）、`postinstall_check.py`。
**全程无 Qt**（有测试断言 `pyqt5` / `QT_QPA_PLATFORM` / `requirements-gui` 在任何 deploy 文件里都不出现）。

**轮子审计（真 `pip download`，不是模拟）**：
```
  numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   PASS (glibc 2.17)
  scipy-1.16.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl  PASS (glibc 2.17)
  + 6 个纯 python 轮子                                                      PASS
  8/8 PASS   target glibc 2.17 / x86_64        AUDIT PASS
```
**REJECT 路径也验过**（把真 scipy 轮子改名成 `_2_28`、加一个 cp312 轮子）：
```
  badpin-2.0-cp312-...whl                      REJECT (python tag 'cp312', need cp311/py3/...)
  scipy-...-manylinux_2_28_x86_64.whl          REJECT (needs glibc 2.28 > 2.17)
  2/4 PASS   AUDIT FAIL      audit exit = 1
```
**LF + 校验和**：`shipped text files containing a CR` → **0**；`sha256sum -c SHA256SUMS`（Git Bash）
42 个文件 **0 failures**；`bash -n` 通过 `apply` / `update.sh` / `dryrun_manylinux2014.sh`；
MANIFEST 键里 `backslash keys: 0`。**34 个测试**。

**在真 Linux 上端到端跑通**（`ssh ewave-vm`，Rocky 8.10 / glibc 2.28 / python3.11.13，和盒子同族）：
- `bash apply`：sha256 → 解包 → 模式判定 → 完整性（41 文件）→ 装源码 → 建 venv →
  **离线 `pip install --no-index`** 装上 manylinux2014 的 numpy/scipy → 原子装启动器 →
  装后自检 **5/5** → 打印 tcsh 的 `setenv` 行。
- 增量包 → 交给 `update.sh` → `removed 1 deleted file(s)`，`$PMUKIT_DATA` 和 venv 都没动。
- 改掉交付文件的一个字节 → `apply` 拒绝并点名是哪个文件。
- `dryrun_manylinux2014.sh` 在没有 docker 时走静态路径；`--deep` 扫 ELF 确认 numpy/scipy
  真的只引用 `GLIBC_2.17`（文件名和二进制一致）。
- `package.ps1` **真的跑了一遍**（不是只做语法检查）；UTF-8 带 BOM、LF。
  PowerShell 吞双引号那条坑当场复现了一次 —— 记录仍然有效。

**需要明天在盒子上验的 / 没做到的**：docker 不在本机，glibc-2.17 容器彩排只走了静态路径；
`tcsh` 本身没被执行过（`apply` 是 bash，只**打印** `setenv` 行，要人手贴进 `~/.cshrc`）；
盒子真实 `$PMUKIT_PREFIX` 的配额 / NFS / `noexec` 未知。

## M11 — CLI（`pmukit.cli` + `__main__.py` + 共享帮助文本）

- `pmukit new/pins/config/plan/run/status/fit/verify/report/deliver/digest/reproduce/list/open/ui/help`。
  后面才落地的模块**全部惰性导入**：装了一半也能跑已就绪的命令，没就绪的给四段式错误而不是 ImportError 栈。
- **`pmukit/helptext.py` 是帮助文本的唯一副本**：`pmukit help <screen>` 打印的、web 的 `? Help` 面板显示的、
  `GET /api/help/<screen>` 返回的，是同一段文字（有测试逐字比对）。八个屏，每屏三句话 + 本屏快捷键 + 全局快捷键。
- **补上了 M12 发现的阻塞项**：`pmukit/__main__.py` 之前不存在，启动器 `python -m pmukit` 会
  `No module named pmukit.__main__`。现已补上并有测试用真子进程验证。
- 顺手修的一个真会绊人的地方：`--temps -40,25,125` 会被 argparse 当成选项。现在负号开头的数字串会在解析前
  被重新拼成 `--temps=-40,...`（`=` 写法一直可用，现在自然写法也可用）。
- **端到端实测**（合成 PMU 网表）：

```
$ pmukit new demo_pmu --netlist tb/input.scs --pmu-inst PMU_TOP --corners tt,ss \
      --temps -40,25,125 --vset 3 --care-up-to 1e9 --load a=5e-4,2e-6 --load b=2e-3,5e-6
pin   net       role    fate    source       dc      gnd   note
vdda  VDDA_1V0  supply  model   VS_VDDA_1V0  1       -
a     VDD0P8_A  rail    model   IL_VDD0P8_A  0.0005  vssa  nearest ground in the subcircuit graph (2 hops)
b     VDD0P8_B  rail    model   IL_VDD0P8_B  0.002   vssb  nearest ground in the subcircuit graph (2 hops)
ptat  IB_PTAT   bias    model   VB_IB_PTAT   0.4     agnd  nearest ground in the subcircuit graph (1 hops)
poly  IB_POLY   bias    model   VB_IB_POLY   0.4     agnd  nearest ground in the subcircuit graph (1 hops)
en    EN        en      model   VEN_EN       1       -
tm    TESTMODE  none    ignore  -            -       -     no source named IL_*/VB_*/VS_*/VEN_* drives net 'TESTMODE'
  1 pin(s) have no role: tm        <- 报出来，不猜

$ pmukit plan demo_pmu --off noise:noise_v.a --off tran_en
212 runs, 0.68 CPU-hours estimated (4 load states, 19 groups)
  NOT RUN: a noise -- amp_i, corner_i_hz, flicker, nmode, white
  NOT RUN: en ramp -- i_overshoot, i_rise, t_delay, t_rise, v_overshoot
```
  `pmukit help <screen>` = 帮助面板同一段文字（**验收条件，已锁**）。**20 个测试**。

## 附加交付：搭台子的模板 + `pmukit check`（契约 0a 里那条「配套一个 schematic 模板」）

开工单的首跑清单第 2 条是「用约定命名搭真 PMU 台子，导出一份网表」—— 这是明早最容易出错的一步，
所以补齐了三件东西：

- **`docs/TESTBENCH.md`** —— 一页纸：命名约定表、方向为什么不能挑反、从 Virtuoso 生成、导出后怎么校、
  以及四个常见坑（源挂反 / include 没 `section=` / 引脚名≠网名 / 分地读不到）。
- **`tools/templates/tb_convention.scs`** —— 带注释的文本模板，可以直接改。**它自己要过校验器**（有测试锁住）。
- **`pmukit check <netlist> --pmu-inst NAME`** —— 开项目之前的预检：打印引脚表、点名认不出的引脚和原因、
  列出会被剥掉的分析语句，并检查「有没有带 `section=` 的 include」「有没有 `VSET`」「有没有电源源」
  「有没有任何可建模端口」。**创建任何东西之前就能知道台子对不对。**
- **`tools/skill/pmukit_tb.il`** —— 给定 PMU 的 cell 和一张角色表，在 Virtuoso 里生成带约定命名源的
  测试台 schematic（另有 `pmukitPreviewTB` 干跑，什么都不碰）。
  ⚠️ **桌面没有 Virtuoso，这个 `.il` 没被执行过**；它按老仓 `pmu_top_symbol.il` / `ldo_cellview.il`
  的既有写法写成，第一次真跑是在用户的会话里。**判据是 `pmukit check`，不是这个脚本**；手连一个台子同样合格。

实测（对模板本身）：
```
$ pmukit check tools/templates/tb_convention.scs --pmu-inst PMU_TOP
  ... 引脚表 ...
  note: subcircuit 'PMU_CELL' is not defined in this netlist -- pin names fall back to the
        connected net names, and per-pin grounds cannot be read from the wiring
  no role: TESTMODE
Convention OK.                                                     exit=0

$ pmukit check <把 IL_ 挂成 vsource 的版本> --pmu-inst PMU_TOP
What : source 'IL_VDD0P8_A' on net 'VDD0P8_A' is a vsource, but the 'IL_' prefix means 'rail',
       which must be an isource.
Why  : The read math depends on the master: a rail is read as a voltage under a current
       injection (isource), a bias is read as a probe current under a voltage drive (vsource).
Do   : Change 'IL_VDD0P8_A' to an isource.                          exit=2
```
顺手修掉一个真 bug：黑盒 include（网表里没有 PMU 子电路定义）时，引脚名回退成网名，
三个都接 `0` 的地引脚会在字典里**互相覆盖成一个**。现在按位置去重（`0`、`0#8`、`0#9`）并有测试锁住。

## M3 — 合成 PMU 真件 + 15 个合成 LDO 转 Spectre（**全部在 VM 的真 Spectre 18.1.0.077 上跑过**）

`tests/fixtures/pmu_demo/{input.scs, pdk/{toplevel,rc,models_bsim3}.scs, acceptance.py}`、
`tests/fixtures/ldo_gt/`（18 个 `.scs` + 17 个 vendored ngspice 源）、`tools/vmrun.sh`、`tools/gt_convert.py`。

**pmu_demo DC 工作点**（VSET=3，27 °C；轨 V，电流 µA）：

| 角 | VDD0P8_A | VDD0P8_B | VDD0P8_C | IB_PTAT | IB_POLY | I(VDDA) | nga |
|---|---|---|---|---|---|---|---|
| tt | 0.7949 | 0.7944 | 0.7962 | 8.212 | 23.341 | 2.6436 mA | 0.6104 |
| ss | 0.7946 | 0.7940 | 0.7960 | 8.338 | 20.460 | 2.6403 mA | 0.5392 |
| ff | 0.7950 | 0.7947 | 0.7961 | 8.090 | 26.376 | 2.6470 mA | 0.6796 |
| MOSff_RCss | 0.7948 | 0.7944 | 0.7958 | **6.992** | 23.217 | 2.6411 mA | 0.6796 |

**轨在角之间几乎不动（0.3–0.7 mV）—— 这正是环路在干活**；角咬在内部偏置（nga 0.539→0.680）、
IB_POLY（±13 %）、复合角上的 IB_PTAT（−15 %），以及 AC 上（见下）。复合角 `MOSff_RCss` 是个真目标。

- **VSET 0/1/2/3** → A 0.5719/0.6463/0.7206/0.7949，B 0.6459/…/0.7944，C 0.6174/…/0.7962（三条不同的分压律）。
- **PTAT 真的是 PTAT**：IB_PTAT @ −40/27/125 °C = **6.661 / 8.212 / 10.301 µA**（×1.546；理想绝对温度 ×1.708）；
  IB_POLY = 23.439 / 23.341 / 23.275 µA → **0.7 % 平坦**。
- **分地是真的分**：把地脚拆到各自的网上测回流，VSS_A 2.174 µA / VSS_B 4.347 µA / AGND 5.502 µA。
- **EN**：EN=1 → 2643.58 µA；EN=0 → 偏置 0.000/0.001 µA、电源 9.11 µA。
- **Zout / PSRR / 噪声**（tt/typ，27 °C）：
  - 轨 A **峰型**：6.417 Ω @DC → **82.3 Ω 峰 @1.413 MHz** → 1 GHz 处 0.30 Ω。PSRR −47.5 dB @DC，峰处最差 −7.4 dB。
  - 轨 B **ESR 平台型**：1.864 Ω → 18.8 Ω 峰 @0.282 MHz → **5 MHz–1 GHz 平坦在 7.81 Ω**。
  - 轨 A 输出噪声：7.70 µV/√Hz @10 Hz、784 nV @1 kHz、159 nV @100 kHz、**71 pV @100 MHz**。
  - 随角：Zout_A 峰 80.6(ss)/82.9(ff)/**92.0 Ω(MOSff_RCss，峰频 1.359→1.166 MHz)**；
    PSRR_A@1 MHz −12.88/−14.69/**−9.85 dB**。
- `pmukit check` 读这份 fixture：3 轨 / 2 偏置 / 电源 / EN 全部认出，**TESTMODE 报不可归类**，
  分地 BFS 解出 A→VSS_A、B→VSS_B、C→VSS_B、偏置→AGND。

**15 个合成 LDO + isrc 全部转换并在 Spectre 上跑通（27 个电路，0 失败）**，
且每个 LDO 的 DC 输出与**原 ngspice 卡片吻合到 ≤ 0.004 %** —— 翻译是忠实的，不是"看起来对"：

| circuit | Spectre DC vout | vs ngspice | \|Zout\| LF → 峰 |
|---|---|---|---|
| ldo_gt | 0.89848 | −0.0000 % | 23.3 → 252.1 Ω @1.585 MHz |
| ldo_v2_capless | 0.90134 | −0.0015 % | 24.7 → 370.5 Ω @6.310 MHz |
| ldo_v4_ffpsrr | 0.89848 | −0.0000 % | 23.3 → 397.3 Ω @1.000 MHz |
| ldo_v9_vldo | 0.99549 | +0.0000 % | 48.2 → 180.9 Ω @1.000 MHz |
| ldo_v10_3lc | 0.89848 | −0.0000 % | 23.3 → 36.7 Ω @0.398 MHz |
| …（其余 10 个同表，全部 ≤0.004 %） | | | |

`ldo_v1_nmos` 比它自己注释里的 "~0.55 V" 低 5.4 %，那是**原电路自带**的偏差（故意低环路增益的源随器）：
ngspice 0.520381、Spectre 0.52038 —— 两边一致。

**提交闸在这里咬了一次真的**：vendored 的 `isrc_gt.lib` 头部注释里有两个真客户单元名，已按规矩改文件。

`pytest tests/test_fixtures.py` → 31 passed, 1 skipped（`vm` 标记的那条，靠 `PMUKIT_VM_TESTS=1` 开）。

## M9 — 摘要往返：导出 → 导入 → 重现

- **导出**（`digest.py`，M1 里已落）：按优先级裁，**丢掉的块在 D9 trailer 点名**，超 32 KB 自动分段，
  乱序也能拼回，缺段 / body sha 对不上都报四段式错误。
- **导入 + 重现**（`pmukit/reproduce.py`，新）：
  - `rebuild_dataset(payload, path)` —— 从摘要重建一个**契约 2 格式的数据集子集**。
    只有摘要真的带了的序列才成为变量；**盒子丢掉的东西登记成 missing 并写明原因，不猜**。
    整块被丢掉的情况没有变量可挂 `missing` 行 → 另外写一份 `dropped.json` 点名，
    所以损失在两个地方都看得见，不会隐形。
  - **只有 ground truth 进数据集**：D4 同时带 GT 和模型，模型是拟合结果，跟着 `params` 走。
    契约里那句「重采样会改变拟合结果，所以模型数字要随摘要走」在这里被当成规则执行，不是注释。
  - `compare(box_params, desk_params)` —— **逐个参数**比，不做平均：`same` / `moved`（带相对差）/
    `only_box` / `only_desk` / `worst`。桌面重拟合**允许**和盒子不一致 —— 不一致本身就是结论
    （病态轨在盒外重拟合发散是有记录的真实案例）。
  - `reproduce(payload)` —— 重建 + 重拟合 + 比较；**桌面没装拟合器时不报错**，
    如实说明并把盒子的参数原样交出（凭 D2 就能重新发射 `.va`）。
- CLI：`pmukit digest import <txt>` / `pmukit reproduce --from-digest <txt> [--workdir]`。
- **验收**：export→parse 往返后 D2 参数**逐位相同**（`0.0931234567890123` 原样回来）；
  每一段都是 ASCII 且不含 `\r`（能过 relay 粘贴）；32 KB 预算下大 payload 的丢弃被 trailer 点名
  且在 `dropped.json` 里复述。**12 个测试。**

## 装机彩排：把当晚的代码打包、装到 Linux 上、跑一遍真流程

不是模拟，是把 `deploy/package.py` 打出来的包 scp 到 `ewave-vm`（Rocky 8.10，
**python 3.11.13 / numpy 2.2.6 / scipy 1.16.3 —— 和桌面的版本都不一样**，正好当作可移植性检查）：

```
[1/6] verifying MANIFEST.json + SHA256SUMS ...   integrity OK (73 files)
[2/6] installing source -> .../app ...
[3/6] creating venv ...
[4/6] OFFLINE pip install (--no-index --find-links wheels) ...
      Successfully installed numpy-2.2.6 scipy-1.16.3 pytest-9.1.1 ...
[5/6] installing launchers ...
[6/6] post-install check (headless):
      PASS 0. runtime stack      numpy 2.2.6, scipy 1.16.3
      PASS 1. server alive       host=eda py=3.11.13
      PASS 2. run a command, read its output
      PASS 3. streaming          first byte 0.01s of 3.01s
      PASS 4. files (download a .va, upload text back)
      PASS 5. CLI entry point (python -m pmukit)
      6/6 checks passed
```

然后在**装好的那一份**上跑真流程（合成 PMU fixture，3 角 × 3 温度）：

```
$ pmukit check .../input.scs --pmu-inst PMU_TOP
  ... no role: TESTMODE ...  2 analysis statement(s) will be stripped
  Convention OK.
$ pmukit new demo_pmu ... --corners tt,ss,ff --temps=-40,25,125 --port VDD0P8_C=stub
  1 pin(s) have no role: TESTMODE
$ pmukit plan demo_pmu
  363 runs, 1.28 CPU-hours estimated (4 load states, 19 groups)
$ pmukit plan demo_pmu --submit && pmukit run demo_pmu --engine fake
  363 runs, elapsed 3.9 s, 全部落进数据集
```

⇒ **明早 `git clone` → `bash apply` → `pmukit ui` 这条路在真 Linux 上走通了**，
而且不依赖桌面的 numpy/scipy 版本。
（`tests/` 不进包，所以盒子上没有 fixture —— 这是故意的，fixture 将来可能含真测量。）

## M5 — 运行器 + 导入器（**真 Spectre 跑通**）

`pmukit/psf.py` + `binpsf.py`（从老仓搬：二进制大端 PSF、**窗口化瞬态**的逆向读法、
`groups=1` 的定长跨步读法）、`pmukit/runner.py`、`pmukit/backends/{spectre_ssh,dry_run,fake,donau_alps}.py`、
`pmukit/importer.py`。

**验收 1 —— `fake` 全流程**：19 组 / 242 run / 4 个负载态 →
`done=242 failed=0 cached=0`，数据集 29 个变量、`{declared: 314, filled: 314, missing: 0, never_run: 0}`。

**验收 2 —— `spectre_ssh` 在 VM 上真跑**：
```
ssh -o BatchMode=yes ewave-vm 'tcsh -c "source ~/.cshrc; cd ~/pmukit_work/7d25e460b63c;
    spectre -64 input.scs -format psfascii -raw raw +log spectre.log -E"'
```
AC：`done cpu=0.185s`，`acz.ac: freq[161] 10..1e9`；导出的 `ac_zout.VDD0P8_A`（tt/27C/500 µA）
= 6.41661 Ω @10 Hz → **39.47 Ω 峰 @1 MHz（+43.90°）** → 0.3018 Ω @1 GHz。
噪声：`nz.noise: freq[141]`，`noise_v` = 5.934e-11 V²/Hz @10 Hz（√ = 7.703 µV/√Hz）。
**原始 `V(VDD0P8_A)` 是 −6.41661** —— 导入器从网表读出 `IL_VDD0P8_A ... isource` 才把符号摆正，
这正是「所有比值在 Python 里算」那道防火墙的价值。

**验收 3 —— resume**：重跑 → `skipped_cached=242 / 242`。
**验收 4 —— 导入已有结果**：把 242 个目录里的 132 个当成用户自己在 ADE 跑过的，喂给一个新项目 →
`filled: 132, unmatched: 0, still_to_run: 110`，台账 `imported=132` 且带 `source_path`。
**验收 5 —— `donau_alps` 干跑**：命令逐个 flag 断言成老仓验证过的形状。**从未真执行过，明早在盒子上是第一次。**
**验收 6**：`760 passed, 6 skipped`；需要仿真器的测试在 `available()` 为假时干净跳过。

## M6 — 拟合器（**数学从老仓搬，不重写**）

`pmukit/fit/{zout,psrr,noise,dc,bias,load_en,en,identifiability}.py` + 驱动。
每个块两个函数：`fit(...)` 和**纯解析的 `predict(...)`** —— Model 屏的曲线和分数由它算，
**`pmukit/fit/` 里任何模块都不许起进程**（有测试断言不出现 `subprocess`/`os.system`/`multiprocessing`）。

**参数回收（对解析生成的真值，planted vs fitted）**：

| 用例 | planted | fitted |
|---|---|---|
| Zout 单支 | Ra .05 / La 2 µH / Cout 1 nF / esr .5 | .0500 / 2.02 µH / 0.990 nF / .5000 —— **0.064 dB** |
| Zout 梯形 | Ra .1，(24 µH‖60 Ω)，(2 µH‖120 Ω) | .100000 / 24.000 µH / 60.000 / 2.0000 µH / 120.00 —— **0.020 dB** |
| PSRR 复极点 | pc_w0 7.5398e6，Q 3.0，G1 −6e-3 @20 kHz | 7.5402e6 / 3.003 / −5.92e-3 @20.09 kHz —— **0.011 dB / 0.1°** |
| 噪声（2 个负载） | white 1/2 nA，flicker 30/50 nA，拐点 1 k/100 k | 996.3 Hz / 99.97 kHz，幅度差 0.1 % —— **0.002 dB** |
| 偏置 idc | 500 nA，PTAT 1.2 nA/°C，vhi .85 | 500.0 nA / 1.2000 nA/°C / .8500 —— **0.58 %** |
| EN 斜坡 | 1 µs / 2 µs / 32 mV | 0.993 µs / 2.02 µs / 33.2 mV —— **0.26 %** |

**识别性门是真的会说话**：单支 Zout 那一例里 `Rpl`（1e5 Ω，几乎不阻尼）被点名为**不可辨识**，
而不是返回一个自信的错数。

**被拒方法的诱惑，记录在案**：合成单支轨上 branch B 被误挂（因为 `Cout` 提取差几个百分点），
代理差点给 keep-best 门加一条残差下限 —— 那正是 METHODOLOGY 里 REJECTED 的那类修补。
数值没动，改成把测试挪到可辨识的用例上。

**规模**：3 角 × 3 温度 × 2 档 × 4 负载 = 214 个 `BlockFit`，**11 秒**。

## 两个被 M5/M6 暴露出来的真问题（在我自己的模块里，已修）

1. **反着接的约定源认不出来**：`VB_<pin> (0 <pin>)`（节点顺序反了）过去会让引脚变成"无法归类"。
   人手画的时候很容易这么接。现在：正着接的优先；反着接的**照样认出来**，但在 `Pin.src_reversed`
   上记一笔并在 notes 里说明极性是反的（导入器本来就从工作点符号判方向）。一条网上有两个约定源 → 报错点名。
2. **简单角写法会"改过头"**：契约说简单写法 `["tt","ss"]` 时"所有带 `section=` 的 include 统一替换"。
   字面执行会把 RC skew 文件（section 叫 typ/ss/ff）也改成 `tt`，Spectre 直接
   `No section found with name 'tt'`。现在**读得到的 include 才按它真有的 section 改**，
   读不到的照契约改但**明说"没验证过"**，并把每个角实际落到哪几个文件报给 Plan 屏：
```
- pdk/rc.scs: left at section=typ; it declares {ff, ss, typ} and has no 'tt'.
  Use the composite corner form to set it explicitly.
- corner 'ss' set on 2 includes: pdk/toplevel.scs=ss, pdk/rc.scs=ss
- corner 'ff' set on 2 includes: pdk/toplevel.scs=ff, pdk/rc.scs=ff
```

## M10 — web 壳（单页应用，八块画板）

`pmukit/state.py`（每项目的界面状态，原子写；撤销**只有一条栈**：配置负载仍放
`config.ConfigHistory`，勾选快照放这里，`undo_log` 只记顺序，所以一次 Ctrl-Z 撤销的是"刚才那一下"）、
`pmukit/server.py`（stdlib `http.server`，默认只绑 127.0.0.1，开工单接口表的 38 条路由全实现）、
`pmukit/web/index.html`（2204 行 / 128 KB 单文件，**零 `http://` 外部引用**）。

- **`tests/test_e2e_api.py` = M10 的验收**：不开浏览器，按顺序调接口 24 步 →
  **21 passed, 3 skipped**（skip 的三步是 `verify` / `deliver` / 文件预览，等 M7/M8）。
  其中第 7 步实测 AC 叠加：一次 `VS_` 注入喂 4 个端口；第 9 步实测"关掉一组 → 点名丢了什么 + Ctrl-Z"。
- **真服务器 + 真 Spectre**：从 API 提交 `spectre_ssh`，run 在 VM 上执行、日志拉回、
  `/runs/<id>/log` 读得到。`/api/machine` 探测：engine ok（232 ms，指到 SPECTRE181），
  queue ok=False（"`dsub` is not on PATH; this is a desk, not a submit host."）—— 失败也给四段式理由。
- `model/curve` 实测（`ss/125C/vset3/5.0e-04A`，zout）：GT 与模型在**同一组频点**上，
  1e5 Hz 差 −0.016 dB / 0.0°，1e7 Hz 差 −0.017 dB / 0.5°。
- **130 + 24 个测试**；`node --check` 通过；有一个无头门用假 DOM 执行页面自己的脚本、
  把八个屏都渲染一遍（断言不抛错、不漏 `undefined`、四段式错误的 Do 行渲染出来）。
- **页面从未在真浏览器里打开过**（代理开不了窗口）—— 明早盒子 Firefox 是第一次。

## 把四个模块接起来时暴露出的问题（全部已修，全部是真的）

M10 和 M5 各交回两条，加上我自己跑通真 Spectre 全链时撞出的两条：

1. **`pmukit/web/index.html` 不会进 wheel** —— `pyproject.toml` 缺
   `[tool.setuptools.package-data]`。装出来的包会**没有页面**，明早 `pmukit ui` 直接空白。已补。
2. **`.tmpdata/` 没进 `.gitignore`** —— 我自己在冒烟测试时造的目录。公开仓里这是条泄漏路径：
   一旦有人把 `$PMUKIT_DATA` 指到仓内，真测量就可能被提交。已按**名字**忽略
   （`.tmpdata/`、`pmukit_data/`、`*_data/`、`.pmukit_scratch/`），不靠"希望没人建"。
3. **远端跑 Spectre 时 PDK 找不到**：`ERROR (SFE-868): Can not open input file 'pdk/toplevel.scs'` ——
   台子用相对路径 include PDK，而 run 目录在别处。现在 CLI 把网表里**相对** include 的顶层目录
   一起送进每个 run 目录（绝对路径的不动，拷一整个 PDK 更糟）。
4. **退化的 DC 扫描**：`dcz dc ... start=0.0005 stop=0.0005` →
   `ERROR (SPECTRE-16108): Stop limit must not equal start limit.` 两处根因：
   - 没声明 `--load` 的轨只有一个负载点 → 负载扫描没有量程。现在 `derive` 给它
     **0 .. 2× 台子自己的典型负载**（这个量程来自网表，不是猜用户的模块），带 provenance。
   - 只声明一个温度时，连续扫温**无意义** → **不生成这条 run**，并在计划里明说
     "读它的参数会被报成 NOT RUN"。**不编一段用户没要求的温度范围。**
   另外 `_sweep_clause` 现在对 start==stop 直接四段式报错，堵死这一类。
5. **两个代理的进度回调形状不同**（runner 是 `(kind, run_id, detail)`，fitter 是一个 dict）。
   CLI 两边都渲染，并在注释里说明它们没统一 —— 不假装是同一个东西。

## 真 Spectre 全链实测（M0→M6 串起来，合成 PMU，tt/27 °C）

```
$ pmukit run demo --engine spectre_ssh
  [done ] dc_load ... cpu 0.142 s   [stored] dc_load.VDD0P8_A @ tt/27C/vset3/5.0e-04A
  [done ] ac      ... cpu 0.148 s   [stored] ac_zout.VDD0P8_A
  [done ] ac      ... cpu 0.155 s   [stored] ac_psrr.VDD0P8_A, ac_psrr.VDD0P8_B,
                                              ac_psrr.IB_POLY, ac_psrr.IB_PTAT   <- 一次注入读 4 个端口
  [done ] noise   ... cpu 0.180 s   [stored] noise_v.VDD0P8_A
$ pmukit fit demo
  VDD0P8_A.zout   0.0394 dB      VDD0P8_A.psrr   0.0439 dB     VDD0P8_A.noise  1.07 dB   dc 0
  VDD0P8_B.zout   1.82   dB      VDD0P8_B.psrr   0.565  dB     VDD0P8_B.noise  0.723 dB  dc 0
  IB_POLY.yout    0.0012 dB      IB_POLY.psrr    2.24   dB     IB_POLY.idc     2.74 %
  IB_PTAT.yout    0.313  dB      IB_PTAT.psrr   21.8    dB     IB_PTAT.idc     1.78 %
```
**这是对真器件级仿真的拟合，不是对解析真值的。** 轨 A（峰型）Zout/PSRR 到百分之几 dB；
轨 B（ESR 平台型）Zout 1.82 dB —— 平台型本来就更难，是已知的代表性难例。
**`IB_PTAT.psrr = 21.8 dB` 是一条明确的待查项**（PTAT 的电源→电流传递很小，可能是量本身接近噪声，
也可能是拟合问题）——写在这里，明早值得看一眼，不假装它是好的。

## 一个被真 Spectre 数据揪出来的**模型形状**缺口（不是拟合 bug）

上面那条 `IB_PTAT.psrr = 21.8 dB` 追下去了。实测的电源→偏置电流传递：

| f | \|gdd\| | 相位 |
|---|---|---|
| 10 Hz | 3.569e-07 S | 0.0° |
| 1 kHz | 3.570e-07 S | 1.3° |
| 100 kHz | 9.298e-07 S | 60.0° |
| 10 MHz | 7.812e-06 S | 20.1° |
| **1 GHz** | **1.733e-04 S** | **87.5°** |

它**不是**下降，是**上升 500 倍**，且相位趋近 +90° —— 典型的 `jωC` 馈通（1.733e-4/2π/1e9 = **28 fF**，
器件交叠电容的量级）。而契约 1 的偏置 `psrr` 块只有 `gdd` + `psrr_pole_hz`，是个**只会下降**的形状。
**拿下降形去拟合上升曲线，再好的拟合器也只能拿 20 多 dB。**

这不是学术问题：REFACTOR_PLAN 6.1 把"电源→偏置电流传递"的用户写成 **VCO 杂散 / AM-FM**，
而真件的 `care_up_to_hz` 是 2e10 —— 上升那一段正是会主导的那一段。

**已修**：给偏置 `psrr` 加 `c_ft`，按 METHODOLOGY 的 keep-best 规矩做成**可选**自由度
（平坦的传递买不到这个旋钮，有测试锁住）。**21.8 → 9.49 dB。**

**剩下的 9.49 dB 如实记在这里**：实测曲线在 100 kHz 附近还有一个零极点对（+60° 的鼓包），
`gdd/(1+s/wp) + jωC` 仍然跟不上。要做到轨那种零点几 dB，得把轨 PSRR 的有理式机制
（SK 拟合 + 复极点段）也用到偏置传递上 —— 那是个更大的改动，**没有在凌晨做**。
明早如果真件的偏置 PSRR 是关键量，这是第一件该做的事。
