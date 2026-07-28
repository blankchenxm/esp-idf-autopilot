# ESP-IDF AutoDev 架构重构讨论记录（归档）

> Archived on 2026-07-21 from the former root `REFACTOR_TODO.md`. This file preserves the full discussion and F01-F37
> migration inventory. Historical `[x]` marks mean that a rule received an intended carrier;
> they are not proof that every carrier is fully implemented. The audited implementation status,
> including remaining post-MVP hardening, is maintained in `todo.md`.

> 状态：讨论中，尚未授权开始整体重构。
>
> 用途：持续记录本轮架构讨论中的目标、约束、决定、待确认项、实施任务和验收标准，避免对话压缩或跨轮讨论造成遗漏。
>
> 更新规则：每个讨论要点必须进入本文件；尚未决定的内容标记为 `OPEN`，明确同意的结论标记为 `DECIDED`，不得把建议自动写成既定需求。

## 1. 已确认的背景问题

### P1 — 流程完整性不足

- 当前主要依靠 `.agents/skills/esp-idf-firmware/SKILL.md` 驱动开发流程。
- 现有流程较长，虽然单个步骤已经相对规范，但仅依赖模型在上下文中持续遵循 `SKILL.md`，中途仍可能遗漏门禁、证据、验证或收尾步骤。
- 需要让流程完整性由可检查的结构和状态保证，而不只依赖模型记忆。

### P2 — 连续自动执行不稳定

- 目标是 Stage 1.5 获得一次批准后，尽可能连续完成实现、构建、烧录、串口验证、集成、closure 和 release。
- 当前执行有时会在内部阶段、工具调用、失败修复或上下文切换后提前中断。
- 需要明确区分：允许等待用户的外部门禁、可自动修复的内部失败、真正无法继续的外部阻塞。

### P3 — 人工参与过多或出现时机不确定

- 用户希望整个流程尽可能少地要求人工确认和现场操作。
- Tier A、Tier B 检查应自动执行。
- 无法通过硬件或协议自动观察的 Tier C 项目，应在流程前期预先声明，并在最后一次性集中确认。
- 不应在开发中途临时增加普通按钮操作、重复批准、是否继续等人工请求。

## 2. 本轮重构的总体目标

| ID | 目标 | 当前状态 |
|---|---|---|
| G1 | 长流程中的每个必经阶段、门禁和证据都不会因为上下文变化而丢失 | DECIDED |
| G2 | Stage 1.5 后能够连续自动执行，直到 release、预先声明的 Tier C、用户暂停或有证据的硬阻塞 | DECIDED |
| G3 | 尽可能消除人工操作；所有可测量、可注入、可回读的行为自动验证 | DECIDED |
| G4 | 流程状态、下一动作、失败原因和完成条件可由程序检查，而不是依赖自然语言自觉 | DECIDED — LangGraph、strict contract、validators 和 graph tests 承担 |
| G5 | Skill、脚本、Hook、状态文件和日志各自只有清晰职责，避免多处定义互相冲突 | DECIDED — 采用 Skill/AGENTS/Docs/Graph/State 的唯一权威归属 |
| G6 | 中断、崩溃、上下文压缩或重新启动后，可以从权威状态恢复，不重复已完成工作 | DECIDED — checkpoint + receipts/evidence + idempotency key 恢复 |
| G7 | 项目代码、组件接口、自测、日志和生成产物采用一致且可验证的目录与格式 | DECIDED — 采用根/局部 AGENTS、标准项目布局与 schema/contract tests |

## 3. 暂定设计原则

以下为讨论起点，不代表已经全部批准。

| ID | 原则 | 状态 | 说明 |
|---|---|---|---|
| D1 | `SKILL.md` 是 firmware Harness 的触发/使用合同和规则索引，不承担执行顺序或运行时状态 | DECIDED | 完整顺序进入 Graph/Contract；架构解释进入 docs；Repo/目录规则进入 AGENTS |
| D2 | LangGraph checkpoint 是控制状态权威；receipts/evidence 是事实权威；`run-state.json` 是供 Hook/人读取的只读投影 | DECIDED | 保持当前 Stop hook 读取接口，不让 Agent 自由写投影 |
| D3 | 状态推进必须通过显式 gate/transition，而不是自由修改 cursor 字符串 | DECIDED | Graph 路由和 deterministic validators 计算合法下一步 |
| D4 | 完成状态由与设计/固件/硬件/hash 绑定的证据校验产生，不允许 Agent 仅凭文字声明 PASS | DECIDED | NodeResult/Receipt/Evidence/GateResult 使用 strict schema |
| D5 | 内部失败自动进入 diagnose → patch → reverify，不产生用户确认点 | DECIDED | 四类 failure policy、bounded transient retry 和 progress fingerprint |
| D6 | 外部阻塞必须包含类别、证据、已尝试方案和唯一所需输入 | DECIDED | 仅 HARD_EXTERNAL_BLOCKER 可终止连续执行 |
| D7 | Stop hook 只做最终防线，不承担主要流程编排；保持 `run-state.json` 读取兼容，等 runner 稳定后再做最小去重/故障终态适配 | DECIDED | 当前不先重写 Hook；主要连续执行、停滞检测和恢复由 LangGraph runner 承担 |
| D8 | 所有硬件交互都要有可重复、可保存、可判定的自动测试接口 | DECIDED | 串口 adapter、自测协议、故障注入、host-side verifier 和 evidence gate |
| D9 | 用户只参与 Stage 1.5、不可避免的单次 Tier C，以及用户主动暂停/改需求 | DECIDED | 真正缺失 secret/artifact 或危险外部授权归入有证据的 hard blocker，不新增普通工程确认点 |

## 4. 架构问题结论索引

### A. 权威流程由什么承载

- [x] Skill 是 Harness 入口、调用合同和规则索引，不再承担完整运行时顺序。
- [x] 使用独立 workflow/state-machine：Python LangGraph + JSON Execution Contract。
- [x] Python Graph 承担运行拓扑，严格 JSON Contract 承担项目特定任务/约束，两者通过 schema/version 绑定。
- [x] Graph router 和 deterministic Gate 计算合法 `next_action`；Agent node 只能返回结构化建议/结果。
- [x] 通过 topology、reachability、gate precondition、terminal validator 和 parity tests 阻止跳过必经门禁。

### B. 连续执行控制

- [x] 主循环由显式 LangGraph runner 驱动；Stop hook 只读取兼容投影并做最终防线。
- [x] checkpoint 控制状态与 immutable receipt/evidence/event 分离；通过 operation/attempt ID 和 atomic commit 更新。
- [x] 使用 progress fingerprint、material artifact/hash change 和 attempt history 检测“有输出无进展”。
- [x] 相同 action 通过 idempotency key、receipt lookup 或从最近安全事务边界重做。
- [x] 按 failure category/node/attempt 使用 bounded retry；重复 failure fingerprint 且无 material change 判定 stalled。
- [x] state/attempt 中保留 failure signature、progress sequence、last material progress 和 receipt IDs；不在聊天摘要中维护。
- [x] 只有 HARD_EXTERNAL_BLOCKER 进入 `BLOCKED`；内部失败持续 repair。是否额外暴露 `FAULTED/STALLED` 名称可在实现时简化，但语义必须存在。

### C. 自动恢复

- [x] CLI 通过显式 project/run ID 或 checkpoint 中唯一 active run 发现；多个 active run 时不得猜测。
- [x] 从 LangGraph checkpoint + bound design + committed receipts/evidence 恢复。
- [x] artifact path/hash、attempt ID、firmware hash、hardware session 和 test ID 共同验证 evidence provenance。
- [x] 副作用先生成/恢复 receipt，再原子提交 state；crash window 通过外部事实 probe + receipt reconciliation 处理。
- [x] 所有副作用定义 idempotency/reconciliation 语义；无法幂等时从明确事务边界安全重做。

### D. 人工交互最小化

- [x] Stage 1.5 一次包含行为解释、assumption/policy、硬件关键事实、selection/version、protocol、credential reference、limits、verification 和 Tier C。
- [x] Secret 只能由用户提供或引用；保存 secret reference/env name，不进入 Contract/log/evidence。
- [x] Firmware 范围内的 build/flash/selftest 按 approved Contract 自动执行；缺失授权的危险外部系统写入作为预声明 policy 或 hard blocker，不临时扩大权限。
- [x] Tier C 只允许当前硬件/协议无法自动观测的最终物理现象；设计 validator 必须先证明 A/B 自动化已最大化。
- [x] 用户 change/pause 形成事件，创建 Design amendment/revision，计算 affected DAG 后恢复；不从头重跑无关 subsystem。

### E. 代码与目录规范

- [x] 目标 repo/project 布局已在“重构完成后的仓库结构”和“执行后单个项目结构”中确定。
- [x] Selection canonical data 在 Contract，原始查询为 design evidence/receipt；可生成 Markdown view 但不是第二权威。
- [x] Semantic API、ownership、lifecycle、failure/thread-safety 进入 Contract + `projects/AGENTS.md` + source tests。
- [x] Selftest 使用统一可配置 gate、明确 command/marker/expected；release 检查 effective selftest-off，不依赖手改文本。
- [x] logs/receipts/evidence 以 run/attempt/test/artifact hash 命名并绑定 provenance；具体 filename helper 在 storage 实现。
- [x] requirements/connections 用户所有；managed/checkpoint 生成器所有；project-owned component/main 由受限 implementation node 修改。

### F. 测试与完成判定

- [x] Contract traceability + typed test/evidence IDs + current hash bindings 计算每个 R/DR verdict。
- [x] Subsystem/Integration/Closure/Release 均由 deterministic GateResult 和 required EvidenceContract 判定退出。
- [x] Release build profile/effective config、firmware hash、flash receipt 和 runtime evidence 必须一致且 selftest-off。
- [x] Integration/closure test plan 强制包含相关 resource/restart/failure cases 和定量 bounds。
- [x] Test-owned cleanup、declared endpoint/fixture、credential redaction 和 destructive-path checks 属于强制 safety policy。

### G. Skill 与 supporting files 的拆分

- [x] Skill 保留 activation、inputs、runner 命令、human gates、capability routing 摘要、outputs/response/failure fallback。
- [x] 调用所需的小型接口/故障参考进入 Skill references；系统设计说明进入 docs；strict schema 进入代码/`schemas/`。
- [x] State/JSON/digest、build/flash/serial、receipt/evidence、cleanup 和 transition 均由 runner/adapters 实现。
- [x] F01–F37 parity inventory + schema/version compatibility + graph/gate tests 检测漂移。
- [x] 必须建立仓库级 lint、contract、topology、adapter cleanup、resume/idempotency 和 terminal tests。

## 5. 实施状态

- [x] 定义新的整体架构图和职责边界。
- [x] 定义 workflow 状态、transition、failure 和 progress schema/policy。
- [x] 定义 versioned design/receipt/evidence/artifact/event/run-state schema。
- [x] 实现 `python -m orchestrator.cli` 的 validate/run/resume/revision/status 接口。
- [x] 生成 Harness v2 精简 Skill 并归档旧版；活动 `.agents` 原子替换因本会话只读挂载而暂存于 `docs/archive/firmware-skill-baselines/v2-harness-candidate/`。
- [x] 将 run-state/DEVLOG 的事实职责迁移为 checkpoint + append-only events + immutable receipts/evidence + read-only projection。
- [x] 保持旧 Stop hook 的根级 `run-state.json` 读取兼容，同时生成标准 `execution/run-state.json`；连续推进、停滞指纹和硬终态由 runner 承担，Hook 不重新承担编排。
- [x] 为合同、DAG、Registry gate、Tier、状态、存储不可变性、adapter cleanup、failure policy、graph topology、Skill parity、Hook 和 terminal 增加自动测试。
- [x] 通过 `projects/AGENTS.md`、source boundary 和 smoke component 建立 component/`main/` 规范模板。
- [x] 使用隔离的 `projects/harness_smoke` 做端到端迁移试验，不恢复或覆盖用户删除的 `projects/crumb`。
- [x] 在已连接 ESP32-PICO-D4 Crumb 上验证 Stage 0 到 Stage 3.6：COM4、MAC `c8:85:41:4a:49:e8`、closure 1/1、fresh release marker `HARNESS_RELEASE_READY`。

## 6. 初步验收标准

以下标准已经量化并由 smoke/自动测试验证。

- [x] 从 Stage 1.5 批准后开始，在不存在 Tier C 或外部阻塞时，无需再次询问用户即可到达已验证 release。
- [x] 外层命令中断后，同一 thread 从 SQLite checkpoint 的失败节点恢复并执行正确的下一动作。
- [x] 任一必经 gate 缺少证据时，validator/graph topology/terminal validator 禁止进入后续 gate 或 COMPLETE。
- [x] progress fingerprint 和 bounded retry policy 能识别无进展并形成分类故障，主循环不依赖重复 Stop-hook 提示。
- [x] smoke R1 具有 contract、owner、test、raw build/flash/serial、Receipt、Evidence、verdict 和 closure 绑定。
- [x] release build、flash、serial runtime、firmware SHA-256 和 selftest-disabled 状态相互一致。
- [x] Tier A/B validator 禁止 `user_involvement=true`，简单输入由自动化 evidence contract 承担。
- [x] Graph 只在 digest approval 和预声明 Tier C batch 使用 `interrupt()`。

