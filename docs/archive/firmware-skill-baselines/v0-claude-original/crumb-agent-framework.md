# Crumb-Agent 流水线框架（完整版 v1.0）

> 单 Agent 实现的 IoTPilot 式流水线：用户给一个 requirement，端到端产出一个在真实硬件上跑通的 ESP-IDF 系统。
> 本文档合并 v0.1~v0.5 全部讨论，作为修改 skill.md 的设计蓝本。

---

## 0. 总体设计哲学

| 原则 | 含义 |
|------|------|
| **正确性优先** | 当前阶段目标是尽可能准确正确地生成，不优化速度/token，慢可以接受 |
| **真实硬件闭环** | build 成功不是目标，真实运行数据正确才是目标（这是本框架超越所有参考论文的核心优势） |
| **端到端 ≠ 用户零参与** | 而是用户参与点【可预期、可控、最小化】，且在 STAGE 1 就提前透明化 |
| **ground truth 分级** | 本地版本 > MCP > LLM 记忆；datasheet 永远是 sensor 的最终依据 |
| **库边界清晰** | 每个子系统库只干自己的功能，不管和别人怎么配合 |
| **报错要主动思考** | 报错→分类→定向到可信来源→修，绝不直接把报错贴给 LLM 让它凭记忆猜 |

---

## 1. 全局硬规则（HARD RULES）

```
H1. 必须用 ESP-IDF 框架代码，不使用 Arduino 抽象层
H2. 用户必须提供 ESP-IDF 版本号 + 安装位置（例：esp-idf 6.0.0 @ /path/to/esp-idf）
    理由：编译用的是用户本地版本，所有 API 必须对齐这个版本
H3. 封装原则：main.c（状态机层）只调用子系统的语义化接口
    （如 network_connect() / audio_record() / storage_save()），
    绝不直接出现 esp_wifi_xxx / i2s_xxx 这类 IDF 原生调用。
    所有"怎么正确调用 IDF"的复杂度封装在子系统库内部。
H4. datasheet 永远是 sensor 的 ground truth，LLM 记忆只是加速草稿，
    且草稿一旦有一处与 datasheet 不一致即整体作废。
H5. register 类错误不触发编译错误，唯一兜底是硬件验证（读 ID / 对 baseline），
    故所有 register 校验必须前置在写代码之前。
H6. 失败处理必须主动思考：分类→定向可信来源→修，不直接贴报错让 LLM 猜。
H7. 子系统实现顺序：先做容易验证的、被依赖的；写一个验证一个，不攒到最后。
```

---

## 2. 整体 STAGE 流程图

