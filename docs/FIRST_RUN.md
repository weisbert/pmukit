# 明早在公司：首跑清单

> 逐条打勾。每条都写明**做什么、期望看到什么、不对的时候怎么办**。
> 遇到任何一条卡住：把失败包复制回桌面（`pmukit digest import` / Run 屏的 "Copy failure bundle"），
> 不用在盒子上硬调。

---

## 0. 装上（5 分钟）

**盒子能 `git pull` 公开仓，但多半够不到 PyPI** —— 所以有两条路，**先试离线那条**。

### (a) 离线包 + 一键安装（推荐）

黄区：`git pull` 后 `.\deploy\package.ps1 -Tar`（Python 3.10+ 即可），`dist\` 里出三个文件：
`pkg.tar.gz`、`pkg.tar.gz.sha256`、`pmukit_install.sh`。三个一起传到红区你建的文件夹里：

```tcsh
cd <workarea>/pmukit
bash pmukit_install.sh         # 注意是 bash，不是 ./ —— 盒子的 shell 是 tcsh
source env.csh
```

全部装在这个文件夹里：`install/`（程序 + `.venv`，numpy/scipy 在这里）、`data/`、`tmp/`、
`env.csh`、`install.log`。不写 `$HOME`、不写 `/tmp`，结尾会自己检查并报告。
以后更新（只改了程序）：黄区 `.\deploy\package.ps1 -Mode code -Tar` → `pkg_code.tar.gz`（约 0.5 MB，
不带依赖），连同 `.sha256` 传进同一个文件夹，再跑一次 `bash pmukit_install.sh`。
`requirements.txt` 变了的话它会拒绝，这时传完整包（`-Tar` 不带 `-Mode`）。

### (b) git clone（只在盒子能联网装 pip 时可行）

```tcsh
git clone https://github.com/weisbert/pmukit.git && cd pmukit && bash apply
```
clone 出来**没有** `wheels/`，`apply` 会认出这是 git-clone 模式并走**联网** pip。装不上就回到 (a)。

---

**期望**：结尾打印装后自检（**6 项**）。(a) 接着给出 `source env.csh`；(b) 给出两行要你贴进
`~/.cshrc` 的 `setenv`，照贴，然后 `source ~/.cshrc`。

- `bash apply` 做完整性校验（`MANIFEST.json` + `SHA256SUMS`）。**改过一个字节就会拒绝并点名文件。**
- 离线装是 `pip install --no-index --find-links wheels`，全程不联网。
- **`$PMUKIT_DATA` 永远不会被 `apply` / `update.sh` 碰** —— 重装不会丢你的数据。

☐ 装好，`pmukit --help` 有输出

**然后看一眼它从这台机器读到了什么**（默认就是盒子的配置：Donau short 队列 + ALPS）：

```tcsh
pmukit site                                   # 上半：保存的设置；下半：从环境变量读到的 + 来源
pmukit site --account <你的 Donau 账号>        # 唯一必须手填的一项
pmukit site --simulator spectre               # 只在要用 Spectre 时（license 紧张，默认 ALPS）
```

下半张表每行都写了来源：`alps_root` 来自 `$ALPS_ROOT`（或 `$ALPS_HOME` 去掉 `/tools/alps`），
`user` 来自 `$USER`（工号，会写进每个 .va 的溯源头），`license` 来自 `$LM_LICENSE_FILE` / `$CDS_LIC_FILE`。
显示 `(not found)` 的就是还缺的。

☐ `pmukit site`：engine `donau_alps`、simulator `alps`、alps_root 有值、account 有值
☐ `pmukit ui` 起来，打印 URL，盒子的 Firefox 能打开

> `pmukit ui` 只绑 `127.0.0.1`。要从别的机器看，加 `--host 0.0.0.0`（自己判断网络策略）。

---

## 1. 搭真 PMU 的台子（最容易出错的一步）

先读 `docs/TESTBENCH.md`（一页）。约定是：

| 引脚 | 挂什么 | 名字 |
|---|---|---|
| 电压轨 | **电流**源 | `IL_<引脚名>`，dc = 你台子里的典型负载 |
| 电流偏置 | **电压**源 | `VB_<引脚名>`，dc = 该脚工作电压 |
| 电源 | 电压源 | `VS_<引脚名>`，dc = 标称 |
| EN | 电压源 | `VEN_<引脚名>` |

加上 `parameters VSET=<n>` 和一行带 `section=` 的 PDK include。分析语句随意（会被剥掉）。

- 懒人路径：`tools/skill/pmukit_tb.il` 能生成这个台子。
  ⚠️ **它在桌面上没跑过**（桌面没有 Virtuoso）。先用 `pmukitPreviewTB` 干跑看一眼要放什么，
  再决定用脚本还是手连。**判据是下一步的校验器，不是脚本。**
- 手连同样合格，模板在 `tools/templates/tb_convention.scs`。

从 ADE 在**标称角**导出一份网表（只要一份，工具自己改角/温度/档位）。

☐ 台子搭好，导出 `input.scs`

---

## 2. 校验（不创建任何东西，30 秒）

```tcsh
pmukit check <input.scs> --pmu-inst <PMU 实例名>
```

**期望**：引脚表全部认出，结尾 `Convention OK`。

- 认不出的引脚会被点名 + 给出原因。测试脚、配置脚认不出是**正常的**，标 `ignore` 就行；
  但**你想建模的引脚必须认出来**。
- `PROBLEM:` 开头的行是真问题（没有 `section=` / 没有电源源 / 没有任何可建模端口），照它说的改。
- 源挂反了（轨挂 vsource、偏置挂 isource）会直接报错 —— 这不是挑剔，读数学依赖 master。

☐ `Convention OK`

---

## 3. 建项目 + 看计划

```tcsh
pmukit new <项目名> --netlist <input.scs> --pmu-inst <实例名> \
    --corners tt,ss,ff --temps -40,25,125 --vset 3 --care-up-to 2e10 \
    --load <轨名>=<开态电流>,<关态电流> \
    --note "<表征时台子处于什么状态，会印进每个 .va 的溯源头>"
