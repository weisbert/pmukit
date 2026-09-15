# 整夜开工单：pmukit 做到能用（阶段 1 到 5，含拟合、发射、验证、打包）

> 给一个全新的 ultracode 会话，**在 Windows 桌面上运行**；需要 Spectre 时 **ssh 到本机的 Linux VM**（Spectre 18.1 在那里）。主控代理先读完本文、`REFACTOR_PLAN.md`、`CONTRACTS.md`、`UX_RULES.md`、老仓的 `docs/reference/METHODOLOGY.md` 和 `TOOL_FACTS.md`，再动手。
> 不要中途向用户提问。拿不准的自己定，写进 `docs/DECISIONS.md`（一行一条：决定、原因、可逆吗）。
> 每个里程碑一个 commit，commit 后立刻 push origin/main。
> 目标：明天用户到公司，把仓库拉到盒子上，`bash apply` 装好，打开 web 界面，就能对真 PMU 开始表征和建模。

## 环境（Windows 开发，Spectre 走 ssh）

- 开发机：Windows 11，Python 3.11（numpy/scipy 已装），node 24，git。仓库 `C:\code\pmukit`。老仓 checkout `C:\code\LDO_modeling`（已 private，只读参考）。黑名单 `.pmukit-denylist` 已在仓根（git-ignored）。
- VM：`ssh ewave-vm`（`~/.ssh/config` 已配好别名和密钥，`BatchMode=yes` 免密）。Rocky 8，Cadence 环境**只在 `~/.cshrc`**，所以远程命令必须是 `ssh ewave-vm 'tcsh -c "source ~/.cshrc; spectre -64 ..."'`，直接 bash 找不到 spectre。Spectre 18.1.0.077 在 `/home/yusheng/Program/eda/cadence/SPECTRE181/bin/spectre`；编译 `.va` 必须 `-64`，在 scratch 目录编译副本。命令和环境参考老仓 `cadence/spectre_run.py`。
- **开工第一步探测 VM**：`ssh -o BatchMode=yes -o ConnectTimeout=8 ewave-vm 'tcsh -c "source ~/.cshrc; which spectre"'`。通了记录版本；不通就先做不依赖仿真器的里程碑（M0 M1 M2 M4 M9 M10 M11 M12 和 M5 的 dry_run/fake 后端），每 20 分钟重试一次，通了再补 M3 M5(spectre_ssh) M6 M7 M8；到早上仍不通，BUILD_REPORT 第一行写明"VM 未开机，仿真相关里程碑未跑"。
- ALPS / Donau / dsub 只在公司盒子上：`donau_alps` 后端从老仓 `cadence/cluster/` 原样搬，dry-run 验证，明天盒子首跑。
- 运行器的 Spectre 后端叫 **`spectre_ssh`**：把 run 目录 scp/rsync 到 VM 的 `~/pmukit_work/<run_id>/`，远程 tcsh 跑 spectre，PSF 目录拉回 `PMUKIT_DATA`；本地无 ssh 时退化为 `dry_run`。每条 run 的配方文本里最后一行就是这条远程命令。

## 硬约束（违反即返工）

1. 仓库公开。客户名、网名、库名、项目号不得进仓：提交闸读 `.pmukit-denylist`，命中即拒。从老仓拷任何文件先扫黑名单。测试夹具全部合成。
2. 运行时只允许标准库 + numpy + scipy。web 壳是**单个内联 HTML** + `http.server`，无 CDN、无 Qt、无第三方 JS。
3. 文本文件 LF。
4. 拷入老仓文件：文件头加 `# from LDO_modeling/<path> @ <sha>`，**数学不重写**，只换数据契约和接口。ngspice 路径和 `.lib` 双胞胎发射器不搬。
5. 真数据目录 `PMUKIT_DATA`（默认 `~/pmukit_data`）；温度、VSET、负载、电源全部来自项目配置，代码不写死。
6. 发射器只用 HB 安全原语白名单：无 laplace、复杂 section 用 gm-C 双二阶、近相消偶极合并、G0 限带、纯电容节点加 Gleak、电压型支路改电流型、非线性项默认关且逐项可开、轨不能吸电流。

## 里程碑与验收