```
┌───────────────────────────────────────────────────────────────────────┐
│ 输入：requirements.txt（功能描述，用户提供，含引脚连接）                  │
│       + ESP-IDF 版本号与安装位置                                         │
└───────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
╔═══════════════════════════════════════════════════════════════════════╗
║ STAGE 1  需求理解与结构化（Spec Synthesis）                              ║
║ 对应 IoTPilot 的 Problem Decomposition Agent                            ║
║                                                                         ║
║  用户粗略 requirements ──LLM总结──▶ 结构化 spec.md，含 6 个模块：         ║
║    ① 硬件清单（器件 / 型号 / 接口 / 角色）                               ║
║    ② 引脚连接表（标准化）                                                ║
║    ③ 核心功能逻辑                                                        ║
║    ④ 状态机（核心控制逻辑）← LLM 推导一版                                ║
║    ⑤ 子系统分解 → 每个标注 [MCU内置] 或 [外部Sensor]                     ║
║    ⑥ 硬件感知确认计划 → 每个子系统标注验证档位 A/B/C + 确认方式          ║
║                                  │                                      ║
║                          【人工审核 spec.md】◀── 强制检查点              ║
║                  用户审核一遍，确认理解无误后才进入 STAGE 1.5            ║
╚═══════════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
╔═══════════════════════════════════════════════════════════════════════╗
║ STAGE 1.5  datasheet 预拉取（Prefetch，不强制立即阅读）                  ║
║                                                                         ║
║  对 spec.md 里所有 [外部Sensor] 子系统：强制尝试拉 PDF 到               ║
║    components/<sensor>/，但【不要求立即阅读】                            ║
║    - 拉到 → 放着，是否阅读由 STAGE 2 的 2.B 逻辑决定                     ║
║    - 拉不到 → 立刻标记，请用户补 PDF（在写任何代码之前）                 ║
║  目的：把"datasheet 可得性"风险提前到最前面暴露，批量处理省事            ║
╚═══════════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
╔═══════════════════════════════════════════════════════════════════════╗
║ STAGE 2  逐子系统实现（Per-Subsystem Implementation）                    ║
║ 对应 IoTPilot 的 Func-coder Agent ×N（单 Agent 串行执行）               ║
║                                                                         ║
║  for 每个子系统（按依赖顺序，先做被依赖/易验证的，写一个验证一个）：       ║
║     2.0 分类  →  2.A/2.B 知识准备  →  2.C 写库                          ║
║              →  2.D 写 example（=调用链 + selftest 合一）                ║
║              →  2.E 单子系统验证（数据正确性 / 硬件感知）                ║
║     （详细流程见第 4 节）                                                ║
╚═══════════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
╔═══════════════════════════════════════════════════════════════════════╗
║ STAGE 3  集成（Integration）                                            ║
║ 对应 IoTPilot 的 Integration Agent                                      ║
║                                                                         ║
║  3.1 任务架构设计（写代码前先想清楚）                                    ║
║      读各子系统 example → 按状态机 + 任务启发式规划 FreeRTOS task        ║
║  3.2 实现 + 验证                                                        ║
║      按 task 架构组 main.c（只调语义接口）→ 验证状态/逻辑正确性          ║
║      默认不改子系统；接口不够 → 回 STAGE 2 加接口重验                    ║
║     （详细流程见第 5 节）                                                ║
╚═══════════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
╔═══════════════════════════════════════════════════════════════════════╗
║ STAGE 4  修订（Revision，按需）                                         ║
║ 对应 IoTPilot 的 Revision Agent                                         ║
║  用户指出问题 → 定位子系统或 main → 局部修改 → 重新验证                  ║
╚═══════════════════════════════════════════════════════════════════════╝

  ┌─────────────────────────────────────────────────────────────────────┐
  │ 【公共模块】失败处理（STAGE 2 / STAGE 3 共用，详见第 6 节）            │
  │   编译失败 → 分类(签名/链接/include) → 定向 grep 本地修               │
  │   串口失败 → 定位层级(启动/数据/逻辑/task) → 定向修                   │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## 3. STAGE 1 详解：需求理解与结构化

### 3.1 职责
把用户口语化、可能不完整的 requirements，固化成一个结构化、可审核的 spec.md。
这一步把"理解"显式化——避免 LLM 在脑子里隐式补全需求且每次补全不一样。

### 3.2 spec.md 的 6 个模块

```
① 硬件清单        器件 / 型号 / 接口 / 角色
② 引脚连接表      标准化（用户在 requirements 里提供，LLM 整理）
③ 核心功能逻辑    用结构化语言复述用户需求
④ 状态机          LLM 推导一版核心控制逻辑（错了影响最大，必须人审）
⑤ 子系统分解      每个子系统标注 [MCU内置] 或 [外部Sensor]
⑥ 硬件感知确认计划  见 3.3
```

### 3.3 硬件感知确认计划（⑥）—— 提前透明化用户参与点

对每个子系统标注**验证档位**，让用户在最开始就知道"哪些环节需要我参与"：

```
档位 A：无需用户，自验证（self-evident）
  数据本身就能判断对错，不需要外部参照。
  - 写后读回：写已知 pattern 再读回比对（如 W25N01GV）
  - ID 寄存器：读 WHO_AM_I / device ID 对上 datasheet 值
  - 数字输入：按钮按下读 1、松开读 0

档位 B：需要 baseline 范围，但不需要用户实时参与
  数据无"绝对正确值"，但有"合理范围"（写在 board profile baseline）。
  - 麦克风：静音 PCM 近零、出声有幅度
  - 温度：室温应 15~30°C，读到 -40/85 = 未初始化

