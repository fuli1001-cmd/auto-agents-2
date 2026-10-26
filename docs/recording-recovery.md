# 有限附件录制纠错设计（2026-10-26）

## 证据与目标

Demo6 TASK-002 的 32 项浏览器测试与 JUnit 均通过，采集在第 257 个物理文件被 `max_files=256` 正常拒绝。保留目录共 323 文件（208 PNG、115 JSON），约 9.62 MiB；140 个同用例 `attachments/` 文件与原件字节一致。没有旧运行混入，也没有视频/trace。作者使用了控制器指示且计划明确批准的 `outputPath` + `attach(path)` 方法；实际预算未进入两份保留的主要作者/审核上下文。

原策略在 `RecoveryController._capture_retry` 无条件先转人工，`adjudicate` 又将附件失败设为 blocked。这是明确策略，而非被上游模型越权覆盖。用户现批准仅为已核实数量超限添加受限纠错闭环。原冻结试验仍失败，后续为改版续跑。

## 不变约束

- 不改四个限额、物理路径计数、采集安全过滤、原失败附件或 hold。
- 不把通过 JUnit 当附件完整，不把能力目录当执行事实。
- 不降低测试、断言、视口、真实 PNG、状态 JSON、下载原件及文件往返要求。
- 不增加任意候选脚本执行能力，不授予产品、依赖或系统配置修改权。
- 不改已提交的人类答案，不伪答旧问题，不重置累计实现/诊断/无进展次数。
- TASK-001 有效验收保留；真正受影响的下游仍按现有规则失效、完整复验。

## 有限入口矩阵

|入口|新增行为|保持的门禁|
|---|---|---|
|设计/计划/实现/保护审核/诊断/自动决定|同一动态采集合同：实际预算、政策 hash、生产目录、计数与重复件规则|静态能力目录 hash 不变；旧冻结逻辑调用不重写|
|task `resume` 采集等待|严格匹配的 max_files 事实可建立 recording receipt；首次只有 diagnose|不匹配、缺失/损坏/过期证据、其它失败仍沿旧人工前提|
|诊断完成与动作选择|仅 diagnose / recording repair / attachment-method PlanPatch|现有无进展、许可、来源审核、源/计划绑定；不允许未变配置 recheck|
|task 实现和 prepared 重入|确定性具体文件范围 + 每份候选强制独立录制保全审核|当前 task 原权限交集；禁止新增/删除、业务/依赖/合同文件；真实检查随后照常|
|PlanPatch 提议、审核、prepared/激活、后续许可|只允许原任务附件方法的显式旧新 acceptance 映射；保留 recording 限制|原始义务独立审查；命令/数量/路径/依赖/其它任务/证据合同/预览不变；原子幂等激活|
|原 unanswered capture 问题|receipt 与第一次正式诊断许可完成后原子 supersede，answer 保持 null|并发用户回答优先，旧问题保留；未授予许可不消费等待|
|最终 delivery 采集|获得同样预算说明，继续原等待策略|本轮不新增无法明确归属任务的最终录制编辑授权|

## 数据与职责

### 动态采集合同

由 `collection` 从当前项目配置生成 `controller_attachment_capture`。包含 schema version、四个实际限额、现有 `policy_identity`、每次命令的独立根目录 `playwright-results`、按物理路径计数（包括同内容的拷贝）、原始文件与 Playwright attach(path) 拷贝均计数、PNG/JSON 真实落盘和完整采集要求。

这不是新的业务义务。它说明已有执行资源边界；作者和审核不能据此删减用户验收。初始计划可以选择控制器支持且预算内的存储方法。现有 `outputPath + attach(path)` 提示改为说明已采纳该方法时的复制成本，不再自行新增普遍强制方法，也不将未验证的 memory-only body 声称已归档。旧已批准方法不得因此消失。

共享 projection 必须保留该合同；新调用 input identity 包含它。旧的诊断/决定 frozen invocation 使用其原输入，不偷换；新的操作才获得新增合同。

### 采集事实准入

新模块只接收控制器当前 active failure，不接收模型自称失败。核对原任务、当前计划/任务 revision、worktree/base/source、实际执行命令身份、checks、JUnit、capture checkpoint、保留 hold 和已归档 artifact 所属 run/字节/hash。唯一允许的失败原因是 `evidence_collection_failed` 且 capture reason 精确为 max_files，进程 exit0、有效完整通过测试报告，其他命令已成功。

不能仅依据 `reason=max_files`：原采集器在尝试第 257 个文件时尚未打开它。增加独立只读元数据核对，在既有 metadata/depth/byte/time 安全边界内确认保留树没有符号链接/特殊文件、路径变化、额外字节/深度等问题；这不采集超限原件，不让失败变成功。核对结果作为新的诊断事实，完整最终采集仍须正式重跑。