## 7. 决策日志

| 日期 | ID | 决定 | 原因 | 影响文件/模块 |
|---|---|---|---|---|
| 2026-07-21 | LOG-001 | 建立独立重构讨论 TODO；讨论完成前不开始整体重构 | 防止讨论要点丢失，并与现有短期 `todo.md` 分离 | `REFACTOR_TODO.md` |
| 2026-07-21 | LOG-002 | Spec Kit Design Package 采用 5 文件 MVP | 保留完整逻辑领域，同时减少物理文件、跨文件漂移和初期实现复杂度 | Design Subgraph、Schema、Approval、LangGraph handoff |
| 2026-07-21 | LOG-003 | LangGraph 采用 4 子图/12–16 checkpoint 节点的 MVP，并接受最小异常与 Hardware Identity 策略 | 保证连续执行和安全恢复，同时避免为尚未发生的异常建立庞大 Graph | Orchestrator、Checkpointer、Adapters、Receipts、Hardware Probe |
| 2026-07-21 | LOG-004 | 采用 Root AGENTS + scoped AGENTS + Docs + 精简 Skill + 可执行 Graph 的职责分层 | 消除 440 行流程由模型重复解释的问题，同时保留完整工程知识 | `AGENTS.md`、`.agents/skills/`、`docs/`、`orchestrator/`、`projects/` |
| 2026-07-21 | LOG-005 | 以当前 422 行 Skill 建立 F01–F37 零丢失迁移基线，并补齐 Preflight、Revision、history、Registry 范围和 serial/selftest 安全缺口 | Harness 重构必须更可靠地执行旧规范，而不是通过换架构静默删减规范 | Migration parity tests、Top-level Graph、Project artifacts、Adapters、Policies |

## 8. 讨论追加区

### Harness 总体架构：Spec Kit + LangGraph

- 用户关注：单靠 `SKILL.md` 难以保证长流程不遗漏，也难以稳定连续执行；希望用 Harness 架构把设计阶段和固件执行阶段分开。
- 用户提出的总体结构：
  - **Spec Kit：设计时 Harness**，采用 SDD（Spec-Driven Development）。
  - 流程：`Constitution → Specify → Clarify → Plan → Checklist → Tasks → Analyze`。
  - 主要产物：`spec.md`、`R/DR` 验收矩阵、子系统依赖图、Verification Plan、Tier C Plan、FreeRTOS 架构。
  - **LangGraph：固件执行 Harness**。
  - 流程：`Preflight → Datasheet → Spec Approval → Component Loop → Build → Flash → Serial → Tier C → Integration → Closure → Release`。
- 当前判断：方向合理，能够把“设计正确性”和“执行连续性”分开治理，优于让根 `SKILL.md` 同时充当规范、状态机、执行器和恢复机制。
- 推荐的整体心智模型：
  - Spec Kit 是前端/设计编译器，负责把用户意图变成完整、可检查的工程设计。
  - 中间需要一个版本化、机器可读的 **Execution Contract / Workflow IR**。
  - LangGraph 是执行后端，消费已批准的 contract，推进硬件闭环并产生证据。
- 已确认约束：
  - `spec.md` 不能成为两个 Harness 之间唯一的机器接口；需要结构化、可校验、可版本化的中间表示。
  - LangGraph 不应在执行期间重新解释或悄悄改变已批准的产品政策、需求边界或 Tier C 决定。
  - 设计产物进入执行前必须冻结版本/hash；执行中的需求变化必须显式返回设计 Harness 重新分析影响。
  - LangGraph checkpoint/chat state 不能单独充当项目事实来源；项目目录中必须保留可审计、可恢复的权威状态与证据。
  - 确定性工作应由确定性节点完成；LLM 节点用于需要工程判断的有限任务，不能把整个阶段包装成一个无法检查的“大 Agent 节点”。
- 需要解决的职责重叠：
  - Datasheet acquisition/validation 是设计 grounding 的一部分，但也需要在执行前确认工件可用；建议实现为两个 Harness 共享的服务/节点，而不是各自独立重复一套逻辑。
  - Component selection 原则上属于设计和计划，但执行前需要 revalidate 选定版本和依赖可用性；需要区分 selection decision 与 execution validation。
  - Spec Approval 是设计 Harness 的终点还是执行 Harness 的入口，需要确定唯一所有者和原子化 handoff。
  - Preflight 是否必须发生在全部设计之前，或只需在进入硬件执行前完成，需要结合“不让用户批准一个根本无法执行的计划”来决定。
- 建议的节点分类：
  - **Deterministic nodes**：schema 校验、状态转换、artifact/hash 校验、build、flash、日志收集、证据存在性检查、acceptance closure 计算。
  - **Agentic nodes**：需求澄清草案、datasheet 信息提取、实现、失败分类、定向修复、架构影响分析。
  - **Human gates**：一次 Spec Approval、预先声明且不可自动化的单次 Tier C、用户主动暂停/变更需求。
- 每个 LangGraph 节点需要定义：输入、前置条件、输出、允许修改的文件、证据、成功条件、失败分类、重试策略、超时、幂等/恢复语义、合法下一节点。
- 当前决定：`DECIDED — 采用 Spec Kit Design Subgraph + LangGraph 顶层 Orchestrator + versioned Execution Contract；职责边界、MVP 状态持久化和节点粒度已在后续章节确认。`
- 影响范围：根 `SKILL.md`、Spec Kit 接入层、workflow schema、LangGraph runner、run-state、DEVLOG/evidence、Stop hook、项目目录规范和测试体系。

### 概念澄清：顶层 Orchestrator、Spec Kit 产物与 Datasheet 归属

- 用户问题 1：什么叫 LangGraph 是顶层 Orchestrator。
- 澄清：顶层 Orchestrator 指拥有整个运行生命周期的唯一控制者；它读取权威状态、决定当前允许进入哪个子流程、处理 checkpoint/恢复和人工 interrupt，并在子流程完成后选择合法下一步。它不意味着所有设计逻辑都必须写进 LangGraph 节点。
- 两种可选部署方式：
  - **父子模式**：LangGraph 从项目开始即运行，把 Spec Kit 当作设计子流程调用；适合追求从需求到 release 的一键式体验。
  - **顺序交接模式**：用户先单独运行 Spec Kit，在批准设计包后启动 LangGraph；结构更简单，但需要一个明确、原子化的 handoff。此时 LangGraph 是固件执行阶段的顶层 Orchestrator，而不是设计阶段的控制者。
- 当前决定：两种方式理论上都成立，但本项目已在后续讨论中选择父子模式 A：LangGraph 拥有整个 run，Spec Kit 是 Design Subgraph。`DECIDED`。

- 用户问题 2：Spec Kit 是否先审核/分析要求并最终产生 `spec.md`。
- 澄清：是。设计 Harness 从 `requirements`/`connections` 开始，执行 specify、clarify、grounding、plan/checklist/analyze，最终生成可审批的 grounded `spec.md`。其中“审核”是检查歧义、遗漏、矛盾、不可验证要求和需要用户决策的政策项，不是擅自改变原始要求。
- 关于 `plan.md` 和 `tasks.md`：
  - 用户之前只要求一个 `spec.md`；因此不能把额外人类文档自动设成必须交付物。
  - Spec Kit 的 `Plan`、`Tasks` 是必要的设计概念，但不等于必须保存为独立 Markdown 文件。
  - 候选方案 A：所有人类可读内容仍集中在 `spec.md`，另生成机器可读的 execution contract/task DAG。
  - 候选方案 B：采用 Spec Kit 原生的 `plan.md`、`tasks.md`，`spec.md` 保持只描述 what/acceptance；适合希望人工审阅工程方案的项目。
  - 当前决定：采用方案 A；只向用户交付一个 `spec.md`，Plan/Tasks/DAG 保存在机器 Contract 内，不生成独立 `plan.md`/`tasks.md`。`DECIDED`。
- 中间产物的用途：是，它们为 LangGraph 提供机器输入。应描述子系统、依赖、前置条件、预期输出、验证方法、证据和 gate，而不是要求 LangGraph重新从长篇自然语言推断执行顺序。
- 需要避免的误解：中间 task DAG 定义“必须完成什么以及如何判定完成”，不必把每行固件实现预先写死。具体代码实现、诊断和定向修复仍可由受约束的 Agent 节点完成。

- 用户问题 3：Datasheet 分析是否应进入设计阶段，以及原 spec 是否过于简略。
- 澄清：是。最终提交审批的硬件 grounded spec 必须建立在已验证 datasheet 上，否则无法可靠确定 pin/active level、电压与接口约束、初始化/ready 顺序、timing、寄存器身份检查和 verification baseline。
- 设计阶段不应把整本 datasheet 复制进 `spec.md`：
  - `spec.md` 保存会影响产品行为、架构、验收和用户审批的关键事实及引用。
  - `components/<sub>/datasheet_notes.md` 保存寄存器、位域、精确初始化步骤、时序和 gotchas 等驱动级 ground truth。
  - execution contract 保存 LangGraph 需要机器读取的约束、节点输入和验收值。
- 推荐的设计信息流：
  - `requirements + connections`
  - `→ hardware/subsystem inventory`
  - `→ datasheet acquisition + identity validation + notes`
  - `→ ambiguity/policy clarification`
  - `→ grounded spec.md`
  - `→ execution contract/task DAG`
  - `→ single Spec Approval`
- 当前决定：`DECIDED — Datasheet grounding 必须发生在最终 Spec Approval 之前；spec.md 保留决策和验收相关事实，完整驱动细节留在 datasheet_notes，机器执行约束进入 execution contract。`

### 架构选择与 Datasheet 分层阅读策略

- 用户决定：采用 **模式 A**，由 LangGraph 作为顶层生命周期 Orchestrator，并把 Spec Kit 作为 Design Subgraph。
- 用户决定：只向用户生成/展示一个人类可读设计文档 `spec.md`；Plan、Tasks、task DAG、execution contract、artifact manifest 等仍可由系统自动生成，但不要求以独立 `plan.md`/`tasks.md` 作为用户交付物。
- 用户决定：Datasheet 必须在设计阶段、最终 Spec Approval 之前开始阅读和 grounding。
- 当前决定：`DECIDED — Mode A；单一用户文档 spec.md；其余执行产物机器生成。`

- 用户关注：现有 Datasheet 流程主要是 PDF 文本提取和简单阅读，必要时依赖 IC 先验知识或网络搜索；未来可能开发专门 Skill 深读复杂 Datasheet。需要判断是否必须在设计阶段一次性提取全部寄存器和实现细节。
- 当前判断：不需要对每颗器件一开始就做同等深度的穷尽阅读。应采用 **progressive/risk-based grounding**，但所有会影响设计、审批、安全和验收的事实必须在 Spec Approval 前验证。
- Datasheet 阅读分层：
  - **L0 Identity（所有外部器件必需）**：精确型号/变体、文档 ID/revision、PDF 内容有效、型号身份匹配。
  - **L1 Design Grounding（Spec Approval 前必需）**：供电和电气边界、pin/active level、接口/地址/模式、外部硬件依赖、reset/ready 约束、影响架构的数据率/timing/resource 上限、身份/自测能力、安全 warning、可用于 verification 的基线、已知不确定项。
  - **L2 Implementation Grounding（该子系统编码前按路径要求）**：初始化顺序、命令/寄存器/位域、延迟、状态机、错误检测与恢复、读写限制、驱动需要的 edge cases。
  - **L3 Deep/Edge Analysis（风险或失败触发）**：复杂时序、异常恢复、未文档行为、功耗模式交互、长时间稳定性、errata 和疑难章节；可由未来专用 Datasheet Skill 承担。
- 不同实现路径需要不同阅读深度：
  - **采用成熟 Registry/vendor 库**：先读库 README、API、examples、兼容性和限制；Datasheet 至少完成 L0/L1，并验证库是否覆盖项目要求。无需为了形式把所有未直接使用的寄存器抄入 notes。
  - **使用本地 ESP-IDF example/built-in API**：本地 example/header 控制 API 和调用方式；Datasheet/技术参考仍控制电气、时序、数据格式和器件行为。
  - **自研外部器件 driver**：在编码前必须完成与所用功能相关的 L2；缺失寄存器、初始化或 timing 事实时不得凭记忆补齐。
  - **出现硬件异常、库行为冲突或要求超出库覆盖范围**：进入 L3，重新读取原文、errata、官方 application note 或库源码。