| # | 里程碑 | 验收 |
|---|---|---|
| M0 | 包骨架 `pmukit/`、venv、`pytest` 空跑、`tools/guard.py` + pre-commit 安装、黑名单生成 | 提交闸用合成词自测能拦；黑名单文件存在且 git-ignored |
| M1 | 五份契约落成代码：`config.py`（0a 三问 + 0b 推导 + `ports` 的 model/stub/ignore）、`spec.py`（契约 1 表）、`dataset.py`（契约 2）、`ledger.py`（契约 3，含 `recipe` 列）、`deliverable.py`（契约 4）、`digest.py`（契约 5 导出/导入）| 每份 schema 校验 + round-trip 测试 |
| M2 | `netlist.py`：老仓 `netlist_augment` 搬入 + 前缀识别 + `section=` / `parameters VSET=` / `options temp=` 改写 + 剥分析 + 配方文本 | 合成 PMU 网表 `tests/fixtures/pmu_demo/input.scs`（两轨、两偏置、EN、一个无角色引脚）全部认出；未分类引脚报错不猜 |
| M3 | **合成 PMU 真件**：用老仓 `ground_truth/ldo_gt.lib` 类电路搭一个 Spectre 语法的 `PMU_DEMO`（两条 LDO 轨 + 两个电流镜偏置含 PTAT + EN），BSIM3 level 49；老仓 14 个合成 LDO 转 Spectre 语法进 `tests/fixtures/` | 经 ssh 在 VM 的 Spectre 跑 DC/AC/noise 各一遍通过 |
| M4 | `plan.py`：规格 × 配置 → 计划，AC 叠加合并，每条 run 带 `feeds` + `recipe`，成本估计可插拔 | PMU_DEMO 配置得到完整计划；去掉一组能列出 NOT RUN 的块 |
| M5 | `runner.py`：一个接口，后端 `spectre_ssh`（真，经 VM）、`dry_run`、`fake`、`donau_alps`（搬 `cluster/`，dry-run）；按 run_id 缓存 resume；写台账；PSF 读取搬 `binpsf.py`/`psf.py` | PMU_DEMO 全计划经 spectre_ssh 跑完落成契约 2 数据集；重跑全部 `skipped_cached`；台账每条有 `consumes` |
| M6 | 拟合器 `fit/`：从老仓搬 dc 表、Zout 梯（AAA 种子 + 最小二乘）、PSRR 实/复极点段、噪声（白 + 1/f + Lorentzian）、偏置 idc(T)/PTAT、yout、电流噪声、电流 PSRR、load-EN assist（`fit_iassist` 的 ODE 路径）、EN 上升；**每个块带解析 `predict(f | T | t)`，Model 屏的模型曲线和分数由它算，不跑仿真**；**每个工艺角分开拟合，温度在角内连续**（DC 量用扫温表，AC/noise 按离散温度点，参数随 T 插值只在单调量上做）| PMU_DEMO 每角每块有分数；识别性门（cond/σ）能报出不可辨识参数 |
| M7 | 发射器 `emit/`：单一 `.va` 发射器（stub 端口发成直流值理想源并在头部列出）（搬 `emit_pmu_model` 的 HB 安全原语 + 分地 + `hb_robust` 默认开），每角一份 `.va` + `.scs` section 库，温度连续，`vset` / `load_en_*` 实例参数，溯源头，`envelope.json`，`report.md`；**数值条件 lint**：每个元件在 `f_max`（配置的 care_up_to_hz）处的导纳动态范围超 1e6 报警 | 三个角的 `.va` 在 VM Spectre 编译 0 error；AC 对比拟合值 ≤ 0.01 dB |
| M8 | 验证 `verify/`：每角分块分数 → 绿黄红；**HB 体检门**：VM Spectre 驱动式 HB，逐项开关非线性项，记录首步残差，任一项比全关高 10 倍即不通过；系统级：一个简单振荡器台 + 模型做自治 HB 收敛；合成 LDO 回归：14 个 GT 各跑一遍全流程，分数写进 `tests/regression/baseline.json` | PMU_DEMO 全绿或有解释；HB 体检结果进 report；回归基线文件生成 |
| M9 | 摘要：导出按优先级裁、丢的点名、分段；`digest import` 重建子集；`reproduce --from-digest` 重拟合并逐数字对比 | 导出→导入→reproduce 全流程在 PMU_DEMO 上 round-trip |
| M10 | web 壳 `pmukit/server.py` + `web/index.html`：八块画板照 `design/*.dc.html` 重写成**单页应用**（一个页面，按项目状态切屏）；路由见下节接口表；长任务在后台线程，页面轮询台账 + 日志流式；项目状态落 `state.json`（当前步、三问、配置历史供 Ctrl Z）；右键菜单、帮助面板、命令回显、四种状态；`--demo` 假数据 + 真项目模式；服务器只绑 127.0.0.1，`--host` 可改 | **`tests/test_e2e_api.py`：不开浏览器，按顺序调接口 new → parse → plan → run(spectre_ssh) → fit → verify → deliver → digest，PMU_DEMO 全程通过**；每条路由冒烟 200；node 语法检查通过 |
| M11 | CLI `pmukit new/plan/run/status/fit/verify/report/deliver/digest/reproduce/list/open/help`，命令回显条的每一条都真能跑 | `pmukit help <screen>` = 帮助面板同一段文字 |
| M12 | 盒子打包 `deploy/`：从老仓搬 `package.py` / `audit_wheels.py` / `apply` / `update.sh`，去 Qt，manylinux2014 (glibc 2.17) 轮子离线包，`bash apply` 装到 tcsh 盒子，`pmukit ui` 一条命令起服务并打印 URL | 本机 dry-run 打包成功，轮子审计全过，安装脚本 LF |
| M13 | `BUILD_REPORT.md`：做了什么、测试数、跳过了什么和为什么、**明天在盒子上要首跑的清单**（Donau 提交、真 PSF 读取、真网表前缀识别、web 在盒子 Firefox）、明早先看什么 | 一页，数字只在表里 |