receipt 绑定当前事实、任务/计划/源/政策、保留树身份和原问题集合。这里已经存在 active failure/attempt，不像 npm 入口需要创建新故障；只给同一 observation 加受限 receipt，归档转换前记录，不重新创建失败或增加 attempt/count。任务 recording receipt 只维持窄范围限制，不能永久豁免资源前提。每个实际 failure/attempt/evidence 的 observation 另行绑定与核验；新 capture 失败必须重新满足 max_files 条件。重复 resume 通过受管 helper 重验目录元数据，保留取消传播和硬超时。同步许可消费只校验不可变观察与有限归档原件，不再同步遍历未知目录；metadata_only 不代表当下已成功采集。删除 observation 或整个 recording 许可字段都受控拒绝。原问题尚未回答才可转换，历史空答案保留。RUNNING 的正常恢复入口执行转换，不离线手改 PAUSED。

### 权限与语义的分离

保留原 `task_scopes` 作为任务定义一致性校验，新增 recording restriction 作为更窄、叠加的执行约束，不能用较宽 task_scopes 绕过。具体可写文件是原 task.allowed_paths 与其正式检查输入的交集中的现有受保护测试/录制配置文件；禁止目录/glob 自动授予未来文件，禁止产品源码、依赖锁、需求/计划文档、新增/删除。共享 helper 中可能同时含断言，路径不是语义证明。

每个修改候选必须在应用前取得独立审核的显式结论：仅改变附件记录/表示；业务断言、测试数量/执行、视口、所需 PNG/JSON/下载原件及状态内容完整保留；符合当前已激活方法。审核绑定原源、计划、政策、prepared diff/preimage 与候选 hash；不允许靠作者自称或已有泛泛 approved。审核不能证明任意 JS 语义，真实完整检查与最终审核仍是必需门禁。

prepared 重入重新核对上述绑定和 preimage，不能因源中已有部分修改而省略限制。失败/拒绝后限制跟随当前责任任务，不能经普通 recovery failure、PlanPatch followup 或新的 task round 丢失。新证据可能改变诊断，但不会自动授予业务修改权；超出录制边界必须返回正式人工/设计讨论。

### 正式附件方法修订

复用已有 `ContractCorrection` acceptance 旧/新映射与独立 `ContractReview`。录制路由下附加结构约束：只修改原 task 的 acceptance 中明确映射的模型派生附件方法，以及相应描述/计划正文；检查 Command 全字段、expected_tests、可写路径、任务 origin/依赖、其它任务、最终/准备命令、preview、environment 必须完全不变。禁止借现有普通 PlanPatch 的扩路径能力增权。

实际 EVID-TASK-002-ATTACHMENTS 的 origin=user，value 精确包含 acceptance[7] 全文；业务证据要求属于用户，但其中具体 attach(path) 技术方法可以正式纠正。因此仅允许同步对应同一 obligation.value；ID、origin、requirement_ids、capability/版本、producer、阶段、target/index 等完全不变，不能把整个用户义务改标 model_derived。现有保守失效把任何 obligation 变化都算全部任务；只对这种“本地 acceptance 正式映射，义务其余身份和语义责任元数据完全不变”的值同步，只在控制器录制 receipt 与许可绑定齐全时使用本地失效；普通相同形式的修订仍按旧保守规则全量失效。本轮不向依赖任务新增录制授权：若失效闭包超出单一责任任务，受控拒绝并报告范围，不能自动扩大。审核仍必须确认没有通过改 value 弱化真实内容。

额外录制保全审核绑定实际旧新计划，明确保留真实用户业务验收、附件内容与方法能力。拒绝或陈旧候选回到既有有界诊断，不自动批准。prepared journal 带录制 restriction 和审核 hash，在 Store.activate_plan_patch 的事务内复验。激活后的原任务 repair 许可继承同一限制；本轮仅实现单一责任任务闭包；额外依赖任务不自动获得修改许可。保留旧候选、批准、计数和来源记录。

独立设计审查查明，本案 design 的 TC-18 已正式采纳 `outputPath → attach(path)`。因此 PlanPatch 不能隐式覆盖仍生效的 design adoption。保留 TC-18 的录制表示优化可继续；计划层的方法修订必须与已采纳上游约束相容。若诊断证明必须改变 TC-18，则保留候选并返回正式 design 来源责任讨论；本轮不自动增加 design 写入/重入权限、不把上述计划修订当作设计撤销。审核必须显式确认上游方法边界，不能只检查计划文本。该限制是本轮的明确正常退出条件。