档位 C：必须用户参与的物理确认
  数据合理，但"是否符合物理现实"只有人能确认。
  - 摄像头：像素合理，但"画面是不是对着的东西"要人看
  - LED/蜂鸣器：代码让它亮/响了，但"真亮了吗"要人确认
```

**关键：尽量把 C 档降级为 A/B 档**（见 3.4），降不了的明确确认方式。

```
spec.md 中的标注示例（Problem 1）：
  BQ25180   → A（读 ID 寄存器自验证）
  W25N01GV  → A（写后读回自验证）
  ICS-41350 → B（PCM 范围，baseline）
  Button    → 需用户按一下（极简，串口提示）
→ 用户审核时即知："这个项目我只需在按钮处按一下，其余全自动"
```

### 3.4 C 档降级技巧（端到端的关键）
```
用"可预测的物理激励"代替"人眼判断"：
  - test pattern：很多 sensor（如 HM01B0）有 test pattern 寄存器，
    开启后输出固定彩条/渐变，Agent 直接比对像素 = 降为 A 档
  - 写后读回：存储类天然可自验证
  - 数据范围：用 baseline 判断而非人工 = 降为 B 档
→ 真正剩下必须人确认的，会比想象的少很多
```

### 3.5 必须确认时的省力设计（问题 B）
```
① 攒到一起，不零散打断：所有需人确认的，攒到子系统/阶段末尾一次性确认
② 看一眼回 y/n，不让用户做复杂操作：Agent 准备好材料，用户只做最终裁决
③ 需物理动作的给明确指令：如串口提示"请按一下按钮""请用摄像头对准手"
④ 走串口通道半自动化：Agent 打印 "[CONFIRM] 画面是否正常？发送 y/n"，
   用户在串口回 y/n，确认也走在已有串口通道里，不用切换工具
```

### 3.6 强制检查点
STAGE 1 结束后**必须人工审核 spec.md**。
理由：LLM 自己生成的理解可能固化错误假设（如把双 mic 理解成两个独立 I2S）。
若 spec 理解错，后面全错。花 2 分钟看一眼，省后面几小时返工。

---

## 3.5 STAGE 1.5 详解：datasheet 预拉取（Prefetch）

### 职责
spec.md 审核通过后，**批量、强制**地把所有外部 sensor 的 datasheet PDF 拉到本地，
但**不要求立即阅读**——是否阅读、何时阅读，交给 STAGE 2 的 2.B 逻辑决定。

### 为什么"拉取"和"阅读"要解耦
```
① 提前暴露"拉不到"的风险
   某 sensor datasheet 自动下载失败 → 此刻就让用户补，
   而不是做到那个子系统才卡住（此时可能已写了一半代码）
② 批量比零散省事
   一次性拉齐所有 PDF，而不是做一个停一下
③ 不强制立即读 = 不浪费 token
   常见 sensor 可能 registry 就有现成库，根本不用读 datasheet；
   提前读反而浪费。所以只"备好"，不"预读"
```

### 流程
```
对 spec.md 里每个 [外部Sensor]：
  尝试拉取 PDF 到 components/<sensor>/（复用 2.B.2 的获取逻辑）
    ├─ 拉到 → 放着不读，标记"PDF ready"
    └─ 拉不到 → 标记"PDF missing"，汇总后一次性请用户补，
              在进入 STAGE 2 写代码之前必须补齐（稀有 sensor 尤其关键）
```

### 与 2.B.2 的关系（不冲突）
2.B.2 的第一条本来就是"components/<sensor>/ 已有 PDF → 直接用"。
STAGE 1.5 只是把这一步提前批量做了，让 2.B.2 走到时本地大概率已有 PDF，
直接进入"这个 sensor 到底要不要读 datasheet"的判断。

---

## 4. STAGE 2 详解：逐子系统实现

### 4.1 STAGE 2 详细流程图

```
对每个子系统（按依赖顺序，先做被依赖/易验证的）：

┌──────────────────────────────────────────────────────────┐
│ 2.0 分类：这个子系统是 [MCU内置] 还是 [外部Sensor]？          │
└──────────────────────────────────────────────────────────┘
            │                                       │
      [MCU内置]                                 [外部Sensor]
   (WiFi/I2S/SPI/ADC/                       (BQ25180/W25N01GV/
    GPIO/NVS/HTTP…)                           ICS-41350/HM01B0…)
            ▼                                       ▼
