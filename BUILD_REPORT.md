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
- **契约 1 `spec.py`** — 11 个块 / 51 个参数的固定物理清单，`SPEC_SHA=959eb3dbdce8`；
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