pmukit plan <项目名>
```

**期望**：一张分组表 + run 数 + 估算 CPU 小时数。

- `--load` 只写你真的会开关的轨。没写的轨照样表征小信号，只是负载 EN 事件在报告里写「未跑」。
- `--care-up-to` 是 **AC 扫频上限**，不是瞬态边沿。两者是解耦的（这是老仓踩过的坑）。
- 觉得太贵：`pmukit plan <项目名> --off <组名>` 会列出**关掉它就没人喂的参数**，
  以及这些参数会在报告里被写成 NOT RUN。先看后果再决定。
- 想看某条 run 到底改了网表哪几行：`pmukit plan <项目名> --runs <组名>` 拿 run_id，
  再 `--recipe <run_id>`。配方最后一行就是真正的提交命令。

☐ run 数和成本看着合理
☐ 抽查一条配方，确认改的行是你预期的

---

## 4. 提交到 Donau（**`donau_alps` 后端的第一次真跑**）

```tcsh
pmukit plan <项目名> --submit
pmukit run <项目名> --engine donau_alps
```

**这是今晚唯一没能真跑过的一环**（桌面没有 Donau 队列）。命令是按老仓验证过的形状拼的，
但第一次真提交是现在。不对的话：

- 先 `pmukit plan <项目名> --recipe <run_id>` 看最后一行的 `dsub` 命令对不对；
- 手动跑一次那行命令，看是 dsub 的问题还是 pmukit 拼错了；
- `-mt N` 必须等于 `cpu=N`；`-I` 要指到 **目录** `<pdk>/alps`，不是 `toplevel.scs`；
  必须调 `.../bin/alps` **包装脚本**，不是裸二进制；`-format ps`，永远不要 `psfxl`。
  （这几条在 `docs/reference/TOOL_FACTS.md` 的 ALPS / Donau 一节里，都是踩出来的。）

☐ 提交成功，拿到 job id
☐ `pmukit status <项目名>` 看得到状态变化

---

## 5. 看台账、处理失败

```tcsh
pmukit status <项目名>
pmukit status <项目名> --status failed
pmukit status <项目名> --why <run_id>       # 这条 run 是为哪个参数而存在
```

- 失败的 run：Run 屏右键 → **Copy failure bundle**（只带 D0/D1/D6），粘回桌面。
- 重跑：缓存是按内容哈希的，**已经成功的不会重跑**。

☐ 失败（如果有）已经复制回桌面

---

## 6. 拟合、验证、交付

```tcsh
pmukit fit <项目名>
pmukit verify <项目名>
pmukit deliver <项目名>
pmukit report <项目名>
```

**期望**：`report.md` 第一段就是「有效范围 / 能用不签核 / 没跑 / 每角每轨绿黄红」。

☐ 报告第一段看得懂，且和你的实际使用范围对得上
☐ 红的地方有解释，不是沉默的外推

---

## 7. 放进自己的台子跑一次真 HB

在 corner 设置里加一行：

```
include "<交付目录>/PMU_<项目名>.scs" section=<角名>
```

角名和 PDK 的角变量同名，所以切角就是切 section。

☐ 真 HB 收敛
☐ 结果和真电路对得上（至少量级和趋势）

---

## 今晚没能验证的，按风险排序

1. **Donau 提交**（第 4 步）—— 桌面没有队列，命令只做过 dry-run。
2. **`tcsh` 本身** —— `apply` 是 bash，只**打印** `setenv` 行，要人手贴。
3. **盒子的文件系统** —— `$PMUKIT_PREFIX` 的配额 / NFS / `noexec` 未知。
4. **SKILL 生成脚本** —— 桌面没有 Virtuoso，一次都没执行过。
5. **盒子的 Firefox** —— web 壳只在桌面用 API 和 `node --check` 验过，没开过真浏览器。

前四条都有"手动替代路径"写在上面；第 5 条不行的话，整条流程 CLI 都能走完。

## 最先看什么

`BUILD_REPORT.md` 顶部：VM 探测结果 + 每个里程碑的验收数字。
`docs/DECISIONS.md`：今晚替你做的每一个决定，一行一条，写明为什么和可不可逆。
不同意哪条，改它的成本就写在那一行的最后一列。