┌───────────────────────────┐   ┌──────────────────────────────────────┐
│ 2.A 知识准备（可信度阶梯）  │   │ 2.B 知识准备（决策树，见 4.3 详版）     │
│                           │   │                                      │
│ ① ls $IDF/examples/<feat> │   │ 2.B.1 search_components(<part>)      │
│   最高：可编译、版本一致   │   │   ├ 找到 → 读 example/README →        │
│ ② grep $IDF/components/    │   │   │        理解调用方式 → 2.C(用现成) │
│   header → 精确签名(最高)  │   │   └ 没找到 → 2.B.2                    │
│ ③ esp-docs MCP → 补思路    │   │ 2.B.2 获取 datasheet                 │
│   ⚠ 版本可能不同，仅参考   │   │ 2.B.3 填 datasheet_notes.md：         │
│ ④ LLM 记忆 → 仅大方向      │   │   路径A 记忆加速校验（二元裁决）       │
│                           │   │   路径B 直接按 datasheet 提炼          │
│ ★ ground truth = ①②      │   │ 2.B.4 (可选)用户手动提供参考          │
│   esp-docs/记忆只做参考    │   │ ★ ground truth 永远 = datasheet      │
└───────────────────────────┘   └──────────────────────────────────────┘
            │                                       │
            ▼                                       ▼
┌───────────────────────────┐   ┌──────────────────────────────────────┐
│ 2.C 写库（编排型）          │   │ 2.C 写库（实现型 / 用现成型）           │
│ 封装 IDF API →             │   │ 实现型：按 register map + 通信协议      │
│ 语义化子系统接口           │   │         写读/写函数                    │
│ <sub>.h / <sub>.c         │   │ 用现成型：封装现成库为子系统接口        │
│ 本质：编排已有 API，不实现 │   │ 本质：从零造轮子，datasheet 是依据      │
└───────────────────────────┘   └──────────────────────────────────────┘
            │                                       │
            └───────────────────┬───────────────────┘
                                ▼
            ┌──────────────────────────────────────────┐
            │ 2.D 写 example（= 调用链 + selftest 合一）   │
            │ 展示"在我们的引脚/逻辑下，怎么按顺序用这些    │
            │ API 完成子系统功能"；同时作为 selftest 载体  │
            └──────────────────────────────────────────┘
                                ▼
            ┌──────────────────────────────────────────┐
            │ 2.E 单子系统验证（数据正确性 / 硬件感知）     │
            │ build → flash → 跑 selftest → 对 baseline   │
            │ 三档 A/B/C；复杂传感器用分层 selftest        │
            │ 对 → 下一个子系统                           │
            │ 错 → 失败处理(公共模块) → 回 2.C 修         │
            │     现成库反复不适配 → 回退 2.B datasheet    │
            │ ★ register 错只能在这里被抓住，不在编译期     │
            └──────────────────────────────────────────┘
```

### 4.2 子系统产物结构
```
components/<subsystem>/
  ├── <subsystem>.h        ← API 声明（语义化接口，给 main.c 用）
  ├── <subsystem>.c        ← API 实现
  └── example/             ← 调用链 + selftest（合一）
```

### 4.3 两类子系统的本质区别
```
[MCU内置]（如 WiFi、I2S）
  API 实现：✅ IDF 已提供，不用写
  你要写的：把 IDF API 封装成"你的子系统接口" + example 调用链
  本质：不是实现 API，而是【正确编排已有 API】
  为何还要封装一层？→ main.c 只看到 network_connect()，
    "怎么正确调用 IDF"的复杂度关在库内部（硬规则 H3）

[外部Sensor]（如 HM01B0）
  API 实现：❌ 要自己写
  本质：sensor 数据读取 = 通过通信协议(I2C/SPI/PDM) 读写传感器 register
  datasheet 的 register map 是 ground truth
