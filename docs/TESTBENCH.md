# 搭台子：pmukit 和你之间的唯一接口

> 你只交**一份**网表（标称角导出就行）。工艺角、温度、VSET 档、负载点、激励，全部由工具改写这一份。
> 这一页就是你明早要做的那件事。做完跑一次 `pmukit check`，绿了再开项目。

## 一、命名约定（工具靠这个认引脚，认不出就报错，不猜）

| PMU 的每个引脚上挂什么 | 名字 | 工具从它认出 |
|---|---|---|
| 电压轨引脚 → 一个**电流源** | `IL_<引脚名>`，`dc` = 你台子里的典型负载 | 这是电压轨；典型负载值 |
| 电流偏置引脚 → 一个**电压源** | `VB_<引脚名>`，`dc` = 该脚工作电压 | 这是电流偏置；顺从电压 |
| 电源引脚 → 一个**电压源** | `VS_<引脚名>`，`dc` = 标称电源 | 这是电源；标称值 |
| EN 引脚（若有）→ 一个**电压源** | `VEN_<引脚名>` | 有 EN，要表征上电 |

方向很重要，**不是随便挑一个源**：
- 轨是「注入电流、读电压」→ 必须是 `isource`；
- 偏置是「加电压、读探针电流」→ 必须是 `vsource`。

挂反了工具会直接报错（而不是默默算出错的数）。

**轨上不挂 decap**：轨只挂 `IL_` 电流源。轨按本征（无 decap）表征，交付的模型里也没有 decap；
台子里挂了 decap 会被拟合进 Zout，你在系统台子里再挂一次就算了两遍。所以顶层有电容接在轨网上时
工具直接报错；接了别的东西（电阻等）会在引脚表备注里点名，因为它也会被当成电路的一部分表征进去。
PMU 子电路内部的输出电容不受影响——那是电路本身。

除此之外还要有：

```
parameters VSET=3                          // 输出档位变量，工具按 vset_codes 逐档改写
include "<pdk>/toplevel.scs" section=tt    // 带 section= 的 PDK include，工具按 corners 逐角改写
```

**档位变量叫什么由你定**：你的 PMU 里控制输出档位的设计变量叫 `vout_sel` 就写 `vout_sel`，
不用改名成 `VSET`。告诉工具它的名字：`pmukit check/new ... --vset-param vout_sel`
（New 屏 “code var” 下拉框从网表的 `parameters` 里选）。不指定就当它叫 `VSET`。
要跑多个档，而网表里没有声明这个变量时，工具直接拒绝，因为那样每一档跑的都是同一个电路。
PMU 没有档位变量的话，只跑一个档就行。

**每颗 LDO 各有档位时**：真实 PMU 里每颗 LDO 都有自己的档位，台子里把它们挂到**同一个**变量上，
把这个总变量交给工具：

```
parameters vsel=3 ldo_a_sel=vsel ldo_b_sel=vsel    // 工具只改 vsel；各 LDO 的变量跟着走
```

各 LDO 的变量只是 `vsel` 的表达式，工具不碰它们。想让某颗 LDO 固定在别的档，就直接写数
（`ldo_b_sel=2`），它就不跟着 `vsel` 变了。交付的模型里也只有一个 `vset` 实例参数，对应的就是这个总变量。

分析语句可有可无 —— 工具会把台子里所有分析语句剥掉，自己写。

## 二、文本模板

`tools/templates/tb_convention.scs` 是一份可以直接改的样例。把 `PMU_CELL` / 引脚名 / 电流电压换成你的就行。

## 三、从 Virtuoso 生成（省掉手连）

`tools/skill/pmukit_tb.il` 给定 PMU 的 cell 和一张角色表，生成带上述命名源的测试台 schematic。