- `datasheet_notes.md` 的定位：
  - 是带来源、章节/page、验证状态的工程知识缓存和跨节点交接物。
  - 不是原始 Datasheet 的永久替代品，也不意味着执行阶段禁止重新打开 Datasheet。
  - 执行节点应先读 notes；需要的事实缺失、冲突或不足时，重新读取原文，更新 notes 和证据后再编码。
  - notes 中的事实应标记 `VERIFIED`、`UNKNOWN`、`DEFERRED` 或 `NOT_APPLICABLE`，不得用先验知识把未知项伪装成已确认。
  - 模型记忆和网络搜索可提供搜索词、候选解释和定位线索；影响代码/验收的事实必须回到官方 Datasheet、errata、官方文档、本地版本代码或选定库的正式资料验证。
- 执行期补充阅读的变更规则：
  - 仅补充寄存器/实现细节，且不改变用户批准的行为、pins、架构、验收、限制或 Tier C：自动更新 notes/execution contract，不再请求用户批准。
  - 若新事实改变产品行为、接线、架构、验收方法、已知限制、风险或 Tier C：使当前 contract 失效，返回 Design Subgraph 生成 spec amendment，并再次请求针对变化范围的批准。

- 关于 `spec.md` 面向非专业用户的详细度：
  - `spec.md` 不应成为 Datasheet 摘抄或要求用户审查寄存器表。
  - 单文件采用分层结构：顶部是短小的“需要你确认什么”，中部是可理解的行为/硬件/验收摘要，后部是技术附录和 R/DR 可追踪信息。
  - 用户主要批准产品行为解释、关键假设、政策选择、外部协议/凭证方式、可感知限制、Verification/Tier C 方案，而不是底层驱动实现细节。
  - `spec.md` 不能只列不确定项；还必须明确列出系统将据此实现的已确认解释和验收结果，否则“批准”没有确定对象。
  - 每项内容需区分：`USER-STATED`、`DATASHEET-VERIFIED`、`REQUIRED-DERIVED`、`ASSUMPTION`、`POLICY/OPEN`、`DEFERRED-IMPLEMENTATION`。
- 推荐的单一 `spec.md` 阅读层级：
  1. `Review summary / 需要用户决定的事项`
  2. `系统行为与状态机（通俗描述）`
  3. `硬件与接线摘要（只列关键约束和风险）`
  4. `子系统、依赖和 FreeRTOS 架构摘要`
  5. `Verification Plan 与 Tier C Plan`
  6. `R/DR Acceptance Matrix`
  7. `技术事实、来源和 deferred implementation 附录`
- 当前决定：`DECIDED — 采用分层、按风险推进的 Datasheet grounding；notes 是可更新的有来源缓存，不禁止执行期回看原文；spec.md 面向非专业用户采用 review-first 的单文件分层结构。`

### Datasheet 深读 Skill 与三类子系统实现路由

- 用户补充：正在开发一个可同时详细读取 Datasheet 图片与文字的专用 Skill，主要服务于子系统库实现，尤其是 custom driver。
- 用户确认：接受 review-first 的 `spec.md` 单文件结构、来源/不确定性分类，以及 notes/contract 的职责划分。
- 当前决定：`DECIDED — 专用 Datasheet 深读 Skill 不要求对所有器件在设计阶段无条件运行；当进入 custom driver、库覆盖不足、复杂器件或验证失败时，由 LangGraph 在 implementation 前调用它完成 L2/L3 grounding。`
- 调用深读 Skill 前置条件：
  - 已完成 L0 型号/文档身份验证。
  - 已完成足以支持 Spec Approval 的 L1 设计事实。
  - Component selection 已确认无可接受库、已选库缺少所需行为，或失败证据表明必须回到器件原理/寄存器层。
- 深读 Skill 预期输出：带 page/section/figure/table 来源的寄存器、位域、初始化、时序、图表含义、状态转换、错误恢复、验证方法和未解决项；输出更新 `datasheet_notes.md`，而不是直接以模型回答代替工程工件。
- 若深读发现只影响内部实现的事实：自动更新 notes/contract 并继续，不新增人工 gate。
- 若深读发现改变已批准行为、接线、架构、验收、限制或 Tier C：使 contract 失效，返回 Design Subgraph 生成最小范围 spec amendment。

- 回顾既有讨论/文档，确认此前已经存在相同基础思想：
  - `crumb-agent-framework.md` Stage 2 将子系统分为 `[MCU内置]` 与 `[外部Sensor]`。
  - MCU 内置路径明确要求优先查看本地 `$IDF_PATH/examples`，再查本地 component headers，`esp-docs` 和模型记忆只作辅助。
  - 外部 Sensor 路径明确要求先 `search_components(<part>)`；找到后阅读 README/example 并封装现成库，找不到后才获取和提炼 Datasheet。
  - 早期框架已经声明“能不造轮子就不造”，但现成库仍必须通过本项目的 example/selftest/真实硬件验证；现成库反复不适配时回退 Datasheet 路径。
  - 旧 `SKILL_claude.md` 也保留了 `[MCU-native] → local examples/headers` 与 `[External Sensor] → Registry → Datasheet` 的决策树。
- 早期方案与新方案的差异：早期只在前期预拉取 PDF、通常不立即阅读；新方案在审批前增加 L0/L1 grounding，仍把昂贵的 L2/L3 深读延迟到 implementation/custom-driver 需要时，因此既减少浪费，又避免未经硬件事实支撑的 spec。

- 正式候选实现路由更新为三类：
  1. **MCU-native / ESP-IDF built-in**
     - 例如 Wi-Fi、GPIO、I2S、SPI、NVS、HTTP。
     - 本地、当前版本的 ESP-IDF example/header/source 优先。
     - 项目代码负责把现有 API 正确编排并封装为 semantic component API，不重复实现 IDF 已提供的底层功能。
     - `esp-docs` 用于概念和指南，所有签名/配置以本地版本为准。
  2. **External part with trusted reusable library**
     - 先通过 ESP Component Registry MCP 按型号搜索，再按能力/interface 搜索；获取候选详情。
     - 优先采用满足目标芯片、IDF 版本、精确器件/模式和需求覆盖的可信库。
     - 读取库 README、examples、headers、Kconfig、依赖、license、维护状态和限制；封装成项目 semantic API。
     - Datasheet 完成 L0/L1，并用于验证电气/时序/数据格式、库覆盖范围和硬件验收；不因库存在而跳过真实 build/flash/serial/selftest。
     - Registry 没有合适项时，可评估官方厂商/Espressif 资源或可审计的第三方网络资源；网络代码只能在来源、license、版本兼容、接口覆盖和维护风险被记录后采用，不能仅凭“常见 Sensor”直接复制。
  3. **External part requiring custom driver**
     - Registry、官方库和可接受网络资源均无合适实现，或现有库不能满足要求/验证失败时进入。
     - implementation 前调用专用 Datasheet 深读 Skill 完成相关 L2，复杂/失败场景完成 L3。
     - driver 仅基于已验证 notes/原文事实实现；模型先验和网络文章仅用于定位线索。
     - 生成项目 component、semantic API、example/selftest，并走完整硬件闭环。
- 当前决定：`DECIDED — 保留并正式化 MCU-native / trusted library / custom driver 三路路由；所有外部器件先查 Registry，custom driver 按需触发专用 Datasheet 深读 Skill。`

### Datasheet Reader 可替换性

- 用户确认：当前阶段继续使用现有的 PDF 获取、文本提取和基础文字阅读能力，不等待新的多模态 Datasheet Skill 完成。
- 当前决定：`DECIDED — Datasheet 阅读在 Harness 中定义为可替换 capability/provider，不把 workflow、LangGraph node 或 execution contract 绑定到具体 Skill 名称。`
- 当前 provider：完成 PDF 获取、型号/文档身份验证、文本提取和基础 L0/L1 grounding；能力不足的图表/图片/复杂时序必须标记 `DEFERRED` 或 `UNKNOWN`，不能伪装成已验证。
- 未来 provider：可替换为能够阅读图片、表格、框图、时序图和复杂文字的专用 Datasheet Skill，并承担 custom-driver 前的 L2/L3 深读。
- Provider 兼容契约至少包括：输入 PDF/part/所需功能/目标 grounding level，输出结构化 `datasheet_notes.md`、来源定位、coverage、unknown/deferred 项、reader 名称和版本、完成 verdict。
- LangGraph 只依赖上述输入输出契约：更换 reader 后，Component Selection、Spec Approval、Implementation、Verification 和 Closure 节点无需改写。
- 允许两种升级方式：全局替换默认 reader；或当前 reader 完成 L0/L1、在 custom driver/复杂器件/失败触发时路由到 deep reader。

### Spec Kit Design Subgraph 候选详细设计

- 用户提出的初稿包含：`spec.md`、`manifest.json`、`requirements.json`、`subsystems.json`、`verification-plan.json`、`datasheet-manifest.json`、`tier-c-plan.json`、`architecture.md`、`acceptance-matrix.json`、`approval.json`。
- 当前评价：核心方向正确；它已经把 Spec Kit 从“写一篇 spec”提升为“把用户要求编译为 LangGraph 可执行合同”的设计时 Harness。
- 需要修正的主要问题：
  - `requirements.json`、`verification-plan.json`、`acceptance-matrix.json` 可能重复保存 tier/expected/owner，容易漂移；必须定义 canonical owner 和 derived view。
  - 已决定用户只接收一个人类文档，因此 `architecture.md` 不应成为第二个用户文档；架构摘要进入 `spec.md`，机器结构进入 `architecture.json`。
  - 当前缺少 `component-selection.json`，无法承接 Registry/library/custom-driver 决策。
  - 当前缺少最终给 LangGraph 使用的 `execution-contract.json`/task DAG。
  - 当前缺少 `decisions.json`，无法机器追踪 clarify、assumption、POLICY 和用户选择。
  - 当前缺少 `design-validation.json`，无法证明 Checklist/Analyze 已通过。
  - `approval.json` 不能由设计 Agent 自己宣布 APPROVED，也不能只绑定 `spec.md`；必须由人工 approval gate 写入，并绑定整个 canonical design payload 的 digest。
  - Approved package 必须不可变；任何设计变化生成新 revision，不能原地修改已批准 JSON。

- 推荐目录候选：

```text
design-package/
├─ current.json                       # 指向当前 draft/approved revision，不保存设计事实
└─ revisions/
   └─ rev-0003/
      ├─ spec.md                      # 唯一用户可读/需审批文档
      ├─ manifest.json                # package 索引、schema/generator/input/file hashes、design digest
      ├─ inputs.json                  # requirements/connections/constitution 的来源与快照 hash
      ├─ requirements.json            # R/DR 的 canonical 定义与来源
      ├─ decisions.json               # assumptions、clarifications、POLICY、用户选择、credential refs
      ├─ datasheet-manifest.json      # 器件文档、L0/L1 coverage、notes/provider/unknowns
      ├─ component-selection.json     # Registry/official/third-party/custom 的候选与决定
      ├─ subsystems.json              # 子系统边界、typed dependency DAG、owned requirements/interfaces
      ├─ architecture.json            # component/task/communication/resource/timing/failure architecture
      ├─ verification-plan.json       # tests、tiers、stimulus、oracle、pass expression、evidence contract
      ├─ tier-c-plan.json             # 不可自动化证明、自动证据前置条件、单批用户动作
      ├─ acceptance-matrix.json       # 由 requirements + verification 生成的只读追踪视图
      ├─ execution-contract.json      # LangGraph nodes/gates/transitions/retry/allowed writes/evidence
      ├─ design-validation.json       # Checklist/Analyze/lint 结果
      └─ approval.json                # 独立 approval gate 对 design digest 的签认
```

- Constitution 建议为仓库级长期规则，不在每个 package 内复制；`inputs.json`/`manifest.json` 记录所用 Constitution 版本和 hash。
- `spec.md` 是上述 canonical JSON 的 review-first 渲染和必要叙述，不是 LangGraph 的唯一输入。若直接编辑 `spec.md` 改变设计事实，必须重新编译 JSON、重新 Analyze 并产生新 digest。
- Machine artifacts 仍由 Harness 自动生成；这不违反“只生成一个 spec.md 给用户”的决定。

- Canonical ownership 候选：
  - requirement statement/type/source/rationale：`requirements.json`。
  - verification tier/method/expected/pass condition/evidence requirement：`verification-plan.json`。
  - subsystem ownership/dependencies/interfaces：`subsystems.json`。
  - component adoption/rejection/version：`component-selection.json`。
  - hardware document/grounding/coverage：`datasheet-manifest.json`。
  - FreeRTOS tasks/communications/resource/timing design：`architecture.json`。
  - requirement-to-test traceability：`acceptance-matrix.json`，必须由上游文件生成，禁止人工独立编辑。
  - runtime nodes/transitions/gates：`execution-contract.json`，由所有设计产物编译生成。
  - runtime PASS/FAIL 和 raw evidence：不写回 approved design package；由 LangGraph execution state/evidence ledger 保存。