```

### 4.4 2.B 外部 Sensor 知识准备（详细版）

```
═══════════════════════════════════════════════════════════════════════
 核心信条：datasheet 永远是 ground truth，LLM 记忆只是加速草稿，
          且草稿一旦有一处与 datasheet 不一致即【整体作废】。
═══════════════════════════════════════════════════════════════════════

【2.B.1】先搜 registry —— 能不造轮子就不造
   search_components("<part_number>")
   ├─ 找到现成库
   │    → idf.py add-dependency "namespace/component"
   │    → 读 managed_components/<lib>/ 的 example 与 README（仿 AutoEmbed）
   │    → 搞清两件事：① 暴露哪些关键函数  ② 标准调用顺序(init→config→read)
   │    → 进入 2.C（用现成库封装），本节结束
   │    ★ 注意：现成库不直接信，仍要写我们场景的 example + 过 2.E 验证
   │      （别人板子能跑 ≠ 我们的引脚/芯片/IDF版本能跑）
   └─ 没找到 → 进入 2.B.2

【2.B.2】获取 datasheet
   按顺序尝试（验证响应以 %PDF 开头，失败则下一步）：
   ① components/<sensor>/ 已有 PDF → 直接用
   ② python tools/fetch_datasheet.py <PARTNUM> → 自动按厂商规则下载
   ③ 全失败 → 请用户把 PDF 放进 components/<sensor>/，不继续写驱动

【2.B.3】填写 datasheet_notes.md —— 两条路径，结果都以 datasheet 为准

   ┌─ 路径 A：记忆加速（仅当 LLM 能写出该 sensor 的具体
   │           register 地址 / I2C 地址 / init 序列时启用）
   │   Step 1  LLM 凭记忆先写一版 datasheet_notes.md 草稿
   │   Step 2  打开 datasheet，逐字段校验草稿（每个 register 地址、
   │           每个配置值、每步 init 序列都比对）
   │   Step 3  二元裁决：
   │           ├─ 全部一致 → 采纳草稿，标注 ground truth = datasheet
   │           │             （删掉 datasheet 未提及、LLM 自己编的字段）
   │           └─ 出现任何一处不一致（哪怕一个 bit）
   │                → 草稿【整体作废】，转路径 B 从头按 datasheet 提炼
   │                → 禁止"只改错的、留下对的"中间状态
   │                  （理由：LLM 分不清自己哪记对哪记错，一处错说明
   │                   整份草稿的可信度已破产；且既然要逐条查 datasheet，
   │                   记忆就没省任何东西，留它无意义）
   │
   └─ 路径 B：直接按 datasheet 提炼（稀有 sensor 如 HM01B0，
              或路径 A 草稿作废后）
       必读章节（章节名因 datasheet 而异，按含义匹配）：
         Pin Configuration / Electrical Characteristics(Timing) /
         Register Map / Detailed Description / Application and Implementation
       跳过：Layout / Mechanical / Packaging / Ordering / Revision History
       提炼进 datasheet_notes.md（永久参考，驱动的唯一 ground truth）：
         # <PartNumber> Datasheet Notes
         ## Function / ## Interface / ## Register map
         ## Init sequence / ## Key timing / ## Gotchas

【2.B.4】（可选）用户手动提供的参考
   - 不自动扒 GitHub（自动 clone/扒工程 = 高复杂度+高不确定性，现阶段不做）
   - 仅当 datasheet 某处不清晰（如 init 时序模糊）时，由【用户】主动把
     参考库关键文件放进项目目录，Agent 才读
   - 参考库只用于"理解逻辑"，具体数值仍以 datasheet 为准
   - 例：HM01B0 找不到 IDF 库，但有 Arduino 参考库可由用户提供，参考其调用逻辑

【关键提醒】register 类错误不触发编译错误（硬规则 H5）：
   #define PWR_MGMT_1 0x6C   // 写错成 0x6C，编译照样通过
   能抓住它的只有 2.E 硬件验证（读 ID / 对 baseline）。
   所以校验必须在写代码之前完成，不能指望编译器事后发现。