```skill
load("/path/to/pmukit/tools/skill/pmukit_tb.il")
pmukitBuildTB("我的TB库" "pmu_tb" "PMU所在库" "PMU_CELL" "PMU_TOP"
  list(
    list("VDDA_1V0"  "supply" 1.0)      ; VS_VDDA_1V0  vsource dc=1.0
    list("VDD0P8_A"  "rail"   500u)     ; IL_VDD0P8_A  isource dc=500u
    list("VDD0P8_B"  "rail"   2m)
    list("IB_PTAT"   "bias"   0.4)      ; VB_IB_PTAT   vsource dc=0.4
    list("IB_POLY"   "bias"   0.4)
    list("EN"        "en"     1.0)      ; VEN_EN       vsource dc=1.0
    list("TESTMODE"  "none"   0)        ; 无角色，工具原样接线
  ))
```

> ⚠️ 这个 `.il` **在开发机上没法跑**（桌面没有 Virtuoso）。它按老仓
> `cadence/skill/pmu_top_symbol.il` / `ldo_cellview.il` 的既有写法写成，第一次真跑是在你的
> Virtuoso 会话里。跑完**务必**用下面第四节校一遍 —— 校验器才是判据，脚本只是省手工。
> 手连一个台子同样合格；这个脚本只是懒人路径。

## 四、导出后先校验，再开项目

```
pmukit check <导出的 input.scs> --pmu-inst PMU_TOP [--vset-param <档位变量名>]
```

它会把引脚表打出来：每个引脚的网、角色、源、dc、地，以及**认不出来的引脚和原因**。
认不出来不是错误 —— 有些引脚（测试脚、配置脚）本来就没角色，标 `ignore` 就行。
但**你想建模的引脚必须认得出来**。

校验通过之后：

```
pmukit new <项目名> --netlist <input.scs> --pmu-inst PMU_TOP \
    --corners tt,ss,ff --temps -40,25,125 --vset 3 --vset-param VSET \
    --care-up-to 2e10 \
    --load VDD0P8_A=5e-4,2e-6 --load VDD0P8_B=2e-3,5e-6 \
    --note "RX 模式，寄存器 0x12=0x03"
```

三问就是这三组参数：跑哪些角/温度/档、**你自己的模块吃多少电流**（开态、关态，可选边沿）、
在乎到多高频率。`--note` 会印进每个 `.va` 的溯源头。

`--load` 只写你真的会开关的轨。没写的轨照样表征小信号，只是负载 EN 事件会在报告里写「未跑」。

**标了 `stub` 的脚还要给一个直流值**：`--stub <引脚>=<值>`（轨填**伏特**，偏置填**安培**）。
为什么工具自己读不出来：约定源给的是**对偶量** —— 轨脚上挂的 `IL_` 是电流，而 stub 要发成电压源；
偏置脚上挂的 `VB_` 是电压，而 stub 要发成电流源。stub 按定义不跑仿真，所以这是网表唯一给不出的数。
不给的话，那个脚会被**弱连而不是驱动**，并在报告里写明 —— 工具不编一个值。

## 五、常见的五个坑

1. **源挂反了**（轨挂了 vsource / 偏置挂了 isource）→ 报错点名，改 master 即可。
2. **档位变量名没对上**：`check` 会报 `no parameters <名字>=`，并把网表里声明了的变量列出来，挑对的那个传给 `--vset-param`。
3. **PDK include 没有 `section=`** → 工具没法生成工艺角，报错时会把当前带 section 的 include 行列给你看。
4. **引脚名 ≠ 网名**：工具按 PMU 实例的**引脚**报角色，按它连到的**网**找源。两者不同名没关系，
   前缀跟着**引脚名**走（`IL_<引脚名>`）。
5. **地**：台子里接到 `0` 的那些 PMU 引脚被认成地引脚；哪条轨回哪个地，是从子电路内部的器件图
   读出来的（就近原则）。如果网表里没有 PMU 的子电路定义（黑盒 include），工具会**明说**读不到，
   要你在 New 屏指定，而不是瞎配一个。
