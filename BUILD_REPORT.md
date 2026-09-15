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