═══════════════════════════════════════════════════════════════════════
```

### 4.5 2.A MCU 内置知识来源可信度阶梯
```
可信度（高→低）：
① 用户本地 $IDF_PATH/examples/        ← 最高：可编译、版本一致
   "这个功能官方怎么用"的标准答案，copy-adapt，不从头写
② 用户本地 $IDF_PATH/components/.../include  ← 最高：API 签名 ground truth
   grep 确认函数存在 + 提取精确签名
③ esp-docs MCP                        ← 中：版本可能不一致
   理解原理、查 Migration Guide、当"思路参考"
   ⚠ 不能直接照抄签名（可能和本地版本不同）
④ LLM 记忆                            ← 最低：版本错位高发
   只用于"大方向"，具体签名必须用 ①②③ 核对
```
> esp-docs MCP 内容：① Programming Guide（API Reference / API Guides /
> Migration Guide）② Technical Reference Manual（芯片寄存器级文档，
> 直接操作寄存器时看）。esp-component-registry MCP：ESP 组件注册表搜索引擎
> （相当于 ESP-IDF 生态的 npm registry）。

### 4.6 2.E 单子系统验证（硬件感知）

**职责**：验证【数据正确性】——"这个传感器读出来的值对不对"
（与 STAGE 3 的"状态/逻辑正确性"区分开）

**三档验证方法**（同 3.3）：A 自验证 / B 靠 baseline / C 需用户

**复杂传感器用分层 selftest**——核心思想：不被动读数据猜对错，
而是主动制造一个已知答案的输入，再看读出来对不对：

```
W25N01GV 的 selftest（写后读回）：
  1. 擦除一个 block
  2. 写入已知 pattern（递增序列 0x00 0x01 0x02…）
  3. 读回，逐字节比对
  4. 全对 = SPI 通信 + 读写时序 + 地址映射 全对
  （pattern 自己定即可；但擦/写/读的命令字与时序来自 datasheet）

HM01B0 的 selftest（分层，从底到顶逐层定位）：
  Layer 1（A档，自动）：读 MODEL_ID，对上 datasheet 的 0x01B0 = SCCB 通信对
  Layer 2（B档，自动）：抓一帧，统计像素直方图
    - 全 0 / 全 255 = 没出数据 / 配置错（自动判）
    - 有合理分布 = 大概率在出图
  Layer 3（C档，用户/或用 test pattern 降级）：
    显示到 LCD 或传回，人确认画面；
    或开 test pattern 寄存器输出固定彩条，Agent 比对 → 降为 A 档
  → 每层失败都能精确定位问题在哪一层
```

---

## 5. STAGE 3 详解：集成

### 5.1 STAGE 3 子阶段划分

```
STAGE 3.1  任务架构设计（写代码前先想清楚）
  - 读各子系统 example，理解调用方式
  - 按状态机 + 任务启发式规划 FreeRTOS task
  - 定 task 列表、优先级、通信方式（queue / event group）
  - 这一步是"想清楚再写"，避免一上来就堆进 app_main

STAGE 3.2  实现 + 验证
  - 按 task 架构组 main.c（只调子系统语义接口，硬规则 H3）
  - 默认不改子系统；接口不够 → 回 STAGE 2 加接口重验
  - selftest = 验证【状态/逻辑正确性】（非数据正确性）
  - 失败处理走公共模块（第 6 节）
```

### 5.2 FreeRTOS 任务调配（STAGE 3 最难、最易被忽视的点）

> 编译成功但 task 没分好 = 典型"编译过但行为错"，最难查：
> 不报错，只表现为卡顿、看门狗复位、数据丢失、WiFi 断连。
> 录音+存储+上传并发（Problem 1）、采集+显示+图传并发（Problem 2）
> 都是任务调配密集型，这是 Problem 2 比 Problem 1 难的核心原因之一。

```
判断启发式（经验性，随实践沉淀，不写死）：

第一步：识别需要独立 task 的信号（命中任一就考虑独立）
  ① 阻塞等待：会 block（等按钮/等 WiFi/等 DMA）→ 不能放主循环
  ② 实时性要求：有帧率/采样率要求（音频采集、摄像头）
     → 独立 task + 合适优先级
  ③ 速率不匹配：两逻辑速率差很大（录音 16kHz 持续 vs 上传偶尔）
     → 拆生产者/消费者两 task + 队列
  ④ 长耗时操作：WiFi 上传、Flash 擦除 → 独立 task，别阻塞实时逻辑