- `manifest.json` 候选职责：
  - package schema version、project、revision、lifecycle status、previous revision。
  - input hashes：requirements、connections、constitution。
  - generator/reader/schema versions。
  - payload 文件清单、角色、schema version、SHA-256。
  - canonical `design_digest`；计算范围排除 `approval.json` 以避免循环 hash。
  - canonical JSON 序列化规则，避免格式变化导致无意义 digest 变化。

- `approval.json` 候选规则：
  - 初始只能为 `PENDING`/不存在；设计 Agent 不得写入虚假 `APPROVED`。
  - approval gate 保存被审阅的 `spec.md` hash、完整 `design_digest`、revision、用户选择、Tier C 接受范围、时间和用户事件来源。
  - `unresolved_policy_items`、blocking unknowns、validation errors 必须为空才能 APPROVED。
  - credentials 只保存 secret reference/env name，不保存 secret value。
  - 任何 canonical payload 变化都会产生新 digest，使旧 approval 自动失效。

- `requirements.json` 候选字段原则：
  - `id`、`type` (`USER_STATED`/`REQUIRED_DERIVED`)、statement、source reference、rationale、criticality、owner subsystem、status、verification IDs。
  - 不重复保存完整 expected/tier；这些由 `verification-plan.json` canonical 持有。
  - `REQUIRED_DERIVED` 必须记录从哪些 requirement/datasheet/architecture constraint 推导而来。

- `subsystems.json` 候选字段原则：
  - id/classification (`MCU_NATIVE`/`EXTERNAL_LIBRARY`/`CUSTOM_DRIVER`)、typed dependencies 及原因、owned requirement IDs。
  - semantic interface inputs/outputs/lifecycle/error contract。
  - selection ID、datasheet part IDs、timing/resource constraints、allowed project paths。
  - DAG 必须无环；依赖不能只是字符串数组而缺少 dependency type/reason。

- `verification-plan.json` 候选字段原则：
  - stable test ID、requirement IDs、owner、phase (`SUBSYSTEM`/`INTEGRATION`/`RELEASE`)、tier、executor/method。
  - setup、stimulus/injection、observation、typed expected values with units/tolerance、sample/duration requirements。
  - deterministic pass expression、required evidence types、cleanup、side-effect boundary、timeout。
  - Tier A/B 的 `user_involvement` 必须为 none；不能只用自然语言 `looks plausible`。

- `datasheet-manifest.json` 候选字段原则：
  - exact part/variant、document source/ID/revision/hash、identity evidence、extraction status。
  - L0/L1/L2/L3 coverage、notes path/hash、reader provider/version。
  - verified/unknown/deferred facts、deep-read trigger、blocking impact。

- `component-selection.json` 候选字段原则：
  - exact/capability Registry queries、candidate identifiers/versions/details evidence。
  - local IDF/official/third-party/custom alternatives。
  - compatibility、requirements coverage、license/maintenance/dependency/limitation review。
  - decision、rejection rationale、version constraint、fallback/deep-read condition。

- `architecture.json` 候选字段原则：
  - components、FreeRTOS tasks、ownership、rates/deadlines、stack/resource budgets。
  - queues/notifications/event groups/mutexes、message schema、depth/backpressure/overflow。
  - startup/shutdown/restart/error recovery、persistent commit ordering、failure isolation。

- `tier-c-plan.json` 候选字段原则：
  - 只包含无法通过现有硬件和协议自动观察的最终物理现象。
  - requirement/test IDs、为何 A/B 不可行、已尝试自动化方法、必须先 PASS 的自动证据。
  - 单次批处理中的用户动作、预期可观察现象、结构化响应和结果映射。

- `acceptance-matrix.json` 候选规则：
  - 设计阶段只表示 `PLANNED`/trace completeness，不得提前出现 runtime `PASS`。
  - 自动生成 requirement → owner → tests → evidence contract → tier 的映射。
  - runtime closure 使用设计矩阵加实际 evidence 计算 verdict，结果写入 execution evidence/closure ledger。

- `execution-contract.json` 候选节点契约：
  - node ID/type/subsystem、preconditions、input artifact hashes、allowed reads/writes。
  - action kind (`DETERMINISTIC`/`AGENTIC`/`HUMAN_GATE`) 和所需 capability/provider。
  - output schema、evidence requirements、success expression、failure classification。
  - retry/change-detection policy、timeout、idempotency/recovery semantics。
  - legal transitions；不能由 Agent 自由填写任意 next action。

- `design-validation.json` 至少检查：
  - schema 和跨文件引用完整；无重复/孤儿 ID。
  - 每个 required R/DR 有 owner 和可执行 verification。
  - subsystem DAG 无环，execution transitions 合法且可达 release。
  - 所有外部 part L0/L1 READY；所有 required component selection READY。
  - Tier A/B 无用户操作；Tier C 有不可自动化证明且已批处理。
  - unresolved POLICY、blocking unknown、credentials/protocol 缺口为零。
  - acceptance matrix 与 canonical 文件一致。
  - `spec.md` 渲染内容与 canonical facts/digest 一致。

- 推荐 Design Subgraph 阶段候选：
  1. Load Constitution and snapshot inputs。
  2. Specify R/DR draft and source traceability。
  3. Build hardware/subsystem inventory。
  4. Perform Datasheet L0/L1 grounding。
  5. Run Component Registry selection。
  6. Clarify ambiguity/assumption/POLICY and collect the single review batch。
  7. Plan subsystem DAG and semantic interfaces。
  8. Design FreeRTOS/resource/failure architecture。
  9. Define Verification Plan and minimize Tier C。
  10. Compile machine task DAG/execution contract。
  11. Generate Checklist and run Analyze/design-validation。
  12. Render the single `spec.md` review document。
  13. Human Spec Approval writes `approval.json` and seals the revision。
  14. LangGraph continues directly into Firmware Execution Subgraph。

- 当前状态：`SUPERSEDED — 本节的逻辑领域、canonical ownership、approval/digest/revision 语义继续保留；十余个物理文件方案已由后续 5 文件 MVP 取代。`

### Spec Kit Design Package 精简候选

- 用户关注：完整候选包包含十余个 JSON，可能过度复杂；需要判断在保证项目质量的情况下哪些物理文件真正必要。
- 当前判断：上一节的 requirements/decisions/datasheet/selection/subsystems/architecture/verification/tier-C/traceability/workflow 等 **逻辑领域仍然必要**，但不必一开始就拆成同数量的物理文件。
- 推荐 MVP：5 个核心文件。

```text
design-package/<revision>/
├─ spec.md                    # 唯一用户文档，由 contract 渲染
├─ execution-contract.json   # 唯一机器设计事实源，包含所有设计领域 section
├─ manifest.json             # inputs/constitution/provider/file hashes、revision、design digest
├─ design-validation.json    # Schema/完整性/可执行性/安全检查报告
└─ approval.json             # 用户 approval event 对 design digest 的签认
```

- `execution-contract.json` 内部 section 候选：

```text
metadata
requirements
decisions
hardware_and_datasheet_grounding
component_selections
subsystems
architecture
verification
tier_c
traceability
workflow
```

- 原候选文件的精简映射：
  - `requirements.json` → `execution-contract.json.requirements`
  - `decisions.json` → `.decisions`
  - `datasheet-manifest.json` → `.hardware_and_datasheet_grounding`
  - `component-selection.json` → `.component_selections`
  - `subsystems.json` → `.subsystems`
  - `architecture.json` → `.architecture`
  - `verification-plan.json` → `.verification`
  - `tier-c-plan.json` → `.tier_c`
  - `acceptance-matrix.json` → 自动生成的 `.traceability`
  - task DAG/原 `execution-contract.json` → `.workflow`
  - `inputs.json` → `manifest.json.inputs`
- `datasheet_notes.md`、PDF、Registry 查询记录和原始资料仍保存在组件/证据目录，由 Contract 记录 path/hash/coverage，不复制进 Design Package。
- Runtime build/flash/serial/evidence/closure 不写回 approved Design Package；继续保存在 execution state、logs 和 evidence ledger。

- 保证质量的真正硬门禁：
  1. `execution-contract.json` 是唯一 canonical machine source；`spec.md` 是渲染视图。
  2. Contract 有严格 JSON Schema、stable IDs、单位和枚举，禁止自由文本替代关键 gate。
  3. 确定性 validator 检查 requirement owner/test、DAG、Tier、grounding、selection、workflow reachability 和 blocking unknown。
  4. `manifest.json` hash inputs、Contract、spec 和工具/provider 版本并生成 design digest。
  5. `approval.json` 只能由用户 gate 产生并绑定 digest；payload 变化自动失效。
  6. Approved revision 不可原地修改；变化产生新 revision。
  7. LangGraph 只执行已验证且已批准的 Contract；runtime verdict 由证据计算。
- 拆分策略：先保持单 Contract；只有单个 section 过大、需要独立版本/生成器、出现多人并行编辑或加载性能问题时，才将该 section 拆成独立 JSON。Manifest/Contract interface 保持兼容，使拆分不影响 LangGraph。
- 当前决定：`DECIDED — 采用 5 文件 MVP；上一节的多文件方案仅保留为逻辑领域模型和未来按需拆分参考，当前不按十余个物理文件实现。`

```text
design-package/<revision>/
├─ spec.md
├─ execution-contract.json
├─ manifest.json
├─ design-validation.json
└─ approval.json
```

- 实施约束：在后续重构获得授权前，本决定只记录设计，不立即创建 Schema、生成器或 LangGraph 节点。

### LangGraph Firmware Execution Harness 评估与 MVP 规划

- 用户提供了一份完整建议，包含 4 个业务子图、约 40 个细粒度节点、nodes/gates/adapters/repositories/schemas 多层目录、LangGraph checkpoint SQLite、独立 runs SQLite 及十余张业务表。
- 当前评价：建议中的工程原则大部分正确，但物理实现接近生产级工作流平台；第一版原样实现会过度复杂，增加事务一致性、迁移、恢复和测试成本。
- 应保留的核心原则：
  - Spec/Design Package approval 后按 digest 冻结。
  - LangGraph checkpoint 只保存控制状态和 artifact/evidence 引用，不保存日志/PDF/secret/进程/串口对象。
  - 节点返回结构化结果，路由由代码和 Gate 确认，不直接相信模型建议。
  - `run-state.json` 改为只读投影，不再由 Agent 自由修改。
  - 每个 subsystem 有独立 attempt；expected evidence 必须在测试前定义。
  - Flash/serial 等副作用必须有 lock、receipt、幂等/恢复语义。
  - Tier C 支持 confirmed/failed/unable/not-performed，失败返回责任子系统。
  - Integration 按测试计划拆分证据，Closure 逐 R/DR 重新判定，Release 使用独立 selftest-off build/flash/runtime 证据。
  - Preflight PASS 不代表硬件永久可靠；后续仍区分 firmware/toolchain/transient hardware/persistent hardware/user input/external service。

- 需要修正/精简的点：
  - 子图 A 必须直接复用已决定的 Spec Kit Design Subgraph 和 5 文件 Design Package，不能再实现第二套 spec/manifest/approval 结构。
  - 运行身份应绑定 `design_revision + design_digest + approval_id`，不能只绑定 `spec_sha256`。
  - `hardware_fingerprint.port` 是 session 属性，不是稳定硬件身份；稳定身份优先使用 chip/efuse MAC/可读取 board ID，无法自动获得的 board revision 标为 UNKNOWN。
  - 不为 build/judge、flash/judge、lock/release 等每个内部函数都建立独立图节点；节点应对应值得 checkpoint/retry 的事务边界。
  - Integration 的多个测试是 data-driven test cases，可由一个循环节点/子图执行并分别出 Evidence，不需要九个硬编码图节点。
  - 全局 `retry_budget_remaining=3` 过于粗糙，可能造成内部工程失败提前 BLOCKED；应按 failure category/node/attempt 使用策略，并通过 progress fingerprint 检测停滞。
  - 第一版不实现独立 `runs.sqlite` 和十余张业务表；使用 checkpoint SQLite + 文件化、带 hash 的 receipts/evidence。需要跨项目查询或高并发后再增加业务 DB/index。
  - 第一版不拆独立 gates/repositories 包；先用 `validators.py`、`storage.py` 和少量 adapters，复杂度出现后再拆。

- 推荐 MVP 运行存储：

```text
runtime/
└─ checkpoints.sqlite                 # 仅由 LangGraph checkpointer 管理

projects/<project>/
├─ design-package/<revision>/         # 已批准 5 文件设计包
├─ execution/
│  ├─ run-state.json                  # 从 checkpoint + receipts 生成的只读投影
│  ├─ run.json                        # run/design/hardware session 身份
│  ├─ events.jsonl                    # append-only transition/failure/repair/user/revision history
│  ├─ receipts/                       # build/flash/serial/gate/release JSON receipts
│  └─ evidence/                       # measurement/requirement evidence JSON
├─ logs/                              # raw build/flash/serial/protocol output
├─ components/
└─ main/
```