## 验证矩阵

1. 旧代码组合反例：实际 Engine task max_files 仍进人工；作者/审核主 context 缺实际预算。测试先在旧代码呈现此行为，再对新合同断言。
2. 原归档只读回放：精确 323/257/256、140 同用例复制对、通过的 JUnit/source/command/policy/hold；不修改/补写历史。
3. 小型真实 Playwright → VerificationService：同内容不同路径仍分别计数，成功只在完整采集后；PNG/JSON 字节与归档相同。
4. 准入反例：字节/时间/深度/unsafe/其它检查失败、缺/损坏 artifact、source/plan/policy 漂移、非原任务、最终 phase 均不能进入新授权。
5. 实际 recovery 组合：首次 diagnose、完成诊断后受限 repair 或 PlanPatch、未改变输入 recheck 排除；建议不等于权限、无进展计数不清零。
6. 编辑反例：产品/依赖/新增/删除/未审/语义保全否定/陈旧或篡改 diff 拒绝；合法具体录制 diff 可应用并重新完整检查。
7. PlanPatch：旧新映射、业务/命令/测试数/视口/原 TASK001 不变；非法路径或其它任务/证据契约变更拒绝；prepared 中断、重复激活、followup 限制继续有效。
8. 等待转换：空答案保留、并发回答拒绝、CAS 漂移拒绝、重入幂等；真正人工前提不绕过。
9. 定向与代表性现有回归、静态检查；最终冻结源码全量和真正独立 astra/max 复审。改变源码后旧全量不覆盖新版本。

## 风险、兼容与回退

主要风险是受限权限在多个既有消费入口丢失，因此使用同一 restriction 校验函数，并在准备/应用/激活各入口执行；测试必须调用真实 Engine，而非只测试 helper 输出。语义审核仍有模型误判风险，明确与确定性约束分开记录。

默认无 recording receipt 的旧许可保持原语义，不能悄悄升级。既有已回答问题不迁移。新 receipt/permit 版本明确；不支持/不完整数据受控拒绝。停用新路由可保留 receipt 和现场返回正常暂停，不删除事实或伪造旧结果。旧失败 hold 继续保留，本轮不改审计保留/清理策略，也不承诺修正源后自动释放它。

不做：提高限额、内容去重计数、任意候选采集器、完整 JS/TS 语义解释器、全工作流权限框架、未确认 final 多任务路由、审计保留系统、历史 demo 重开。

## 实现核验补充

独立初版审查提出的 observation 豁免、旧问题前缀、原样候选、激活后复验、全局失效外溢、拒绝理由丢失、缺失准入证明及受管目录遍历问题均作为组合反例核验。生产解析的嵌套录制/合同审核继续要求既有 authority 字段。真实归档回放只拦截出版，不修改原数据库；小型浏览器探针与产品隔离，替代存储形式仅验证采集字节和物理计数，不能替代 Demo6 上游 TC-18 的正式审批。

扩展回归保留了既有逐角色上下文严格相等断言，发现新增预算在 `_invoke` 临时注入而 PlanPatch journal 未保存。修复为新 PlanPatch 创建时即冻结预算，由作者/审核共同继承；旧冻结记录预算缺失保持原样。新增预算字段真实引用和 prepared 重入、旧记录兼容反例，不以删除严格断言消除失败。

冻结预算例外只由 `recovery/patch`、`recovery/patch-review` 的控制器操作身份及实际已保存 patch-context 决定；新的 authority-reaudit 仍取得当前预算，不依据任意 review_kind 将新审核误判为旧冻结调用。


## Completed diagnosis receipt compatibility

A diagnosis originally invoked under `review` can have a completed invocation
receipt without a `recovery/invocation-inputs` checkpoint. Additive attachment
budget guidance must not cause that same completed diagnosis to be called again.
The existing diagnosis identity consumer recognizes the finite historical
encodings lacking the known capability catalog, the current attachment contract,
or both. Every candidate identity still includes the source, remaining factual
inputs, schema, configuration, provider execution identity and original stage.
Unknown or modified guidance is not stripped. The original receipt is retained.

Native diagnosis/decision logical calls keep their frozen context on completion
and interruption; a separate new operation receives the current policy. A migrated
old review receipt continues to require its complete configuration identity.
This compatibility does not grant a recovery action, change budgets, reset counts,
or make an earlier diagnosis evidence that a later collection succeeded.

The full02 regression produced a second model call from an unchanged old review
receipt. The original regression assertion is retained, with additional real
Engine invocation cases for both historical encodings, repeated migration, input
drift, unknown guidance, interrupted native calls and new-operation policy refresh.