第二步：典型 task 划分（以 Problem 1 为例）
  - audio_task（高优先级）：持续采集 mic，实时性强
  - storage_task：消费 audio 数据写 Flash
  - network_task（低优先级）：有 WiFi 时慢慢上传
  - main/button：监听按钮，控制状态机
  task 间用 queue / event group 通信，不共享裸变量

第三步：task 相关 failure signal（并入公共模块）
  - 看门狗复位 → 某 task 占 CPU 不让出（缺 vTaskDelay / 死循环 polling）
  - WiFi 断连/数据丢失 → 高优先级 task 饿死 WiFi task，或中断里干太多活
  - 数据错乱 → 多 task 共享变量没加锁 / 没用 queue
```

### 5.3 STAGE 3 改不改子系统？
```
默认原则：不改子系统库（STAGE 2 已独立验证过，改了会破坏已验证的东西，
          且分不清是子系统问题还是集成问题，且 register 错不报编译错）

允许改的唯一情况：接口不够用
  集成时发现子系统接口无法支撑状态机逻辑。
  例：audio 只有 audio_record_blocking()（录完才返回），
      但状态机需要"按住录、松开停" → 需 audio_record_start()/stop()
  → 回 STAGE 2 给子系统【加接口】，加完重过该子系统 selftest，再回 STAGE 3

判断规则：集成出错 → 先问"是状态机调用逻辑错，还是子系统接口不够"？
  调用逻辑错（顺序/条件/时机）→ 改 main.c
  接口不够（缺函数/语义不对）→ 回 STAGE 2 改子系统
  绝不在 STAGE 3 偷偷改子系统内部实现（register 读写那些）
```

---

## 6. 公共模块：失败处理（STAGE 2 / STAGE 3 共用）

> 核心：把"报错→修复"变成"报错→分类→定向可信来源→修复"
> （这是 IoTPilot Chain 1 的本质，用朴素的"分类+grep本地"实现，适合单 Agent）

```
═══════════ 编译失败 → 分类 → 定向修 ═══════════
  "unknown function / too many arguments"
     → API 签名错 → grep 本地 $IDF/components 的 header 确认真实签名，按它改
  "undefined reference"
     → 链接错 → 检查 CMakeLists 依赖、组件是否注册
  "implicit declaration"
     → 缺 include → 找该函数所在 header 补 include
  原则：定向到本地可信来源修，不让 LLM 凭记忆改

═══════════ 串口失败（编译过但行为不对）→ 定位层级 → 定向修 ═══════════
  无任何串口输出       → 启动就挂了（看 boot log / panic）
  有输出但数据全0/满幅  → 子系统数据问题（STAGE 2 本应排除）
  数据对但状态流转不对  → 状态机逻辑问题（STAGE 3）→ 改 main.c
  诡异卡顿/复位/断连    → task 调配问题（STAGE 3）→ 见 5.2

差异（定位到的问题归谁修）：
  STAGE 2：问题基本都在子系统内部（驱动、register、协议）
  STAGE 3：问题可能在状态机逻辑、task 调配、或接口不够
```

---

## 7. 两个测试用 Problem（验证框架用）

### Problem 1（中等）— Crumb 录音设备
```
芯片：ESP32-PICO-D4
子系统（5 个，相对独立）：
  电源  BQ25180   I2C   读充电配置/状态           [外部Sensor] 档位A(读ID)
  存储  W25N01GV  SPI   NAND 读/写/擦 + FIFO队列   [外部Sensor] 档位A(写后读回)
  音频  ICS-41350×2 PDM/I2S  采集 + 双mic处理       [外部Sensor] 档位B(PCM范围)
  网络  ESP32 WiFi  连接 + HTTP 上传               [MCU内置]
  输入  Button    GPIO  录音起止                   需用户按一下(极简)
核心逻辑：按住按钮录音→存 Flash；有 WiFi 按时间由远到近逐个上传后删除；
         无 WiFi 持续录音
