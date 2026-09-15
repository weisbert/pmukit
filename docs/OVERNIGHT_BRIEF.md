# 整夜开工单：pmukit 阶段 1 + 2 + 3（不依赖仿真器的全部）

> 给一个全新的 ultracode 会话。主控代理读完本文、`REFACTOR_PLAN.md`、`CONTRACTS.md`、`UX_RULES.md` 再动手。
> 不要中途向用户提问。拿不准的自己定，把决定写进 `docs/DECISIONS.md`（一行一条：决定、原因、可逆吗）。
> 每个里程碑一个 commit，commit 后立刻 push origin/main。

## 机器与范围

在 Windows 桌面上跑，没有 Spectre，没有 ALPS，没有 Donau。所以今晚只做**不需要仿真器**的部分；凡需要真仿真的，用可注入的假执行器和 dry-run 走通，明确标"仿真器验证待 VM"。

**做：** 契约落成代码 · 网表约定实现 · 计划编译器 · 台账 · 运行器（dry-run + 假执行器）· 摘要导出/导入/复现骨架 · web 壳八块画板通假数据 · 提交闸 · 测试 · 打包脚本。
**不做：** 拟合器、发射器、合成 LDO 回归、任何要跑 Spectre/ALPS 的验证、skillbridge、schematic 模板的 SKILL 脚本。

## 硬约束（违反即返工）

1. 仓库公开。任何客户名、网名、库名不得进仓：提交闸读仓外 `.pmukit-denylist`（桌面已有，位于仓根、git-ignored），命中即拒绝提交。从老仓拷文件时先用黑名单扫一遍再放进来。
2. 运行时只允许标准库 + numpy + scipy（`requirements.txt`）。web 壳是**单个内联 HTML** + `http.server`，无 CDN、无 Qt、无第三方 JS。
3. 所有文本文件 LF（`.gitattributes` 已强制）。
4. 老仓在桌面 `C:\code\LDO_modeling`（已 private，只读参考）。可拷清单见 `REFACTOR_PLAN.md` 第 3 节；拷入的文件头加一行 `# from LDO_modeling/<path> @ <sha>`，不改逻辑只改 import。
5. 真数据目录由环境变量 `PMUKIT_DATA` 指定，默认 `~/pmukit_data`；代码里不写任何客户数字。
6. 温度、VSET、负载、电源全部来自项目配置；代码里不写死。

## 顺序与里程碑（每个都有验收）

| # | 里程碑 | 验收 |
|---|---|---|
| M0 | 包骨架 `pmukit/`，venv，`pytest` 空跑通，`tools/guard.py` + pre-commit 安装脚本 | 提交闸能拦住黑名单词（用一个合成词自测）|
| M1 | 契约落成代码：`config.py`（0a 三问 + 0b 推导）、`spec.py`（契约 1 表）、`dataset.py`（契约 2 读写）、`ledger.py`（契约 3 SQLite，含 `recipe` 列）、`deliverable.py`（契约 4 目录和头）、`digest.py`（契约 5 导出/导入）| 每个契约有 schema 校验 + round-trip 测试 |
| M2 | `netlist.py`：老仓 `netlist_augment` 搬入 + 前缀识别（IL_/VB_/VS_/VEN_）+ `section=` / `parameters VSET=` / `options temp=` 改写 + 剥分析 + 配方文本生成 | 用 `tests/fixtures/pmu_demo/input.scs`（自己写的合成 PMU 网表，两轨两偏置一 EN 一 TESTMODE）认出全部引脚；未分类引脚报错不猜 |
| M3 | `plan.py`：模型规格 × 配置 → 测量计划，按 AC 叠加合并，每条 run 带 `feeds` 和 `recipe`，成本估计可插拔 | 合成配置得到 282 条 run 的同构结果；去掉一组能列出 NOT RUN 的块 |
| M4 | `runner.py`：接口一份，后端三个：`dry_run`（只写网表）、`fake`（注入执行器，生成假 PSF 目录）、`donau_alps`（搬老仓 `cluster/`，不在本机执行）；按 run_id 缓存 resume；写台账 | 假执行器跑完 282 条，台账状态机正确，重跑全部 `skipped_cached` |
| M5 | `digest.py` 完整：预算按优先级裁、丢的点名、分段、`import` 重建数据集子集、`reproduce` 骨架（拟合器缺席时只做数字对比） | 导出→导入 round-trip 无损；超预算时 trailer 列出丢的块 |
| M6 | web 壳：`pmukit/server.py`（http.server，路由与契约一一对应）+ `web/index.html`（单文件内联，八块画板照 `design/*.dc.html` 重写成普通 HTML/JS，去掉 DC 模板语法）；`--demo` 假数据；右键菜单、帮助面板、命令回显、四种状态、Ctrl Z 配置撤销 | `python -m pmukit ui --demo` 起服务，八个屏都能点通；无 CDN；Node 语法检查通过；一个 Python 冒烟测试请求每条路由 200 |
| M7 | CLI：`pmukit new/plan/run/status/report/deliver/digest/list/open/help`，命令回显条显示的每一条都真能跑 | `pmukit help <screen>` 打印帮助面板同一段文字 |
| M8 | 打包：`deploy/package.py` 从老仓搬入并去 Qt，产出一个 tar，盒子上 `bash apply` 能装 | 本机 dry-run 打包成功 |
| M9 | `BUILD_REPORT.md`：做了什么、测试数、跳过了什么和为什么、待 VM 验证清单、明早建议先看什么 | 一页，数字只在表里 |

## 并行建议

M1 的五个契约模块互不依赖，可并行；M2 和 M3 依赖 M1 的 `config`/`spec`；M4 依赖 M3；M5 依赖 M1 的 `dataset`；M6 依赖 M1–M5 的接口签名但可先按契约文档写假后端并行开工；M7 在 M6 之后。

## 汇报格式

每个里程碑结束在 `BUILD_REPORT.md` 追加一段：里程碑、commit sha、测试通过数、没做的、决定。失败的测试原样贴输出，不描述。

## 明早交接

用户会做三件事：走一遍 `--demo` 壳并给交互反馈；把仓库拉到 VM 上跑合成 LDO 回归；决定何时开阶段 4（拟合器 + 发射器）。
