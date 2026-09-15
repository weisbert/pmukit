# 决定簿（overnight build）

> 规矩：不问用户，拿不准自己定，一行一条：**决定 · 原因 · 可逆吗**。
> 「可逆」= 改掉它要动多少东西：**易**（改一个常量/参数）、**中**（改一个模块 + 测试）、**难**（动契约或数据格式）。

| # | 里程碑 | 决定 | 原因 | 可逆 |
|---|---|---|---|---|
| D1 | M0 | 开发用 `C:\code\pmukit\.venv`（Python 3.11.7 + numpy 2.4.6 / scipy 1.17.1 / pytest 9.1.1），不复用老仓 venv | 老仓 venv 里有 PyQt5/skillbridge 等本仓禁止的依赖，共用会让"只许 stdlib+numpy+scipy"这条约束失效 | 易 |
| D2 | M0 | 提交闸匹配规则 = 大小写不敏感 + 非 `[A-Za-z0-9_]` 边界；token < 3 字符丢弃 | 要能在 `xi.VDD0P8_A` 里认出网名，又不能让 `ACMECHIP7X` 这种更长的词误报；短 token 噪声太大 | 易 |
| D3 | M0 | 提交闸命中后**改文件不改闸**：`design/*` 的真模拟电源网名→`VDDA_1V0`，`docs/CONTRACTS.md` 的三条真轨引脚名→`VDD0P8_A/B/C`，`tools/make_denylist.py` 例子里的真项目码→占位符 | 开工单硬约束第 1 条；这些名字来自真芯片，仓库是公开的 | 难（名字已进设计稿/文档，改回等于把客户名放回公开仓） |
| D4 | M0 | git hook 走 `core.hooksPath .githooks`（进仓、可版本化），不用 `.git/hooks` | `.git/hooks` 不进仓，换机器就失效；盒子和桌面要同一套闸 | 易 |
| D5 | M0 | 依赖用 `pyproject.toml`（PEP 621）+ `requirements.txt` 两份 | `pyproject` 给 `pip install -e .` 和入口点，`requirements.txt` 给 M12 的离线轮子流水线（`pip download` 读它） | 易 |