- Evidence 有效性先采用内容绑定而不是主动维护复杂失效数据库：
  - 每条 Evidence 绑定 `design_digest`、test ID、firmware/ELF SHA-256、hardware session/chip ID、artifact path/hash。
  - Closure 只接受与当前 approved design、当前 firmware、当前 hardware session 和存在的 artifact/hash 一致的证据。
  - 代码/配置变化产生新 firmware hash，旧证据自然变为 stale，不必第一版实现 `invalidated_at` 级联数据库。
  - 后续若需要解释主动失效原因，再增加 evidence index/runs DB。

- 推荐 MVP 目录：

```text
orchestrator/
├─ cli.py
├─ graph.py
├─ state.py
├─ models.py
├─ policies.py
├─ validators.py
├─ storage.py
├─ subgraphs/
│  ├─ design.py
│  ├─ subsystem.py
│  ├─ integration.py
│  └─ release.py
└─ adapters/
   ├─ idf.py
   ├─ serial.py
   ├─ hardware.py
   ├─ registry.py
   ├─ datasheet.py
   └─ agent.py
```

- 推荐顶层图（约 10 个语义节点/子图）：

```text
initialize_run
→ environment_and_hardware_preflight
→ design_subgraph
→ spec_approval_interrupt
→ bind_approved_design
→ bind_or_refresh_hardware_session
→ subsystem_bringup_subgraph (loop)
→ tier_c_interrupt (conditional)
→ integration_subgraph
→ requirements_closure
→ release_subgraph
→ finalize_run
```

- 推荐 Subsystem Bring-up 子图（约 7 个检查点节点）：

```text
prepare_attempt
→ implement_or_patch
→ validate_source_and_contract
→ build_attempt
→ flash_and_serial_attempt
→ evidence_gate
   ├─ PASS/PENDING_TIER_C → commit_attempt
   ├─ REPAIR → diagnose_and_repair → validate_source_and_contract
   └─ HARD_BLOCKER → blocked
```

- `flash_and_serial_attempt` 内部由 adapter/context manager 完成：停止旧 monitor、获取 lock、核对 hardware session、flash、生成 flash receipt、启动 serial、执行 test、保存 raw log、释放 monitor/lock；图节点返回 receipt/evidence 引用。若需要单独重试 flash/serial，再在后续版本拆节点。
- 推荐统一边界模型：Pydantic/严格 schema 的 `NodeResult`、`Receipt`、`Evidence`、`GateResult`、`Failure`；LangGraph State 只保存 IDs、phase、current attempt、approved design、hardware session 和 last result。
- 路由规则：每个节点选择一种路由机制；静态/conditional edge 与 `Command(goto=...)` 不混用，避免双路同时执行。
- Human interrupt 节点：interrupt 之前只做可重复读取或幂等 upsert；approval/Tier C 的副作用和 ledger commit 放在恢复结果验证之后的独立节点。

- 分阶段工程计划候选：
  1. **Contracts first**：5 文件 Design Package schema、NodeResult/Receipt/Evidence/GateResult、failure taxonomy、hash/idempotency 规则和 validators。
  2. **Walking skeleton**：CLI、SQLite checkpointer、thread/run identity、只读 run-state 投影、fake adapters；跑通 approval interrupt/resume 和 COMPLETE/BLOCKED。
  3. **One real subsystem vertical slice**：真实 IDF wrapper、hardware probe、serial adapter、build/flash/serial receipts、一个 subsystem attempt/repair loop。
  4. **General subsystem loop**：dependency order、Registry/custom-driver capability、attempt history、stale evidence rejection、Tier C pending。
  5. **Integration/Closure/Release**：data-driven integration tests、R/DR closure、独立 release manifest/selftest-off evidence。
  6. **Hardening**：crash/resume、port loss、stale receipt、partial side effect、interrupt replay、loop stagnation、migration/schema compatibility tests。
  7. **Scale only when needed**：业务 `runs.sqlite`、repositories/gates 分包、跨 run 查询、Postgres/async/multi-worker。

- 粗略工程量判断：
  - 原附件完整版：中大型工程；对单人开发而言通常需要数周到数月，并且硬件故障注入和恢复测试会占很大比例。
  - 推荐 MVP：中等工程；约 12–16 个 checkpoint-worthy 节点、1 个 checkpoint DB、文件化 receipts，可按 5–7 个增量里程碑交付。
  - 不建议一次完成全部目录、表和节点后才第一次跑真实硬件；必须先做一条从 approval 到 release 的窄垂直路径。
- 当前状态：`DECIDED — 按上述 LangGraph MVP 实施，不直接照搬附件完整版；采用 4 个子图、约 12–16 个 checkpoint-worthy 节点、单一 checkpoint DB、文件化 receipts/evidence，并从一条真实 subsystem 垂直路径逐步扩展。`

### LangGraph MVP 的连续执行保证、异常边界与串口识别

- 用户核心问题：MVP 是否能按 subsystem 顺序一口气执行完、不在内部 gate 停止，并能否在一个 Codex 对话中完成；是否能严格遵循现有 Skill 流程但不过度复杂；是否必须第一版处理大量未实际遇到的异常。
- 连续执行目标澄清：
  - LangGraph 可以保证同一 `run_id/thread_id` 在没有 Human Gate 或硬阻塞时持续选择合法下一节点，不把 build/flash/serial/repair/subsystem PASS 当作停止点。
  - 它可以按 dependency DAG 自动完成一个 subsystem 后进入下一个，并通过 checkpoint 在进程/模型回合中断后恢复。
  - 可靠保证对象应是“同一 firmware run 最终完成”，而不是“必须在一个模型回合内完成”。
  - 可以使用同一个用户对话启动、查看和恢复同一 run；但长时间 build/flash/repair 可能跨多个 Codex tool call、回合、上下文压缩甚至新会话。只要 runner/checkpoint/run ID 持久化，流程不应依赖对话上下文。
  - 若要求 Codex UI 关闭后仍无人值守继续，需要独立 orchestrator runner/service；单纯在一次对话里调用 LangGraph 不自动保证后台进程永久存活。
- Skill 顺序执行原则：
  - 不让 LangGraph 每次重新阅读长 Skill 后自由解释顺序。
  - 将 Skill 中稳定的阶段、gate、failure policy 编译/实现为 Graph + `execution-contract.workflow`；Skill 只保留触发方式、工程原则和 Agentic node 指令。
  - 通过 contract/graph tests 检查阶段顺序、合法 transitions、不可绕过 gate 和 Release 可达性，防止 Skill 与 Graph 漂移。
  - Subsystem 和 Integration 的数量/测试项保持 data-driven，避免为每个器件或异常增加固定图节点。

- MVP 异常策略：不穷举所有可能异常，只实现四类统一 failure policy。
  1. **INTERNAL_REPAIRABLE**：compile/link/config/driver/state-machine/test failure；自动 diagnose → minimal patch → reverify。
  2. **TRANSIENT_RESOURCE**：port busy、monitor 未释放、短暂 USB/serial 消失、临时外部服务失败；cleanup/re-probe/bounded retry。
  3. **CONSISTENCY_ERROR**：artifact missing/hash mismatch、receipt 未提交、design/firmware/evidence digest 不匹配；拒绝旧结果并从最近安全事务边界重做。
  4. **HARD_EXTERNAL_BLOCKER**：wrapper/环境不可用、目标硬件持续不存在、必需 secret/artifact 无法获得、外部服务持续不可用且无 fallback；保存证据后 BLOCKED。
- 第一版最低必须覆盖的异常：
  - monitor/进程占用串口；
  - 串口消失或号码改变后的 re-probe；
  - Flash 成功但节点在 receipt commit 前中断；
  - build/flash/serial artifact 缺失或 hash 不一致；
  - expected marker 超时；
  - 相同 failure fingerprint 连续无进展。
- 其他罕见异常不预先建立专用节点：统一归类为上述四类，先安全失败并保存 context；在真实运行中出现后再增加定向 recovery policy。

- 当前 `esp-serial` 自动选口核查：
  - `serial_mcp.py::_default_port()` 仅执行 `serial.tools.list_ports.comports()` 并返回 `ports[0]`。
  - `hwtest/preflight.py` 在多个端口时同样打印提示后选择第一个。
  - 因此当前能力是“自动选择一个枚举端口”，不是“识别并确认同一块 ESP32”；存在多个串口设备时可能选择错误目标。
- 推荐 MVP Hardware Identity：
  - `hardware_identity`：expected target chip、可读取的 chip ID/eFuse MAC、可用时的 USB VID/PID/serial/location；不强制无法自动读取的 board revision。
  - `hardware_session`：当前 port、baud、connected/probed time、probe result。
  - port 是可变 locator，不进入长期稳定 identity。
  - 自动选口策略：显式 approved port → stable USB identity 匹配 → 枚举候选并 probe expected chip/chip ID → 仅一个有效候选时采用；多个有效/无法判定时才进入外部 blocker，不能盲取第一个。
  - 设备从 COM7 变为 COM9 时，只要 stable identity/chip ID 一致，就更新 hardware session 并继续，不要求用户确认。
- MVP 进一步优化建议：
  - Happy path 与最小恢复路径同时实现，不为从未出现的异常提前建立大量节点/数据库表。
  - 把 port rediscovery、lock cleanup、artifact verification、receipt recovery 实现在 adapters/validators/policies 内，不膨胀 Graph。
  - 用内容 hash 使重复 build/flash/verify 可判断和可安全重做；用 operation/attempt ID 防止重复 ledger commit。
  - Top-level Graph 保持 10 个左右语义阶段，Subsystem 子图保持约 7 个 checkpoint 节点。
- 当前状态：`DECIDED — 接受以上连续执行保证边界、四类统一 failure policy、第一版最低异常集合和 Hardware Identity/Session 区分；不为罕见异常预建大量专用节点。`

### Skill、AGENTS.md、Docs 与 LangGraph 的职责拆分

- 用户关注：当前 firmware Skill 内容已经较完整，但随着执行顺序改由 LangGraph 代码化控制，是否应把长 Skill 拆成更简洁的入口，并把架构、验证、安全和局部规则分别放入根 `AGENTS.md`、`docs/` 和嵌套 `AGENTS.md`。
- 用户提出的方向：
  - 根 `AGENTS.md` 负责项目定位、最高优先级禁令、修改后必跑检查、资料路由和职责边界。
  - `docs/` 负责系统架构、固件工作流、硬件安全、验证、证据模型和 LangGraph 编排等详细设计。
  - `orchestrator/AGENTS.md`、`components/AGENTS.md`、`tools/AGENTS.md` 负责对应目录的局部规则。
  - `SKILL.md` 不再单独承担全部执行顺序。
- 已核对的 Codex `AGENTS.md` 发现语义：
  - Codex 在一次 run 开始时建立 instruction chain，先加载全局作用域，再从项目根目录走到**启动时当前工作目录**。
  - 每个目录最多选取一个指令文件，优先级为 `AGENTS.override.md`、`AGENTS.md`、配置的 fallback 文件名。
  - 越接近当前工作目录的文件越晚加载，因而可覆盖上层规则。
  - 发现过程停在当前工作目录；之后仅仅编辑某个更深目录内的文件，不会因此自动加载那个目录的嵌套 `AGENTS.md`。
  - 项目指令链存在默认 32 KiB 的组合上限，因此根文件不能无限扩张，也不能假设所有嵌套规则总会进入上下文。
- 对用户草案的关键修正：
  - 方向正确，但嵌套 `AGENTS.md` 不能成为唯一的关键规则承载者；根 `AGENTS.md` 必须保留清晰的资料路由和不可绕过的全局不变量。
  - 当前仓库并没有根级 `components/`；真实组件位于 `projects/<project>/components/`。如果创建根 `components/AGENTS.md`，它不会约束现有项目组件。候选位置应是 `projects/AGENTS.md`，必要时再为生成的项目或组件目录设置更局部规则。
  - 即便存在 `orchestrator/AGENTS.md` 和 `tools/AGENTS.md`，从仓库根启动 Codex 时它们也不会自动进入 instruction chain。根 `AGENTS.md` 必须明确要求：修改对应领域前主动读取其局部规则；或者运行入口显式以相应目录作为 CWD。不能只依赖隐式发现。
  - `docs/` 默认也不会自动加载；必须由根路由、Skill、Graph node 或开发命令按任务选择性读取。
- 建议的唯一权威归属，避免同一规则在四处复制后发生漂移：

