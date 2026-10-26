# 两个 P0：正式义务检查闭环与恢复组合验证

基线：`db4daa3f88d75868b9835677ad496ab651d701c3`。范围仅本设计；不改 Demo5，不启动新产品，不改模型配置。

## 证据与现状

Demo5 `run_22419c9eef564f2b` 的 `plan_revision/journal/*` 原件：revision 1 的 TASK-001 acceptance[8] 同时要求单测、tsc 和生产构建，但其 checks 仅含前两项及环境探针；runtime_prepare 有构建不代表该任务会选择它。revision 2 明确加入任务构建。另一条 revision 2→3 修订把全源码 tsc 加入 TASK-002：修复产品后的任务检查不能产生关闭最终类型错误的实际证据。TASK-002 原验收条款并未逐字要求 tsc，原本只在 final_checks 中；不能因此判原任务计划违规，也不能声称新映射会提前发现 TypeScript 产品错误。该反例检验 final 失败→正式 PlanPatch 加入修复验收检查→修复/重检→final 的闭环。已有 `verification_commands()` 只选择任务输入依赖的 preparation，最终检查才 prepare_all；这一职责划分应保留。

现有 EvidenceContract 说明有限能力与来源，不等于运行事实。`reusable()/assess_observation()` 已验证实际命令、源码、环境、输入、报告、附件及产物。任务审核前后经过 `_require_task_evidence()`，最终审核前后经过 `_require_candidate_evidence()`。初始计划经 `validate_admission_review()` 接受；PlanPatch 经同一审核及 prepared journal 再验证、Store CAS 激活。恢复 repair/recheck 已共用调度，验收与许可 outcome 已原子提交。本轮不重写这些机制。

## 最小设计选择

**不让作者新增一套义务语言。** 正式义务仍来自既有计划验收条款/检查及其来源合同。计划独立审核在现有 `EvidenceAssessment` 内新增版本化 `check_production`：逐条解释任务 acceptance 如何得到证据；最终命令单独列为最终阶段生产者。计划作者只得到同一说明，仍负责原计划检查设计。审核者不能创造义务或执行命令。

映射条目包含：原目标（任务 acceptance 或 final_checks）、任务 ID/索引/原值、有限类别（build、typecheck、tests、attachments、command、review）、本阶段检查的索引及完整 Command 快照、必要附件媒体类型、简短理由。每一条既有任务验收必须有且仅有一条映射；一个混合条款可以引用多个生产者。review 表示需要现有语义/来源/环境审核的条款，并非免验；此类不得声称命令或附件已经产生。build/tsc/测试/附件要求不能以 review 代替，识别这种语义错分仍是独立审核职责，代码不能证明任意文字的含义。

任务映射只可引用该任务 checks 中的完整命令。若所需 build 只在 runtime_prepare，作者应通过正式计划将该命令也列入任务 checks，既有归一化负责去重。这避免把依赖自动选择误当必运行。最终映射只引用对应 final_checks，绝不要求每个任务执行所有最终命令。相同 argv、不同 inputs/expected_tests/cwd 等不是同一生产者。附件只核验已支持 PNG/JSON 等实际归档，不新增采集能力；媒体类型存在不证明内容语义正确。

## 入口与数据流

1. `admission_context()` 提供有限版本协议。初始计划与 PlanPatch 独立审核使用同一 schema、说明及确定性验证；不得批准缺项、重复、越界、错阶段或旧快照映射。已拒绝意见可记录不完整映射，但不能被批准消费。
2. 现有 admission receipt 保存审核映射、原计划/审核哈希及协议版本。初始接受与计划 checkpoint 同一批写；PlanPatch 与计划/版本/失效状态在同一既有事务激活 `evidence_admission/active`。保存 required 标记，使缺 active 记录不能被解释为兼容旧版。
3. prepared 校验重建同一映射、检查旧新绑定，不凭审核 approved 放行。新审核不能把已接受的命令闭环条款改成仅 review；此类改变须走原有正式条款纠错并保留旧新映射/实质义务的独立审核。正常添加命令/加严门槛仍可经现有 PlanPatch，相关任务失效重验。
4. `_require_task_evidence()` 取得与当前计划绑定的 active receipt，对本任务生产者引用解析为现有 `verification_commands()` 实际归一化命令。复用 `reusable()` 的严格结果核验，再记录本次闭环观察（不是新的许可）。漏结果、结果错输入、附件原件缺失不进入/通过实现独立审核；缺合同/错绑定产生 contract 故障，缺实际结果产生 evidence 故障，沿现有诊断→责任阶段 PlanPatch/重检处理。
5. `_require_candidate_evidence()` 最终阶段核验当前完整候选实际执行的 task/final 映射；重集成同样执行完整候选检查，复用这一门禁。映射不增加或裁剪任何命令。其他 runtime/completion 不被错误要求重复所有最终命令，仍保持原有严格运行时、浏览器及交付证据门禁。完成后的历史项目不重开。
6. 无变更复用、任务缓存审核、重检、准备激活及恢复使用同一 receipt/版本绑定；新协议有变化则不得沿用旧新协议混合的审核/观察。旧版本已批准记录保留可读，不伪造映射；新计划/新补丁审核必须新协议。原先冻结的旧 prepared journal 按原协议验证，明确不冒称新闭环已验证。