建议实现顺序：电源/存储(独立易验证) → 音频(依赖存储验证"录的能存") →
            网络(依赖存储验证"存的能传") → main.c 状态机串起来
```

### Problem 2（较难）— Camera 图传设备
```
芯片：ESP32-S3（带 PSRAM）
子系统（3 个，强耦合）：
  摄像头 HM01B0  SCCB(I2C)+DVP  配置+采集   [外部Sensor] 稀有，必走 datasheet
  显示   ST7789  SPI   高带宽刷屏                [外部Sensor]
  网络   ESP32 WiFi  图像编码+传输             [MCU内置]
核心逻辑：HM01B0 采集 → 实时显示 ST7789；连 WiFi 时可切换为图传到网站
为何更难：① HM01B0 稀有，无 IDF example，必啃 datasheet（SCCB 寄存器/时序）
         ② 图像数据量大，算力压力高
         ③ 三路(DVP采集/SPI刷屏/WiFi传输)抢带宽，DMA+内存分配是关键
         ④ HM01B0 接口选择(DVP vs LCD_CAM)需 datasheet+IDF 文档共同决定
         ⑤ 实时性约束强，掉帧就卡，没有"存起来慢慢处理"的退路
         → 任务调配(5.2)在此成为成败关键
```

---

## 8. 全部硬规则汇总（速查）

```
H1.  必须用 ESP-IDF 框架，不用 Arduino 层
H2.  用户提供 IDF 版本号 + 安装位置，API 对齐本地版本
H3.  封装原则：main.c 只调语义接口，IDF 原生调用封装在库内
H4.  datasheet 永远是 sensor ground truth，记忆只是加速草稿
H5.  register 错不触发编译错，唯一兜底是硬件验证，校验必须前置
H6.  失败处理主动思考：分类→定向可信来源→修，不直接贴报错让 LLM 猜
H7.  子系统先做被依赖/易验证的；写一个验证一个，不攒到最后

派生规则：
- 端到端 ≠ 用户零参与，参与点可预期/最小化，STAGE 1 提前透明化
- C 档（需用户）优先用 test pattern/写后读回 降级为 A/B
- 必须确认时：攒到一起、看一眼回 y/n、走串口、给明确动作指令
- 现成库不直接信：读懂 → 写我们场景 example → 过硬件验证
- 记忆草稿二元裁决：全对采纳 / 一处错即整份作废，禁止中间状态
- 稀有 sensor（HM01B0）必走 datasheet
- GitHub 不自动扒取，卡住时由用户手动提供参考
- STAGE 2 验证数据正确性；STAGE 3 验证状态/逻辑正确性
- STAGE 3 默认不改子系统，只有"接口不够"才回 STAGE 2 加接口
- FreeRTOS 任务架构在写 main.c 前先设计（3.1）
- 失败处理是 STAGE 2/3 公共模块，不写两遍
```

---

## 9. 与四篇参考论文的对应关系

| 框架要素 | 对应论文 |
|----------|----------|
| STAGE 1 需求分解 | IoTPilot Problem Decomposition / AutoEmbed Functionality Separation |
| STAGE 2 逐子系统 | IoTPilot Func-coder Agent / AutoEmbed Knowledge Generation |
| example = 调用链 | IoTPilot SFG / AutoEmbed Utility Table（你的实现更直接：可编译的真实代码） |
| ground truth 分级 | IoTPilot 内外知识冲突 / EmbedAgent 本地 header 优先 |
| 失败处理主动思考 | IoTPilot Chain 1（关联挖掘/白黑名单） |
| 真实硬件验证 | IoT-SkillsBench（唯一做真实硬件，但单次生成；你做了闭环） |
| spec.md / datasheet_notes | IoT-SkillsBench Human-Expert Skills（专家知识结构化） |
| 编译反馈最有效 | EmbedAgent R1-Compiler（ESP-IDF 41.9% 错误是头文件/版本） |

> 本框架的独有优势：真实硬件 + baseline + 闭环验证，是四篇论文里验证最强的设计。
```
> Historical design record only. The operational source of truth is `SKILL.md`
> plus `references/run-state.md`; where this document differs, follow those files.