## 接口表（M10 的契约；页面只通过这些路由拿数据）

所有响应 JSON；错误统一 `{"error": {"what","why","do","where"}}`（四段式）。长任务返回 `{"job": id}`，进度从台账或 `/api/jobs/<id>` 轮询。

| 屏 | 路由 | 作用 |
|---|---|---|
| Home | `GET /api/projects` · `POST /api/projects` · `GET /api/machine` · `GET /api/deliverables/diff?a=&b=` | 项目列表与新建；引擎/队列/PDK/license 探测（各带超时）；交付版本对比 |
| New | `POST /api/p/<n>/netlist`（上传或路径）· `GET /api/p/<n>/pins` · `PUT /api/p/<n>/pins/<pin>`（role/fate）· `GET/PUT /api/p/<n>/config` · `GET /api/p/<n>/config/derived` · `POST /api/p/<n>/config/undo` · `POST /api/p/<n>/measure-load` | 解析网表、引脚表、model/stub/ignore、三问、推导配置、撤销、从网表量负载 |
| Plan | `GET /api/p/<n>/plan` · `PUT /api/p/<n>/plan/groups`（勾选）· `GET /api/p/<n>/plan/consequences` · `GET /api/p/<n>/plan/runs?group=` · `GET /api/p/<n>/runs/<id>/recipe` · `POST /api/p/<n>/submit` | 计划、勾选、后果、每组的 run 列表、配方、提交 |
| Run | `GET /api/p/<n>/ledger?status=` · `GET /api/p/<n>/runs/<id>` · `GET /api/p/<n>/runs/<id>/log`（流式）· `POST /api/p/<n>/runs/<id>/{retry,skip,kill}` · `POST /api/p/<n>/fit` | 台账、详情、日志流、动作、拟合 |
| Model | `GET /api/p/<n>/model/summary`（信任四格）· `GET /api/p/<n>/model/grades` · `GET /api/p/<n>/model/cell?port=&corner=&temp=` · `GET /api/p/<n>/model/curve?port=&cell=&block=`（GT + predict 同频点）· `POST /api/p/<n>/verify` | 顶部四格、格子、分块、曲线、HB 体检 |
| Deliver | `POST /api/p/<n>/deliver` · `GET /api/p/<n>/deliverables` · `GET /api/p/<n>/deliverables/<stamp>/files/<name>` | 交付、列表、文件预览 |
| Digest | `GET /api/p/<n>/digest/blocks` · `POST /api/p/<n>/digest`（blocks, budget → text, parts）· `POST /api/digest/import` | 块清单与大小、导出、导入 |
| 全局 | `GET /api/help/<screen>` · `GET /api/cli?screen=&state=` · `GET /api/jobs/<id>` | 帮助文本、命令回显、后台任务进度 |

## 并行建议

M1 五个契约模块并行；M2、M3 并行；M4 依赖 M1/M2；M5 依赖 M3/M4；M6 依赖 M5 的数据集；M7 依赖 M6；M8 依赖 M7；M9 依赖 M1/M6；M10 可从 M1 后按契约接口并行开工，最后接真后端；M11 在 M10 后；M12 独立可早做。

## 明天在公司的首跑清单（写进 BUILD_REPORT，供用户逐条打勾）

1. `git clone` 公开仓 → `bash apply` → `pmukit ui` → 盒子 Firefox 打开。
2. 用约定命名搭真 PMU 台子，导出一份网表，New 屏认引脚。
3. 计划屏看 run 数和成本，提交到 Donau（`donau_alps` 后端首次真跑）。
4. Run 屏看台账；失败的复制失败包回桌面。
5. 拟合、验证、交付；把 `.scs` 放进 corner 设置跑一次真 HB。

## 汇报格式

每个里程碑结束在 `BUILD_REPORT.md` 追加：里程碑、commit sha、测试通过数、没做的、决定。失败的测试原样贴输出。