| 内容 | 唯一权威来源 | 其他位置如何处理 |
|---|---|---|
| 阶段顺序、合法 transition、gate、retry/failure policy | LangGraph 代码 + `execution-contract.workflow` + contract/graph tests | Skill 和 docs 只概述与链接，不复制完整顺序表 |
| Repo 级禁止事项、固定命令、用户交互边界、最低 Done Criteria | 根 `AGENTS.md` | Skill 可引用，但不能给出冲突版本 |
| 特定目录的编码与修改规则 | 对应嵌套 `AGENTS.md` | 根文件负责路由，并保留真正全局的安全规则 |
| 架构原因、设计解释、状态/证据模型、故障策略说明 | `docs/` | 不作为运行时状态或 gate 判定来源 |
| 如何触发 firmware Harness、输入输出、允许的人工 gate、调用失败如何处置 | firmware `SKILL.md` | Skill 调用 runner，不再由模型逐条解释 440 行流程 |
| 运行时状态、attempt、receipt、evidence、verdict | checkpointer/state store + 项目 evidence artifacts | AGENTS/Skill/docs 不保存动态真相 |
| 硬件安全约束 | adapter/validator/test 机械强制；根 `AGENTS.md` 保留不可绕过摘要 | `docs/HARDWARE-SAFETY.md` 解释原因和恢复流程 |

- 建议的目标结构候选：

```text
repository/
├─ AGENTS.md
├─ .agents/skills/esp-idf-firmware/
│  ├─ SKILL.md
│  └─ references/                  # 仅保留调用所需的契约/故障参考，不复制实现文档
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ DESIGN-HARNESS.md            # Spec Kit / Design Subgraph
│  ├─ EXECUTION-HARNESS.md         # LangGraph 节点、状态和恢复模型
│  ├─ VERIFICATION-EVIDENCE.md     # Verification 与 Evidence Model 合并，避免重复
│  └─ HARDWARE-SAFETY.md
├─ orchestrator/
│  ├─ AGENTS.md
│  └─ ...
├─ tools/
│  ├─ AGENTS.md
│  └─ idf.ps1
├─ projects/
│  ├─ AGENTS.md                    # 约束生成项目、main/components/selftest/logs
│  └─ <project>/
│     └─ components/
├─ requirements/
└─ connections/
```

- Docs 数量控制建议：不必一开始机械创建 6 个长文档。`FIRMWARE-WORKFLOW.md` 与 `LANGGRAPH-ORCHESTRATION.md` 很容易重复，`VERIFICATION.md` 与 `EVIDENCE-MODEL.md` 也高度相关；MVP 先采用上面的 5 个职责明确文档，后续只有在单文件出现独立维护者、独立生命周期或明显过长时再拆分。
- 未来精简后的 firmware Skill 候选内容：
  1. frontmatter：触发条件与负触发条件；
  2. 输入发现：project、requirements、connections、approved design revision；
  3. 唯一标准调用方式：启动/恢复/查询 Harness 的具体命令；
  4. 用户交互合同：Stage 1.5、预声明 Tier C、真正 hard blocker；
  5. capability 路由摘要：Registry、local IDF、Datasheet reader/custom driver；
  6. 产物位置与完成响应；
  7. runner 无法使用时的 failure signals 和受限 fallback。
- 不建议把现有完整 Skill 立即删减：在 Graph、execution contract 和 tests 尚未实现前，当前 Skill 仍是唯一完整流程基线。过早拆分会造成尚未迁移的 gate/细节丢失。
- 推荐迁移顺序：
  1. 冻结现有 Skill 作为 migration baseline，不继续同时改写多份规范。
  2. 为 Skill 中每条规范建立 parity inventory，标记其未来唯一归属：Graph/contract、root AGENTS、nested AGENTS、docs、validator/test 或保留在 Skill。
  3. 实现 LangGraph walking skeleton、execution contract、状态/receipt/evidence schema 和顺序/gate tests。
  4. 建立根资料路由、局部 AGENTS 与最小 docs；由测试确认关键规则已有机械执行或显式加载路径。
  5. 跑真实 ESP32 端到端迁移试验，确认从 approval 到 release 没有依赖旧 Skill 中未迁移的隐含步骤。
  6. 最后把 Skill 收缩为 Harness 使用接口，并将旧完整版归档为非权威迁移记录；禁止长期保留两套都声称权威的执行顺序。
- 关于行数：不设置“必须少于 250 行”的项目硬门槛。精简的理由应是执行顺序已经迁入可测试代码、Skill 只保留调用合同，而不是为了达到某个数字。若精简后仍需超过 250 行，只要每段都是调用 Harness 必需且没有重复，就可以保留。
- 当前状态：`DECIDED — 用户确认采用该分层方式；按 parity inventory → Graph/Contract/tests → AGENTS/docs → 真实硬件迁移验证 → 最后精简 Skill 的顺序实施。`

### 完整 Firmware Skill 零丢失迁移审计

- 审计目的：新的 Spec Kit/LangGraph Harness 是为了把完整 Skill 的要求变成可执行、可恢复、可测试的工程合同，不是为了用“架构升级”删除旧流程中的工程约束。
- 审计基线：
  - 当前 `.agents/skills/esp-idf-firmware/SKILL.md`：422 个物理行，是本次迁移的主要规范基线。
  - 旧 `.agents/skills/esp-idf-firmware/SKILL_claude.md`：369 个物理行，用于检查历史能力是否曾在当前 Skill 中被明确修正、替代或遗漏。
  - Supporting contracts：`references/run-state.md`、`references/devlog-format.md`、`references/datasheet-notes-format.md`、`references/board-profile-schema.md`。
  - `crumb-agent-framework.md` 作为历史设计来源，不作为未来运行时权威。
- 零丢失的定义：
  - 必须保留每条规范的**工程语义、门禁和可验收结果**；不要求保留原来的 Markdown 段落、文件名或由模型逐条解释的执行机制。
  - 若某条规则被更安全或更严格的机制替代，必须在迁移矩阵中明确写出 replacement，不得静默消失。
  - 每条 normative rule 必须有一个且只有一个未来权威来源，并至少由 schema、validator、graph test、adapter test 或真实硬件迁移测试中的一种方式保护。

#### 逐项迁移矩阵

| ID | 当前完整 Skill 要点 | 未来唯一权威/执行位置 | 迁移判定 |
|---|---|---|---|
| F01 | firmware Skill 的正/负触发范围：ESP-IDF、ESP32、driver/build/flash/serial；排除 Arduino、非 ESP、修改 `serial_mcp.py` 和一般文档任务 | 精简后的 `SKILL.md` frontmatter + Skill activation tests | 已覆盖；必须保留负触发条件 |
| F02 | `requirements/<project>.md`、`connections/<project>.md` 是必需且用户所有，除非明确要求不得重写 | 根 `AGENTS.md` + Design Subgraph input validator + manifest input hashes | 已覆盖；增加只读 ownership test |
| F03 | `activate.local.ps1` 必须可用；禁止猜 IDF 路径；禁止 bare `idf.py`；首次 build 前执行 `tools/idf.ps1 --version`；不硬编码 v6.0 | 根 `AGENTS.md` + `orchestrator/adapters/idf.py` + adapter tests | 已覆盖；具体命令从长 Skill 移入 adapter，不丢失语义 |
| F04 | `esp-component-registry` 和 Windows `esp-serial` 必须在 Stage 0 验证可调用；`esp-docs` 可选并可回退本地资料 | Preflight capability probe + capability receipts + root routing | 部分已覆盖；本轮明确加入 capability-level preflight receipt |
| F05 | 硬件 Preflight 必须在连续 firmware work 前 PASS，并记录 target、IDF、port、baud；port 可变但硬件身份需稳定 | `hardware.py` + Stage 0 preflight node + approved-design 后 session re-probe | **此前有顺序冲突，本轮补齐为两段式 Preflight** |
| F06 | `0 → 1.0 → 1.1 → 1.2 → 1.3 → 1.5 → subsystem loop → 2.9 → integration → closure → release` 的必经 Gate | Graph topology + `execution-contract.workflow` + reachability/no-bypass tests | 已覆盖；Graph 名称可变化，但 Gate 不可消失 |
| F07 | Datasheet 获取必须验证精确型号/变体、文档 ID/revision、有效技术内容和可用提取；禁止只看 PDF magic bytes；实现事实不得猜测 | Design Subgraph L0/L1 + datasheet provider contract；custom driver 前 L2/L3 | 已覆盖并增强为 progressive grounding |
| F08 | `spec.md` 包含硬件/pin、状态机、子系统/依赖、selection、verification、R/DR、限制和单批 Review；区分 USER/DERIVED/ASSUMPTION/POLICY | `execution-contract.json` canonical sections + review-first `spec.md` renderer | 已覆盖；`spec.md` 是视图，不是唯一机器源 |
| F09 | Tier A/B 自动验证；Tier C 只用于无法机器观测的最终物理现象；按钮等简单输入不得要求用户现场操作 | Contract verification schema + design validators + evidence gate | 已覆盖；validator 禁止 A/B 的 `user_involvement` |
| F10 | 在编码前建立完整 inventory、分类和 dependency order；逐 subsystem、一次一个地实现验证 | Contract `.subsystems` typed DAG + subsystem loop router | 已覆盖；必须有 DAG cycle/owner validation |
| F11 | Registry search 是 selection gate；精确型号无结果后必须按 capability/interface 搜；区分 valid zero-result 与 MCP outage；保存候选详情和 adopt/reject 理由 | Design Subgraph component-selection service + raw Registry receipt + Contract `.component_selections` | 已覆盖，但 Registry 适用范围需按 F12 的严格规则固定 |
| F12 | 当前 Skill H10/Stage 1.0 的严格范围：每个 external part **或 reusable software subsystem** 均要搜索 Registry；即使最后采用 local IDF built-in，也要保留 search/reject 证据 | inventory 字段 `registry_search_required` + selection validator | **此前 TODO 只明确“所有外部器件”，本轮补齐为当前 Skill 的严格范围；除非用户以后显式放宽** |
| F13 | 采用 Registry 库时使用精确 namespace/version constraint；检查 target/IDF/API/example/Kconfig/dependency/license/维护/限制；不修改 `managed_components/`；仍须 wrapper、真实硬件和 semantic API | Contract selection record + dependency adapter + `projects/AGENTS.md` + source validator | 已覆盖；增加 generated-dir write guard 和 version-lock receipt |
| F14 | MCU-native 路径优先本地 `$IDF_PATH/examples`、headers/source；`esp-docs` 只补概念；任何签名以已激活本地版本为准 | implementation node policy + local-source evidence receipt + source validator | 已覆盖；不能仅保存在 docs 中 |
| F15 | 修改 subsystem 前读取 previous DEVLOG failure、raw logs、datasheet gotcha、acceptance rows 和本地 API signature，避免重复历史错误 | `prepare_attempt` 输入收集 + structured event/attempt history + agent node prompt contract | **此前仅散见 TODO，本轮要求成为节点前置条件** |
| F16 | `main/` 只做产品编排/状态机；底层 IDF、寄存器、bus 和外设细节在项目-owned component；采用库也必须包 semantic API | `projects/AGENTS.md` + `docs/ARCHITECTURE.md` + source boundary lint/test | 已覆盖；实际组件目录是 `projects/<project>/components/` |
| F17 | 每个 component 保留可重用 selftest，覆盖 init、identity/readback、normal/boundary/failure；不是只打印 initialized/raw sample | Contract tests + component selftest interface convention + evidence validator | 部分已覆盖；本轮明确 selftest API/marker 是项目代码规范必选项 |
| F18 | 运行测试前必须先声明 marker/value/range/count/duration/rate/tolerance/failure response；“看起来合理”禁止 PASS | `EvidenceContract`/test case schema + `prepare_attempt` validator | 已覆盖；expected 必须早于副作用 receipt |
| F19 | Build 首次/target 变化时 set-target；所有 build 通过 wrapper；保存 raw log；build-only 不构成 subsystem PASS | IDF adapter + build receipt + evidence gate | 已覆盖；精确日志路径由 attempt ID 生成，避免覆盖 |
| F20 | Flash 只在 clean build 后；先停止 serial；记录实际 port 和 raw log；port busy 自动 cleanup/retry | flash/serial adapter context manager + resource lock + receipt | 已覆盖；列入第一版最低异常集合 |
| F21 | Windows serial 使用 MCP；boot capture 与 persistent session 分开；bounded timeout/output；预期 marker；normal/timeout/error/cancel 全路径 `monitor_stop`；absence of marker=FAIL | serial adapter contract + mocked cleanup tests + real hardware vertical test | **此前只有概述，本轮锁定精确资源清理语义** |
| F22 | 失败先按层分类，做 smallest directed repair，然后完整 reverify；build/flash/serial/protocol/integration failure 都不是询问用户的理由 | `Failure` taxonomy + policies + `diagnose_and_repair` loop + progress fingerprint | 已覆盖；失败信号表进入 policies/tests，不复制进 Graph 节点 |
| F23 | 每次 subsystem verdict 保存 selection、knowledge、implementation、config、expected/actual、R/DR、Tier、logs、failure/repair，并立即进入下一 dependency | attempt record + receipts/evidence + append-only execution events；`DEVLOG.md` 为人类投影 | **此前缺少完整事件历史载体，本轮新增最小 `events.jsonl`/等价 append-only 结构** |
| F24 | Selftest 源码永久保留；bring-up/integration 有效启用；release 有效禁用；release 必须 fresh build/flash/runtime verify，不得复用 selftest-on 镜像证据 | verification/release build profiles + release manifest/receipt + config assertion | 已覆盖；可用独立 config profile 取代手改默认值，但有效状态与证据不可改变 |
| F25 | Persistent-storage selftest 只删除 test-owned record；不得删 newest/oldest/arbitrary data；network test 使用声明 endpoint/fixture、隐藏 credentials 并区分 transport/TLS/auth/protocol/application | `projects/AGENTS.md` + `HARDWARE-SAFETY.md` + selftest contract + destructive-action tests | **此前 TODO 未明确展开，本轮补齐为强制安全规则** |
| F26 | Stage 2.9 无 Tier C 自动跳过；有则先完成所有 A/B，只展示一次 exact action/expected pattern/统一响应，之后不再交互 | Tier C plan schema + conditional interrupt + resume validator | 已覆盖 |
| F27 | FreeRTOS 架构必须在 integrated `main.c` 前设计：task ownership、blocking/real-time separation、queue/backpressure/overflow、notification/event group/mutex、禁止 bare shared mutable/把 volatile 当同步 | Contract `.architecture` + design validators + integration source/runtime tests | 已覆盖但阶段前移：在 Design Subgraph 冻结，Integration 执行并测量；重大改变返回 spec amendment |
| F28 | task priority/stack/queue depth/timeout 由 timing 和测量决定；测 stack high-water、heap/handle/socket/file/record、queue occupancy 和重复周期资源行为 | architecture contract + integration quantitative evidence | 已覆盖；必须为 machine-checkable typed value/unit，不得只写解释性 prose |
| F29 | Integration 必须验证 materially different real call patterns：并发、持续运行、reconnect/reboot、queue/storage/network/peripheral failure、throughput/latency/drop/jitter/recovery；Stage 2 one-shot 不可代替 | data-driven integration test plan + per-test Evidence | 已覆盖；保持测试 data-driven，不膨胀固定图节点 |
| F30 | Integration failure 先定位 owner；main 问题修 main，component 问题返回 Stage 2，interface/dependency change 重验消费者，protocol uncertainty 先 host reproduce；禁止为压症状乱改 component | failure ownership policy + affected-set calculator + graph routing tests | 部分已覆盖；本轮明确 direct-consumer/acceptance impact routing |
| F31 | Closure 逐个 R*/required DR* 重新计算；PARTIAL/FAIL/BLOCKED/mock/untested 或 conflicting limitation 禁止 release；检查重启/commit/power loss/storage/resources/sync/protocol/credential/license | deterministic closure validator + evidence bindings | 已覆盖；不能由 LLM 写 PASS |
| F32 | Release 保留诊断源码但 selftest-off；clean build、flash、fresh boot/runtime serial；normal operation 不依赖 selftest command/output；无 destructive test/secret leakage；所有 evidence 路径/hash 一致 | release subgraph + immutable release manifest + closure/release validator | 已覆盖；fresh release firmware hash 必须和 flash/serial evidence 相同 |
| F33 | Revision 支持 bug/new requirement/non-functional improvement；先建立量化 baseline，定位最小 owner，重验 affected consumers/failure/closure/release，保留历史，不必重跑无关 subsystem | `initialize_run(mode=revision)` + Design amendment/impact analysis + affected DAG + new release | **此前 MVP 顶层图遗漏，本轮新增 Revision/Change-Impact 路径** |
| F34 | Raw build/flash/serial/protocol 进入 `logs/`；摘要引用路径和关键值，不粘贴整墙日志；credentials 不进入 evidence | adapters/storage policy + root AGENTS + redaction tests | 已覆盖；logs 按 run/attempt/test 命名并 hash |
| F35 | 最终响应只有 COMPLETE/allowed interrupt/evidenced blocker；成功必须报告 release stage、build/flash、selftest-off、R/DR closure、runtime、关键证据 | 精简 Skill `Response` + CLI status/final renderer + terminal validator | 已覆盖；模型不得绕过 runner 自行宣布完成 |
| F36 | 当前 Stop hook 读取 `run-state.json` 并阻止 CONTINUOUS 提前 final | 继续保留 Stop hook 读取兼容；`run-state.json` 改由 orchestrator 原子生成 | 已覆盖且遵守“暂不重构 Stop hook 架构”；只改变写入所有权 |
| F37 | 当前 failure-signals 中的 API、link、Registry、dependency、serial、data-path、state-machine、watchdog、backpressure、sync、storage、protocol、performance、limitation、stall 等诊断方向 | `policies.py` + adapter/graph tests + docs troubleshooting | 已覆盖；作为可测试分类规则迁移，不为每一条建图节点 |