## P0-2 组合矩阵

在已有真实 Engine repair/recheck 集成夹具上增加**生产严格解析器**的 JSON 往返及新闭环门禁，运行小型真实命令，不安装三个旧 demo。

| 场景 | 正向验收 | 反例 |
|---|---|---|
| 初始计划 | 每任务闭环，final-only 命令只在最终执行 | build 只列 preparation、漏映射、同名不同输入、跨任务引用 |
| 正式 PlanPatch | 新检查加入、prepared 中断恢复、单次激活、原次数保留 | 过期计划/来源/映射、未审/缺记录、review 降格 |
| repair→新基线 | 正式授权责任任务先修，旧依赖验收失效后重验 | 建议冒充许可、范围/输入不匹配 |
| recheck | 已应用/已测试候选复用且不再实现；闭环结果必须实际存在 | 无写权限、缺/过期证据不得完成 |
| 接受/结果/集成中断 | 验收与 outcome 一致；恢复仅集成、不重复修复 | 不完整旧记录拒绝、多任务结果不串用 |
| 附件 | 实际文件哈希/媒体类型/来源匹配且审核读内容 | 仅目录/数量、缺正文、失效源不能关闭 |

原 Demo5 两份计划只读回放并记录原件哈希。测试说明人工构造映射用于表达已知反例，不假称模型在历史上给过新协议回复。新增测试既要旧代码失败，也要合法流程交付；现有广泛反例保留，不扩为所有组合。

## 范围、风险、验收与回退

预计修改 models/evidence_admission、新有限 check_production 模块、Engine 两个门禁、Store 既有激活事务、提示说明及相关测试/文档。不加命令执行器、不增加权限、不增加业务义务、不改变恢复预算或原型审批。不做自然语言关键词裁判。审核语义错分仍可能发生，需真实业务验收及来源审查；不能声称全量通过即长期稳定。

风险：命令归一化/合并须按全对象及执行序列解析；旧记录兼容必须不静默标为新协议通过；新 metadata 不能绕过已有检查保留。独立设计审阅先于编码。定向原反例、组合测试、静态检查、冻结源码全量、固定提交独立复审全部通过，再提交父会话新 demo6 规格。D 盘约 6.44 GiB，仅小隔离工件，测试前后测空间；不自行删文件。回退仅回退代码提交，不改原运行历史；运维上禁止把含新协议的未完成运行交旧版本继续。旧代码不认识新checkpoint，本轮不假称可以阻止用户手动降级执行，也不为此引入数据库迁移项目。

## 独立审阅期间收敛的实现约定

- 不更改 PlanResult/PlanPatch 的义务载体。新映射放入独立 EvidenceAssessment，允许历史缺字段反序列化并保持旧序列化哈希；当前协议的批准则明确要求完整映射。能力目录仍不是执行事实。协议本身在引用目录标记 controller_protocol，不能作为项目通过证据。
- 归一化索引由 builds.py 现有算法在同一次计算中返回原 checks→实际合并 Command 的对应关系；不按 argv/名字自行匹配。任务全部已声明检查仍执行，映射不裁剪命令。final_checks 与 task checks 两类引用的 phase 由控制器确定，不接受模型任意指定阶段。
- PlanPatch 的冻结审核上下文附带已接受的映射回执。旧条款的 command→review 降格、删除或必要附件缩减须原有正式条款纠错，普通 metadata 重排不授权。新映射语义变化保守使相关验收失效；即使任务文字未变化，也不能静默复用旧审核。prepared 校验重建这一差异，CAS事务同步激活计划和映射。
- 旧 prepared context 没有新协议时按原冻结协议验证，不能产生新式成功回执；已有新 required 标记的运行禁止回退旧协议。新候选按当前协议重新独立审核。no-change继承先验真原run映射与同一计划；旧source_binding留作来源，不冒充新run原始输入。当前覆盖判断及真实新检查仍走现有流程。
- 实际附件采集记录只有 path/purpose/artifact，没有可信 MIME。新映射使用有限 PNG/JSON格式，读取归档字节验证内容哈希及PNG签名/JSON可解析；不因路径后缀或模型声称就认定文件有效，不增加原条款没有的路径模板义务。语义与逐用例完整性仍需独立审核。
- 任务和no-change已有post-review gate→consumer complete顺序。final成功分支目前顺序相反；把成功complete移到最后源码/证据检查之后，复用已有pending引用保护直到最后门禁结束。拒绝分支不变。这样不新增永久引用或“consumed等于批准”的规则；审核后原件释放仍按原策略。增加GC时序及源变化反例。
- metadata-only映射变化用共享差异函数计算新增affected及依赖后继，不新增reopen。RecoveryController._patch先保留原affected快照作为审核输入；审核返回并通过后补齐新增affected的真实快照并重新验源，journal记录最终总affected；prepared验证重建同一差异，Store仍同一CAS事务激活。reason说明变化不触发失效。冻结审核上下文声明这条确定性失效规则，审核者不自行指定新权限。

- 已有 test_file_attachments 合同如果直接指向 checks/final_checks/runtime_prepare/runtime_probes，也按当前真正执行阶段读取原件；final-only 探针不成为各任务的义务。当前协议的 accepted 回执丢失/损坏属于状态冲突，沿既有等待核对流程处理，不伪造新回执或当作旧版兼容。