#### 审计发现的真实缺口与处理决定

1. **首次 Preflight 顺序冲突**
   - 当前 Skill 把 Environment/Hardware Preflight 放在全部设计之前。
   - 之前推荐顶层图却是 `design_subgraph → approval → hardware_preflight`，会让用户先批准一个可能无法在当前环境/硬件执行的设计。
   - 修正为两段式：
     - `environment_and_hardware_preflight` 在 Design Subgraph 前：验证 wrapper、IDF、MCP capability、至少一块匹配目标的 ESP、flash/serial 基础通路，并产生 preflight receipt。
     - `bind_or_refresh_hardware_session` 在 approval 后、首次副作用前：重新枚举、匹配 stable identity、更新可能改变的 COM port/baud/session。
   - 初次 PASS 不永久证明 USB/硬件可靠；后续 transient failure 仍按 failure taxonomy 恢复。

2. **Revision 路径遗漏**
   - 原 MVP 主要描述 greenfield 从 design 到 release，没有完整承载当前 Skill Stage 4 Revision。
   - `initialize_run` 必须区分 `new`/`resume`/`revision`；revision 先创建新 design revision 或 amendment、计算 affected set，再只重跑受影响 subsystem/integration tests，但最后仍须完成完整 closure 和新的 Stage 3.6 release。
   - timing/resource/non-functional 变化即使 API 未变也可能影响消费者，不能沿用旧版“interface unchanged = reintegration free”的宽松规则。

3. **工程历史缺少结构化载体**
   - checkpoint 只保存控制状态，receipts/evidence 保存结果，但当前 Skill/DEVLOG 还要求保存 selection、知识来源、失败分类、repair、过程观察和 revision history。
   - MVP 增加一个 append-only structured event stream（候选 `execution/events.jsonl`），或提供语义等价的 append-only attempt records。
   - `DEVLOG.md` 保留为由 structured events/receipts/evidence 渲染的人类视图，不再作为 Graph 判定权威；raw logs 仍独立保存。

4. **Registry 范围在 TODO 中发生收缩**
   - 当前完整 Skill 要求 external part 和 reusable software subsystem 都搜索 Registry；之前 TODO 的简写只强调“所有外部器件”。
   - 为零丢失，默认恢复当前严格范围，包括最终可能选择 Wi-Fi/NVS/HTTP 等 local built-in 的 reusable subsystem；搜索之后仍可明确 reject Registry 并采用本地 IDF。
   - 若未来认为这一步对显然的 IDF native 功能浪费，应单独讨论并显式修改 policy，不能在重构时静默删除。

5. **Selftest 的语义与实现机制需要分离**
   - 必须保留：源码和诊断 API 不删除；bring-up/integration 有效启用；release 有效关闭；release 重新 build/flash/runtime verify。
   - 不强制未来必须通过编辑 Kconfig 中的 `default y → default n` 实现；可以使用独立 verification/release sdkconfig profile。Release validator 检查最终有效配置和 firmware hash，而不是检查某一行文本。

6. **FreeRTOS 设计阶段前移但不能丢细节**
   - 旧 Skill 在 Integration 前设计 tasks；新架构把 task/queue/resource/failure architecture 放到 Spec Kit。
   - 前移是设计增强，不是删除：原 Skill 中 blocking、cadence、backpressure、ownership、sync、stack/heap 测量等每一项进入 Contract `.architecture`；Integration 负责用真实调用模式验证。

7. **串口和副作用清理规则必须机械化**
   - `monitor_stop` on success/timeout/error/cancel、flash 前释放 port、bounded wait/output、marker absence=FAIL 不能只留在 docs。
   - Serial/Flash adapter 必须用 context manager/finally 等机制执行资源释放，并通过 simulated exception tests 验证所有退出路径。

8. **旧输出文件的语义迁移需要明确**
   - `component-selection.md` 的 canonical 内容迁入 Contract `.component_selections`；原始 MCP 响应进入 design evidence/receipt，可选生成 Markdown 视图，但禁止形成第二权威。
   - `run-state.json` 从 Agent 写入的权威状态改成 orchestrator 的只读兼容投影。
   - `DEVLOG.md` 从人工维护的判定账本改成人类审计投影；structured events/receipts/evidence 才是机器事实。
   - `datasheet_notes.md`、PDF、源代码、raw logs 继续作为独立工件存在，Contract 记录 path/hash/coverage。

#### 旧 `SKILL_claude.md` 中被有意替代、不应原样恢复的内容

| 历史内容 | 处理 |
|---|---|
| 固定要求 wrapper 必须输出 ESP-IDF v6.0 | 已由当前 Skill 改为接受 `activate.local.ps1` 激活的本地版本；只验证可用性并以本地 headers/examples 为准 |
| Spec Approval 后才预取 Datasheet，并“拉取但先不阅读” | 已由 L0/L1 approval 前 grounding 替代；L2/L3 仍延迟按需执行 |
| 无 Datasheet 时留下 `#error` register stub | 不恢复；在 verified artifact 缺失时停在 Design/Implementation blocker，禁止生成猜测寄存器代码 |
| Build 必须“run in background” | 不作为规范；runner 可以同步、异步或后台执行，但必须可取消、保存日志、产生 receipt 并支持恢复 |
| Interface 未变则 reintegration free | 不恢复；timing/resource/dependency/behavior 变化即使 API 未变也可能要求重验 affected consumers |
| 每个回复首行 `STAGE:` 仅用于 `session_stats.py` 成本分段 | 降为可选 telemetry，不作为 workflow gate；状态以 runner/checkpoint 为准 |
| `DEVLOG-stats.md` 自动或固定生成 | 保持 on-demand telemetry；不进入 completion criteria |
| Preflight PASS 后所有失败都视为 firmware bug | 收紧为“先假设 firmware/tooling，但用 evidence 重新分类”；允许 USB/port/transient/persistent hardware failure |
| `type: rigid` Claude frontmatter | 不迁移到 Codex Skill；刚性由 Graph、schema、validators 和 tests 提供 |

#### 原始 `TODO.md` 的两项问题映射

| 原始问题 | 新架构处理 | 状态 |
|---|---|---|
| `CONTINUOUS` 时 Agent 提前结束，Stop hook 重复注入同一 continuation，仍停在相同 uploader cursor | LangGraph runner 持续推进；progress fingerprint/attempt history 检测无进展；checkpoint 决定下一动作；Hook 只读取投影并去重，不再靠重复 prompt 充当主循环 | 设计已覆盖；实现与回归测试待重构阶段完成 |
| 每个外部器件或可复用 subsystem 实现前强制 Registry MCP selection gate 和 adopt/reject evidence | F11/F12、Contract `.component_selections`、raw Registry receipt、selection validator 和 subsystem precondition | 设计已覆盖；必须纳入 parity/graph tests |

- 审计结论：`DECIDED — 当前完整 Skill 的所有 normative engineering requirements 均已获得明确未来承载位置；本轮发现并补齐 Preflight 顺序、Revision、structured engineering history、Registry 严格范围、serial cleanup、selftest 安全和阶段迁移等缺口。后续实现必须建立 machine-checkable parity checklist，任何 F01–F37 未通过均禁止删除/精简原 Skill。`

### 重构完成后的仓库结构

```text
repository/
├─ AGENTS.md                          # Repo-wide invariants、资料路由、固定命令、Done Criteria
├─ .agents/
│  └─ skills/
│     └─ esp-idf-firmware/
│        ├─ SKILL.md                  # Harness 触发/使用合同，不再复制完整状态机
│        └─ references/               # 仅保留 Skill 调用所需的接口/故障参考
├─ docs/
│  ├─ ARCHITECTURE.md                 # 系统边界、ownership、数据流
│  ├─ DESIGN-HARNESS.md               # Spec Kit、5-file Design Package、approval/revision
│  ├─ EXECUTION-HARNESS.md            # LangGraph、checkpoint、transition、resume
│  ├─ VERIFICATION-EVIDENCE.md        # Tier、Evidence/Receipt、closure/release
│  └─ HARDWARE-SAFETY.md              # flash/serial/secret/persistent-data 安全
├─ orchestrator/
│  ├─ AGENTS.md
│  ├─ cli.py                          # start/resume/status/pause/revision
│  ├─ graph.py                        # top-level topology
│  ├─ state.py                        # minimal checkpoint state
│  ├─ models.py                       # strict schemas
│  ├─ policies.py                     # failure/retry/progress/ownership policies
│  ├─ validators.py                   # design/evidence/gate/terminal validation
│  ├─ storage.py                      # atomic artifact/event/projection writes
│  ├─ subgraphs/
│  │  ├─ design.py
│  │  ├─ subsystem.py
│  │  ├─ integration.py
│  │  └─ release.py
│  └─ adapters/
│     ├─ idf.py
│     ├─ serial.py
│     ├─ hardware.py
│     ├─ registry.py
│     ├─ datasheet.py
│     └─ agent.py
├─ schemas/                           # Contract/Receipt/Evidence/Event 等版本化 schema
├─ runtime/
│  └─ checkpoints.sqlite             # LangGraph control checkpoints；不放 logs/PDF/secret
├─ tools/
│  ├─ AGENTS.md
│  ├─ idf.ps1
│  └─ ...
├─ hwtest/
├─ requirements/                      # 用户所有
├─ connections/                       # 用户所有
└─ projects/
   ├─ AGENTS.md                       # 所有生成项目的 main/component/selftest/log 规则
   └─ <project>/
      └─ ...
```

- `schemas/` 是否物理独立可在实现时按模块规模调整；即使 schema 与 `models.py` 同目录，其版本化/strict validation 职责不能删除。
- `runtime/checkpoints.sqlite` 是 Harness 自身控制数据，不应提交 secrets、raw serial、PDF 或项目源代码。
- 根 `AGENTS.md` 必须显式路由到 `orchestrator/AGENTS.md`、`tools/AGENTS.md`、`projects/AGENTS.md` 和相关 docs，因为从仓库根启动时嵌套 AGENTS 不会自动发现。

### 执行后单个 Firmware 项目的标准结构

```text
projects/<project>/
├─ CMakeLists.txt
├─ sdkconfig / sdkconfig.defaults
├─ design-package/
│  └─ rev-0001/
│     ├─ spec.md                      # 唯一用户审批文档
│     ├─ execution-contract.json      # canonical machine design
│     ├─ manifest.json                # input/provider/file hashes + design digest
│     ├─ design-validation.json       # schema/completeness/safety/reachability
│     └─ approval.json                # user event 对 design digest 的签认
├─ execution/
│  ├─ run.json                        # run/design/hardware identity binding
│  ├─ run-state.json                  # checkpoint/receipts 生成的 Hook-compatible 只读投影
│  ├─ events.jsonl                    # append-only transition/failure/repair/user/revision history
│  ├─ receipts/
│  │  ├─ preflight/
│  │  ├─ build/
│  │  ├─ flash/
│  │  ├─ serial/
│  │  └─ release/
│  └─ evidence/
│     ├─ subsystem/
│     ├─ integration/
│     ├─ tier-c/
│     └─ closure/
├─ components/
│  └─ <subsystem>/
│     ├─ CMakeLists.txt
│     ├─ idf_component.yml            # 仅在该 owning component 管理依赖时存在
│     ├─ include/<subsystem>.h         # semantic public API
│     ├─ <subsystem>.c                 # project-owned implementation/wrapper
│     ├─ selftest/                     # retained diagnostic/selftest source
│     ├─ datasheet_notes.md            # external part only；带来源/coverage/status
│     └─ <validated datasheet files>   # external part only；路径/hash 写入 Contract
├─ main/
│  ├─ CMakeLists.txt
│  └─ main.c                           # orchestration/product state machine only
├─ managed_components/                 # generated；只读，不手改
├─ logs/                               # raw build/flash/serial/protocol output，按 run/attempt 命名
└─ DEVLOG.md                           # 从 events/receipts/evidence 渲染的人类审计视图
```

- 不强制所有 component 只能有一个 `.c`；复杂 component 可在内部继续拆分，但 public semantic API、ownership 和 selftest contract 不变。
- `events.jsonl` 是候选文件名；若实现采用每-attempt immutable JSON 达到完全等价的 append-only 语义，也可替代，但不得只依赖自然语言 DEVLOG。
- Design Package revision 永不原地修改。Revision 生成 `rev-0002/`，旧 release/receipt/evidence 均保留，并通过 digest/hash 自动判为 current 或 stale。

### 重构后的完整端到端流程

#### 0. Skill activation 与 run 初始化

1. Codex 根据任务意图加载精简 firmware Skill。
2. Skill 解析 project，调用唯一 runner CLI；不自行按 Markdown 推进 Stage。
3. `initialize_run` 判断 `new`、`resume` 或 `revision`：
   - `new`：建立 run/checkpoint 和输入快照。
   - `resume`：验证 checkpoint、run identity、receipt/evidence hash 后从最近安全边界恢复。
   - `revision`：绑定上一 approved/release revision，进入 change-impact 路径。

#### 1. Stage 0：环境、能力与硬件 Preflight

1. 验证 `activate.local.ps1` 和 `tools/idf.ps1 --version`；记录真实 IDF version/target。
2. 验证 Registry MCP、Windows Serial MCP 可调用；记录可选 Docs provider/fallback。
3. 枚举硬件，probe expected chip/stable identity，执行基础 flash/serial preflight，记录 port/baud/session。
4. 失败按 environment/tool/MCP/hardware 层分类：可修复项自动处理；真正缺失 wrapper/硬件/必需服务才形成精确 blocker。

#### 2. Design Subgraph：从用户输入编译 5-file Design Package

1. 读取且 hash `requirements`、`connections`、Constitution/Repo policy；不重写用户输入。
2. 建立完整 inventory、classification、typed dependency DAG、requirement owner。
3. 对 external part 完成 Datasheet L0/L1；缺少图表/复杂事实标记 UNKNOWN/DEFERRED，不伪造。
4. 对所有 `registry_search_required` 项执行精确名称与 capability/interface 搜索，读取 credible candidate 详情并记录 adopt/reject。
5. 选择 MCU-native/local IDF、trusted library 或 custom driver；规划 L2/L3 trigger。
6. 设计行为状态机、semantic interfaces、FreeRTOS tasks/queues/ownership/resources/failure recovery。
7. 建立 verification plan、expected values/units/tolerance、Tier C、R/DR traceability 和 workflow DAG。
8. 生成 `execution-contract.json` 和 review-first `spec.md`，再生成 manifest/digest 和 deterministic validation report。
9. validation error、blocking unknown、未完成 selection 或不可达 workflow 都禁止进入 Approval。

#### 3. 唯一 Spec Approval Gate

1. 一次展示 `spec.md` 的 review summary、行为解释、关键硬件事实、policy/assumption、外部协议/credential reference、限制、验收和 Tier C。
2. 用户 approval event 绑定完整 `design_digest`，不是只绑定 `spec.md` 文本。
3. Approval 后进入 `CONTINUOUS`；除预声明 Tier C、用户主动 pause/change 或 hard blocker 外，不再询问是否继续。

#### 4. Approved design 绑定与硬件 Session 刷新

1. `bind_approved_design` 校验 revision、manifest、approval 和 digest。
2. 重新 probe 硬件；COM 变化但 stable identity 一致时自动刷新 session。
3. 多块匹配硬件且无法确定、目标芯片持续不存在或 identity 冲突时才 BLOCKED。

#### 5. 逐 Subsystem Bring-up Loop

对 Contract DAG 中每个 dependency 依序执行：

1. `prepare_attempt`：确认 selection gate、读取旧失败/日志/notes/acceptance、创建 attempt ID。
2. Knowledge route：
   - MCU-native：本地 IDF examples → headers/source → optional docs。
   - Trusted library：固定版本 README/API/example/Kconfig/dependencies/limitations。
   - Custom driver：确认 L2 完成；缺失/复杂/失败时调用可替换 deep Datasheet provider 完成 L2/L3。
3. `implement_or_patch`：只修改 owning component，建立 semantic API 和 selftest；`main/` 不泄漏底层细节。
4. `validate_source_and_contract`：检查允许写路径、managed component 只读、API boundary、依赖和 effective config。
5. 在任何硬件副作用前冻结 test expected/marker/range/timeout/evidence contract。
6. Build：必要时 set-target，通过 wrapper build，保存 log 和 build receipt/firmware hash。
7. Flash + Serial：停止旧 monitor、锁定硬件 session、flash、fresh boot/interactive selftest、bounded capture、保存 raw logs，最终无条件释放 port/lock。
8. Evidence Gate：将实际值与预先声明 expected 比较：
   - PASS：commit attempt/ledger，立刻进入下一 dependency。
   - PASS_PENDING_TIER_C：仅限 approved Tier C，继续下一 dependency。
   - REPAIR：分类、smallest patch、重新 source validate → build → flash → serial → evidence。
   - HARD BLOCKER：保存失败证据、尝试历史和唯一所需输入。

#### 6. 单次 Batched Tier C

1. 无 Tier C 自动跳过。
2. 有 Tier C 时先确保每项所有自动证据 PASS。
3. 一次展示所有 unavoidable physical checks；结构化接收 confirmed/failed/unable/not-performed。
4. confirmed 转 PASS 后立即恢复 continuous；failed 返回 owning subsystem，不新增后续确认批次。

#### 7. Integration Subgraph

1. 只有全部 subsystem 为 PASS/合法 Tier C 状态才进入。
2. 按 approved task/queue/resource architecture 实现 `main/`；若必须做重大架构改变，返回 Design amendment/Approval。
3. 运行 data-driven integration tests：并发、持续、重启/reconnect、backpressure、storage/network/peripheral failure、resource bounds、rate/latency/drop/jitter/recovery。
4. 每个 test case 独立产生 expected、receipt、raw log、Evidence 和 verdict。
5. 失败按 ownership 路由到 `main/`、owning component、dependency selection 或 host protocol reproduction；修复后重跑 affected tests。

#### 8. Requirements Closure

1. Deterministic validator 逐个 R*/required DR* 绑定 current design、firmware、hardware、test 和存在的 artifact/hash。
2. 检查 reboot/commit/power-loss、storage/reclaim/cleanup、RAM/heap/stack/handles、synchronization/failure isolation、protocol/credential/dependency/license。
3. 任一 PARTIAL/FAIL/BLOCKED/mock/stale/missing 或 conflicting limitation 都返回 owning stage，不能进入 Release。

#### 9. Release Subgraph

1. 保留 selftest/diagnostic source，但选择 selftest-off release profile。
2. 生成 fresh release build 和新的 firmware hash，不复用 selftest-on 镜像。
3. 释放 serial、flash release image、fresh reset/boot，并只依据 normal runtime marker 验证。
4. 检查 release 不进入 destructive test path、不生成 test-owned persistent junk、不暴露 credentials、不依赖 interactive selftest command。
5. 生成 release manifest/receipts/evidence；只有 closure 全 PASS 且 build/flash/runtime 绑定同一 release hash 才可完成。

#### 10. Finalize、恢复与最终响应

1. Orchestrator 原子提交 terminal event，更新 checkpoint，并生成 Hook-compatible `run-state.json` 投影和 DEVLOG 视图。
2. `COMPLETE` 的唯一成功位置仍是 verified release；Skill/CLI renderer 输出 build、flash、selftest-off、R/DR、runtime 和关键证据路径。
3. 任意进程/会话/上下文中断后，以同一 run ID 从 checkpoint + immutable artifacts 恢复；不从聊天摘要猜测状态。
4. 若 Codex UI 已关闭还要求继续运行，必须由独立 runner/service 承载；同一对话只是启动和观察入口，不是持久化保证。

#### 11. Revision/Change 流程

1. 将用户变更映射到 R/DR、policy 和当前 release；为 non-functional change 先定义 workload/metric/sample/duration/threshold。
2. 创建新 Design Package revision，计算 affected components、consumers、integration tests、closure rows 和 release config。
3. 仅重做 affected subsystem/selection/datasheet/implementation；若用户已明确提出该变更且无新增 policy，可按既定 approval policy处理；新增行为解释、pin、架构、验收、限制或 Tier C 必须批准新 digest。
4. 重跑 affected integration tests，但最终始终执行全量 current-revision closure 和新的 selftest-off release。
5. 旧 design、events、receipts、evidence 和 release 永不覆写。

- 完整流程状态：`DECIDED — 上述结构和流程作为一口气重构时的目标蓝图；具体 Python 包内文件可在不改变职责、schema、Gate 和 F01–F37 parity 的前提下做小范围合并。`

后续每一轮讨论按以下格式追加：

```markdown
### <主题>

- 用户关注：...
- 已确认约束：...
- 候选方案：...
- 权衡：...
- 决定：OPEN / DECIDED ...
- 待跟进：...
- 影响范围：...
```
