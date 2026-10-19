# 验证记录

## 自动恢复边界系统修复（2026-10-19）

逐阶段审计发现并修复了动作名强制人工、内部覆盖证据要求用户补齐、模型格式错误丢失类型、README 错退规划、候选路径拒绝误分类、旧许可范围影响新决策及分类日志误导。共性、处理矩阵和保留的人工条件见 [边界审计](quality-first-implementation.md#自动恢复边界审计2026-10-19)。

新增 `test_action_authority.py`、`test_proposal_recovery.py` 和 `test_generation_routing.py` 共 47 项，覆盖五种阶段回退、自动化关闭、旧回答与未答问题、完成诊断复用、发布和并发发布保护、资源前提、不同范围的旧许可、十类普通生成／审查响应、格式失败重启、成功响应保存后的中断、明确阶段回退生成新候选、审查问题范围和原逻辑调用回执、覆盖证据、澄清批次，以及 README 和原型候选拒绝。既有恢复、传输、原型、诊断及交付专项一起运行。

最终完整套件 **1356 项通过，0 失败、0 跳过**，耗时 **909.05 秒**；`ruff check src tests` 和 `git diff --check` 通过。完整测试在全部实现及回归代码修改之后运行，151 个源码、测试和配置文件的指纹在验证前后保持一致。[最终验证与源码指纹](evidence/action-authority-validation.json) 保存实际终端摘要。此前为补充修复而中断的运行不计入本次交付结论。

只读读取 demo 已完成的 `design` 诊断，并核对项目自动决策处于开启状态。使用当前路由规则回放得到 `decision`，阶段名不再产生必然的人工确认；回退范围明确包含设计及后续阶段、受影响任务和需要保留的验收／预算／审批历史。见 [demo 路由回放](evidence/action-authority-demo-replay.json)。该回放没有调用真实模型、推进 demo 迭代或替用户回答问题；它证明控制器会进入决策评估，不保证模型一定选择批准。

## 设计文件应用与阶段权限修复（2026-10-19）

只读检查 demo 的 `run_d179c491f3384771`：已保存的设计包含修改入口文档和新建 `design-pagination.md` 两项提案，旧流程未应用 `files`，却单独覆盖了入口文档。原入口替换锚点在迭代基线上匹配一次，在当前集成候选上匹配零次。计划又将缺失设计文件的写入分配给 T007，因实现阶段契约保护被拒绝；随后局部计划修订删除了既有验收条目，被控制器再次拒绝。

修复将设计文件应用、替换纠错、部分应用恢复和独立审查纳入设计门禁。显式入口替换不再被 `document` 覆盖；规划使用最新源码快照，并提前拒绝将其他阶段的契约文件分配给实现任务。设计审查的精简输入保留实际设计材料。恢复摘要显示补丁拒绝及修订校验错误；已有上游门禁授权继续核验原绑定。

新增 23 项回归覆盖入口和附加文件、无效范围、原锚点纠错、部分写入中断、保存审查后中断、生成后及审查期间的用户编辑、拒绝审查、旧设计回退归档、任务轮次与原型批准保留，以及计划范围与输入呈现。原任务审查中断夹具明确只拦截实现审查，保留原有“重启不增加实现调用及轮数”的断言。

受影响子系统先完成验证，最终完整套件 **1309 项通过，0 失败、0 跳过**，耗时 **816.93 秒**；`ruff check src tests` 和 `git diff --check` 通过。完整测试期间 146 个源码、测试及配置文件指纹不变，详见 [最终验证记录](evidence/design-documents-validation.json)。

使用真实 demo 提案和原基线在临时目录回放了两文件的部分应用与恢复，结果摘要与逐文件哈希见 [设计提案回放](evidence/design-documents-demo-replay.json)。真实 T007 计划已在规划校验处被准确拦截。该回放只验证控制器文件应用和权限规则，没有调用真实模型做语义设计审查，也没有推进 demo 的迭代或替它回答授权问题。

## 全阶段模型输入选择验收

本轮按调用用途选择必要正文、源码、研究章节、检查和恢复记录，完整原始事实继续归档。呈现版本与逻辑调用身份分离，短必要正文直接内联，大材料通过绑定来源的引用补读；未知归属的共享约束保守保留。规范及范围见 [按阶段选择模型输入](design.md#按阶段选择模型输入)。

最终完整自动测试为 **1286 项通过，0 失败、0 跳过**，pytest 控制台报告耗时 **778.67 秒**；`ruff check src tests` 和 `git diff --check` 通过。新增输入选择测试共 38 项，覆盖全部角色、精确章节与链接定义、来源绑定、跨任务约束、源码依赖与嵌套规则、历史基线、证据状态、引用歧义、冻结恢复和逻辑调用身份。完整套件在最后一项生产代码修改之后重新执行。[最终检查摘要及源码指纹](evidence/context-input-validation.json) 保存实际控制台结果与本次源码、测试和配置文件摘要。

使用 demo 已归档的五份研究文档进行离线回放，不重新抓取网页，也不改动 demo 状态。原研究审查 prompt 为 **1,562,609 字符**；最终选择、附件路径和 prompt 编译后为 **57,130 字符**，schema 为 **1,308 字符**，合计 **58,438**，低于 64,000 字符软目标。三个精确锚点选择完整章节；无锚点长文提供索引和原文入口，短完整页面直接内联。见 [回放记录](evidence/context-input-demo-replay.json)。

真实对照在隔离项目运行，读取用户已有 provider 配置，使用 `gpt-6-astra` 的原有 medium/high 阶段设置和真实只读沙箱。源码修改由控制器在隔离候选中应用，并实际执行加法断言；研究检查必须识别章节外的 `project:member` 约束；视觉检查必须识别图片中的 `Delete all` 按钮；决策结果执行正式引用验证。最终五组两版均通过预设正确性检查。

| 场景 | 原输入字符 | 最终输入字符 | 原实际输入 token | 最终实际输入 token |
|---|---:|---:|---:|---:|
| 研究审查 | 132,115 | 6,780 | 41,208 | 32,791 |
| 任务实现 | 104,022 | 10,779 | 32,102 | 16,760 |
| 任务审查 | 185,402 | 12,336 | 116,015 | 34,901 |
| 恢复决策 | 88,817 | 16,672 | 52,222 | 17,494 |
| 视觉审查 | 9,863 | 6,115 | 16,826 | 16,144 |
| 合计 | — | — | **258,373** | **118,090** |

最终配对样本累计输入减少 **54.29%**。原版决策需要一次引用纠错，表中计入原调用及纠错调用的输入；最终新版决策一次通过。早期精简版本因小文本引用和省略历史结果导致额外补读，累计输入一度上升；改为短必要文本内联、完整短链接定义内联及相同历史结果精确去重后，只复测受影响组。原响应、被替代观察及消耗均保留，没有以首包变短替代实际用量验证。

整个对照过程共 **15 次主 CLI 调用、5 次读取探针**，包含重测与格式纠错；全部累计用量为 **861,965 token**（输入 856,314、输出 5,651，缓存命中 381,696 单列，不重复加到 total）。这些总消耗包括被替代的观察与探针，不等于表中的最终配对主调用用量。五组固定场景属于有限烟雾验证，不能证明所有项目的质量或消耗等价。见 [完整真实对照记录](evidence/context-input-comparison.json)。

复现入口为 `tests/manual_context_comparison.py`，需要显式指定 `--provider-project`、隔离的 `--project` 和 `--output`；不会激活其他 provider 或推进配置来源项目的迭代。`--rerun` 和 `--revision` 只用于保留旧记录后重测指定新版场景。

## 恢复决策与进度停止验收（2026-10-16）

基于 `3afa351` 实现独立诊断配置、已有 provider 的有限决策、任务血缘进度停止及阻塞摘要。诊断默认 `max`，决策默认 `deep`；自动实现累计上限 12、自动诊断累计上限 4，每次追加 1。默认同时适用于新项目与旧项目续跑，不改写用户模型/native profile，不清零历史、不伪造原型人工审批。配置与关闭方法见 [README](../README.md#自动恢复与停止)，契约见 [设计规范](design.md#恢复与有界循环)。

先验证受影响子系统：配置、任务执行、恢复、回退、provider、交互等组合测试通过 149 项；决策/环境恢复专项通过 45 项；最后补充用例数量不能伪造进展的边界后，决策/回退及更新的恢复夹具组合通过 42 项。新增 `tests/test_decisions.py` 共 31 项，覆盖固定义务的历史最好结果、无进展停止、缺失证据补采边界、额度上限、人工回复竞争、事务回滚、状态漂移、已落盘响应恢复、原型审批排除、事实答案批处理和暂停保留。

隔离项目中使用真实 pytest/JUnit 验证了两个任务循环：六个固定用例逐轮通过，任务在第 6 轮完成；检查已通过而审查只改变措辞时，第 3 次观察后进入等待，不启动下一轮实现或诊断。模型响应由测试代理/协议夹具提供，没有收费模型调用。没有读写或重启正在运行的 demo 项目。

首次全量运行发现依赖解析故障的旧断言仍期待消耗 3 轮，新规则在连续两次缺少有效证据时停止；更新断言并验证停止后没有诊断/决策调用。同时补充升级前已回答问题的兼容测试。随后全量为 943 通过、2 失败：一个旧夹具仍替换旧授权入口，另一个仍断言诊断使用 review。已改为实际人机问答入口注入源码漂移，并更新独立诊断阶段断言；两项分别复测通过，原有现场保留及人工审批断言保留。

最终全量 **946 项通过，0 失败、0 错误、0 跳过**，JUnit 记录耗时 **628.249 秒**。[完整 JUnit](evidence/decision-recovery-suite.xml) 与 [最终源码摘要](evidence/decision-recovery-source-manifest.json) 独立归档，完整套件对应最终生产源码及测试，没有以分批计数替代全量通过。

Ruff、`git diff --check`、项目 Conda 环境 `pip check`、CLI 启动和 wheel 构建通过；wheel 中全部 61 个 Python 源文件与当前源码逐字节一致。数据库仍为 v6，检查器仍为 v10；普通阶段保持旧提示/调用身份兼容，新角色有独立阶段身份。OCR 历史报告及 R1–R5 验证证据保持原样，本轮没有新增 OCR 扫描。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-decision-recovery-acceptance.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/aa2-decision-recovery-wheel
```

## 第五轮 OCR 修复验收（2026-10-16）

基于 `072de83` 核对 37 条评论，实施 35 项对应修复；原生 Windows 范围项和历史 XML 重名项保留反证说明。逐条结论、明确复现的 R4 回归、扫描范围变化及循环修复效果分析见 [第五轮核对与实施](quality-first-implementation.md#第五轮-ocr-核对与实施2026-10-16)。秘密文件保护继续只依据路径，日志独立脱敏。

最终源码完整回归 **915 项通过，0 失败、0 错误、0 跳过**，JUnit 耗时 **742.555 秒**。相较基线新增 **80 项**永久行为与故障注入用例，集中于四个 `test_ocr5_*` 模块。[完整 JUnit](evidence/ocr5-fixes-suite.xml) 独立归档；先验证受影响子系统，再对最终全部源码和测试执行完整套件。

新测试覆盖正常执行、取消、清理失败、提交后失败及重启，使用真实 Git、受管子进程、bubblewrap、Chromium、本地 TCP 和 SQLite。8 GiB 附件用稀疏文件测试读取前拒绝，没有实际复制 8 GiB；另外验证读取期间变长、总量/数量限制、元数据保护、辅助进程超时及事件循环可响应。UTF-16/32 BOM、FIFO/链接拒绝、原始 deflate 头冲突、目录链接镜像、畸形和旧版进程记录、字面 SQL/Git 前缀都有针对性检查。

恢复证据包含集成工作区快照提交后的中断、verification_only 修订/恢复时重新核验覆盖、模型结果落盘后的 OSError/ValueError 不重复调用，以及附件超限进入 WAITING_USER、同配置不重复执行、调高限制后成功并释放旧现场。测试中的 provider 是实际协议子进程夹具，不是收费模型调用。用途明确的普通附件在没有消费者后释放，打开的问题、核心证据、未知用途旧附件和混合用途对象继续保留。

真实远端 Conda/npm 安装后的 Python、React/Vite、Next.js 三种交付全部通过。两个前端均在工作区被 GC 回收后，从主目录启动生产预览，通过桌面/移动端点击和文本断言；移走 dist/.next 的反向检查均拒绝通过。[交付记录与截图索引](evidence/ocr5-delivery.json) 独立归档。Next.js 首次运行曾被其新增 route-cache 的写入误判为产物变化；[前后摘要](evidence/ocr5-next-runtime-cache.json) 保留了失败依据，识别明确的运行缓存后重跑三种交付通过，没有忽略服务代码变化。临时项目和旧失败报告仍保留，脚本只清理自建运行依赖。

最初全量为 850 通过、6 失败：旧锁重放测试断言了旧文件路径；归档移入辅助进程后，本地大小错误文案和链接附件分类需保留；deflate 错误需区分超限与截断。修正后又一轮检出了截断文案的两处断言差异，随后完善了明确的错误分类。最终全量覆盖之后补充的资源归属和构建缓存用例，不以分批计数代替完整通过。旧 R1–R4 XML 未改写；历史 repeat-6 XML 仍有 67 行、66 个原始名字，当前解析器验证为 67 个不同 ID。

Ruff、`git diff --check`、项目 Conda 环境的 `pip check` 和 wheel 构建通过；wheel 中全部 **59 个 Python 源文件**与最终源码逐字节一致。本轮未进行新的 OCR 扫描或收费模型调用，未声称扫描已零问题。用户模型、推理配置和权限不变；数据库仍为 v6，检查器为 v10，提示版本为 `2026-10-16.2`。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/ocr5-delivery-acceptance.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/ocr5-wheel-output
.conda-env/bin/python tests/manual_delivery.py
```

## 第四轮 OCR 修复验收（2026-10-16）

基于 `86a3b6f` 实施，核对 R4 全部 43 条评论：38 条成立、2 条部分成立、1 条误报、2 条不纳入。处理 40 条对应项，其中 25、26 按用户要求替换为秘密路径保护。四轮扫描范围、逐条关系、反证和五处有旧／新执行依据的回归见 [四轮 OCR 对照](quality-first-implementation.md#第四轮-ocr-核对与四轮对照2026-10-16)。

最终全量 **835 项通过，0 失败、0 错误、0 跳过**，JUnit 记录耗时 **504.058 秒**。[完整 JUnit](evidence/ocr4-fixes-suite.xml) 独立归档，前三轮 XML 未修改。新增三个 `test_ocr4_*` 模块共 64 项，覆盖真实调用链、协议子进程和中断窗口；先运行受影响子系统，再执行全套。

过程中首次全量为 826 通过、2 失败：修正了链接目录副本身份误包含未选秘密文件的问题，并将旧“启动后登记失败”夹具改为新的“登记失败时尚未启动”生命周期。随后一轮为 828 通过、3 失败：坏回执隔离规则使无摘要的旧测试回执被保留；补齐测试回执摘要，保持原日志回收断言。补充空回执、无效摘要及损坏 JSON 的隔离测试后，对最终全部源码和测试重新运行上述完整验收，未用分批计数替代全量通过。

实际执行包括 Git、bubblewrap、Chromium、本地 TCP、受管子进程和离线本地 wheel 安装。线程创建的服务子进程能通过实际 TCP 所有权检查；普通 HTTP 响应关闭不再挂住反向泵；启动通道失败和握手取消会等待清理；正常运行中的收养僵尸被回收。慢日志写入期间事件循环仍能取消并停止工作负载，文件等已提交 I/O 完成后才回收。监督循环超过 200 次、proc 读取和单个 kill 失败的分支用隔离故障注入验证，不在宿主系统制造不可杀进程。

有效模型响应及用量在清理未确认时保持持久化，门禁阻塞；确认清理后复用同一结果，测试账本证明没有第二次调用。协议子进程实际读取与秘密文件值相同的普通源码、提示词和附件，同时确认 `.env` 不在模型工作区。此策略依赖真实秘密仅存秘密路径；日志继续独立脱敏。

首次全满足前端用实际控制器、Git 和核验命令走完整交付，保留原业务源码、不调用实现阶段；该专项测试的模型、研究和浏览器结果为受控夹具，验证原型人工批准及门禁顺序，不冒充真实模型端到端验收。既有独立浏览器测试仍使用真实 Chromium。

其他覆盖包含构建准备／发布／恢复故障及文件同步顺序、held 产物依赖闭包、v5 缺列恢复和新库初始化故障、复验 reserved/started/completed、审批前树／HEAD 基线、诊断拒绝后的身份保留、同树合并父提交，以及 GIT_* 环境污染和 DB／恢复文件并发变化下的快照拒绝。gzip/deflate 正常响应、压缩炸弹、截断体和慢速正文分别验证；单次 I/O 超时不能替代整体获取时限。

Ruff、`git diff --check`、项目 Conda 环境 `pip check`、wheel 构建和内容校验通过。wheel 中全部 **53 个 Python 源文件**与最终源码逐字节一致，包括新增 `http_body.py`。本轮未执行远端 Conda/npm 安装、收费模型调用或新 OCR 扫描，未宣称扫描已零问题。模型、推理设置和执行权限未自动调整。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/ocr4-acceptance-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/ocr4-wheel-output
```

## 第三轮 OCR 修复验收（2026-10-16）

基于 `425db9d` 实施后的工作树。R3 的 35 条评论经核对，31 条成立、1 条部分成立，本轮处理合计 32 条；1 条误报和 2 条不纳入项保留结论。逐条根因、三轮关系及反证见 [三轮 OCR 对照](quality-first-implementation.md#第三轮-ocr-核对与三轮对照2026-10-16)。其中两处为第二轮引入的回归，不把所有第三轮发现统称为新问题。

最终全量 **771 项通过，0 失败、0 跳过**，耗时 **473.89 秒**。相较第二轮的 674 项增加 97 项：第三轮三个测试模块包含 95 项，旧结构迁移扩展到 v4 增加 2 项。[完整 JUnit](evidence/ocr3-fixes-suite.xml) 单独归档，既有证据未改写。先完成受影响子系统和故障注入验证，再执行完整套件；中间全量发现的显示器夹具问题已补建真实迭代并保留原清理断言，最终全量针对更新后的全部源码重新执行。

实际运行覆盖真实 bubblewrap、Chromium、Git、子进程与本地 TCP。沙箱内创建的 result.json 链接无法使控制器读取屏蔽文件，FIFO 和超大结果被拒绝，缺失结果仍能使用有效 stdout。工作命令退出后仍能清理创建新会话的后代，重复 stop 不在控制器中发送数字进程组信号。错误预览服务在三次有界尝试中均收不到 HTTP 字节；正常受管应用仍能完成双视口截图与动作验证。第二个原型上下文创建失败时，首个视口的截图保留。

Git 验证包括 CR 与非 UTF-8 文件名的身份变化、私有 JSON 往返、模型文本入口明确拒绝，以及漏掉新文件的部分提交不能完成检查点。预期树覆盖原索引的 skip-worktree / assume-unchanged 标志。诊断授权等待期间源码变化会保留证据基线，后续恢复也不能悄悄接受新基线；已提交未集成的任务保留候选定位。构建发布与回滚双重故障后，在 RUNNING、CANCELLED、SUCCEEDED 三种状态下，旧副本、工作区和临时输入均被保留；解除注入后可以核验恢复并释放 holds。快照复制这些恢复资源并仅重绑定可变定位，原库字节保持不变。

真实 pip 在无索引、无依赖下载模式下，从私有副本安装本地 wheel 到临时 target，覆盖直接 wheel、--find-links、-f 和 file URI。原目录 wheel 在安装前移除，证明未回读旧工作区。安装器恢复仍使用受控 Conda/npm 命令边界验证错误标记和旧实例保留；本轮没有重跑远端 Conda/npm 安装、收费模型或 GitHub 托管 CI，也没有声称 OCR 再扫描已零问题。需求/研究解码、权限读取失败、配置错误、秘密别名和错误收集器分类另有自动回归。

Ruff、`git diff --check`、项目 Conda 环境的 `pip check` 和 wheel 构建通过；wheel 中全部 52 个 Python 源文件与当前源码逐字节一致，包括五个新增运行模块。模型、推理设置及执行权限沿用用户配置。数据库 v5、公开视图 v4、调用身份 v4、检查器 v9、浏览器检查器 v5 的兼容行为见 [设计](design.md#2026-10-16-第三轮-ocr-修订) 和 [运行说明](operations.md#第三轮-ocr-修订后的恢复注意事项)。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/ocr3-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/ocr3-wheel-output
```

## 第二轮 OCR 修复验收（2026-10-15）

基于 `f46a690` 核验第二份报告的 52 条评论：51 条成立，第 35 条部分成立，原有调用内源码漂移防护继续保留，补足 HEAD 和跨步骤基线。逐条落点见 [第二轮实施对照](quality-first-implementation.md#第二轮-ocr-核对后实施2026-10-15)。历史验收文件未修改。

最终源码全量回归 **674 项通过，0 失败、0 跳过**（505.96 秒），新增 **101 项**行为、隔离及故障注入回归。[完整 JUnit](evidence/ocr2-fixes-suite.xml) 已单独归档。先验证受影响子系统，再执行完整套件；Ruff、已跟踪及新增文件的差异检查、项目 Conda 环境的 `pip check`、wheel 构建和 wheel 内容校验通过。

真实系统检查包含 Chromium、bubblewrap、Git 和本地 HTTP/CONNECT。沙箱中的测试进程确认无法读取控制器文件，仍可创建指向它的附件根链接；控制器拒绝归档。预览服务拒绝非 HTML 链接、目录链接及链接索引；内部正常静态资源仍可读取。网络半关闭允许响应完成，httpcore 请求和流错误保留 HTTPX 类别。使用真实 pip 离线 dry-run 检查带空格路径的本地 wheel：正确 hash 可解析，错误 hash 明确拒绝，未安装依赖。

恢复回归覆盖最后一次运行成功后回执读取、运行入口写入和完成事务失败；续跑不重复检查或消耗预算。重复失败的阻塞跨重启保留，显式增额只授予有限新尝试。新增文件修复在 staging、commit 和 ref 更新后的中断均可恢复；用户修改仍被保留。计划修订源码/HEAD 漂移、缺失命令观察、文件/目录/链接转换和三条浏览器失败恢复路径分别验证。

状态库 v4 测试从实际缺少表的旧结构开始，覆盖 v1/v2/v3、原始产物引用、DDL 与数据更新回滚、幂等迁移和预算保留。手动工具验证 0600 旁路文件、规范化后的副本定位、外部可变路径拒绝、主工作树配置间接引用、浏览器部分诊断及主异常保留。两项旧测试仅调整受信任二进制夹具位置及加强重复失败停止断言，没有放宽原有读取或门禁条件。

Provider 协议使用受控子进程夹具，安装恢复使用受控安装器边界。本轮没有运行收费模型、远端 Conda/npm 安装或 GitHub 托管 CI；本地回归结果不代表这些外部组合均已实跑。模型、推理设置及执行权限沿用用户配置。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/ocr2-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/ocr2-wheel-output
```

## OCR 核对后修复验收（2026-10-15）

基于 `bc58f801` 对本轮 OCR 评论核对后实施。修复对照及未采纳项见 [实施记录](quality-first-implementation.md#ocr-核对后实施2026-10-15)，新增恢复与网络约束见 [设计修订](design.md#2026-10-15-ocr-边界修订)。原有历史验收文件保持不变。

最终源码全量回归 **573 项通过，0 失败、0 跳过**（475.30 秒），比基线新增 **109 项**行为与故障注入用例。[完整 JUnit](evidence/ocr-fixes-suite.xml) 已归档。受影响子系统分组先行验证，最终完整套件覆盖全部修改；Ruff、`git diff --check`、项目 Conda 环境的 `pip check` 及 wheel 构建通过。Wheel 元数据明确包含新增直接依赖 httpcore、packaging，其版本沿用已安装的项目环境/开发锁。

实际运行覆盖：Chromium 对外部请求、跨来源重定向、WebSocket 和弹窗的阻断；真实 bubblewrap 中无法读取控制器文件的测试仍可制造报告链接，但控制器拒绝归档该链接；输出管道塞满且忽略 SIGTERM 的子进程可被清理；优化模式下手动脚本不能把失败结果标成成功。另用真实 pip 离线 dry-run 验证含空格路径的 editable extras，只生成临时构建元数据，未安装包。

恢复检查包含 SQLite 转移中断、完成修复的重放边界、修复后重编译构建输出、观察 ID/耗时变化不刷新自动复验额度、主目录运行预算跨重启保留、取消与完成竞态、渲染失败解锁、GC 后代保护和快照原文保全。安装器选择和锁重放使用受控命令边界故障注入，没有把这些测试表述成真实远端 Conda/npm 安装验收。

验证限制：当前宿主 DNS 将 `example.com` 解析为非公网测试网段地址 `198.18.0.122`，真实 HTTPS 抓取在连接前被安全策略拒绝。因此本轮没有取得该环境下公网抓取成功的证据。额外临时 pytest 合同检查通过，确认新传输适配器连接校验后的数字地址，同时保留原 Host、TLS SNI、主机名检查和必需证书验证；该受控网络检查不替代真实 TLS 握手。未放宽地址策略或修改宿主 DNS/代理。未重跑收费的真实模型对照，也未运行 GitHub 托管 CI；相关脚本与工作流改动不追溯改写旧证据。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-ocr-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
.conda-env/bin/python -m pip check
.conda-env/bin/python -m hatchling build -t wheel -d /tmp/aa2-ocr-wheel-output
```

## 故障生命周期、动作预算与真实 demo 续跑（2026-10-15）

本轮针对旧现场的完整恢复过程验收。控制器将证据有效性与检查成功分开，逐项关闭问题；旧计划停滞不再阻断诊断或已有任务修复，实现额度耗尽时也先诊断当前失败。上游恢复保留下游独立的待修复问题，当前检查反馈不会被旧失败覆盖。一次验证继续收集独立检查，只有依赖失败产物的消费者明确标记未执行，避免单元测试失败掩盖类型或构建错误。

最终全量回归 **464 项通过，0 失败、0 跳过**（401.44 秒），ruff 和差异检查通过。[完整 JUnit](evidence/recovery-lifecycle-suite.xml) 已归档。新增及扩展覆盖旧门禁、实现额度边界、诊断预算血缘、重复复验阻断、跨任务范围保护、部分问题关闭、过期证据、下游修复保留、版本 2→3 迁移中断、计划文档已写入后的恢复和未知用户修改保护，以及同一 argv 的不同检查契约。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-recovery-v3-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

真实模型验收复制了 `/home/fuli/projects/demo` 的数据库、Git 引用、未完成工作区、报告和配置引用，保留原数据库字节；只在隔离副本重绑定基础设施路径。使用原 run `run_f8a82b6d34f24434`、原模型与推理配置，从版本 2 的等待状态迁移续跑，最终交付到 `/tmp/aa2-recovery-v3-demo/project` 的 `main`：**SUCCEEDED / DELIVERED**，提交 `36644cb9820b45d9492a8d3cc57a60f652064c88`。[完整恢复及验收证据](evidence/recovery-lifecycle-demo.json) 已归档。

| 核验项 | 实际结果 |
|---|---|
| 原有用例保留 | 原 293 个不同用例全部保留并通过，新增 1 个焦点回归用例，去重后共 294 个通过 |
| 后端完整套件 | 189/189 通过，含原 T001 的 171 项及交付集成 18 项 |
| 前端完整套件 | 89/89 通过，保留标题、HTTP 客户端和界面原有断言 |
| 原浏览器 E2E | 16/16 通过，桌面与手机原焦点、双向切换、筛选和持久化断言保留 |
| 构建与运行 | TypeScript、生产构建、Python/Node 运行探针通过；Python 3.12.15、Node 22.21.1 |
| 实际界面 | 候选及交付后分别执行 84 次浏览器动作，无错误 |
| 交付后额外复验 | 在同一源码上重跑完整检查，包括原 16 项 E2E，全部通过且无跳过 |
| 重复续跑 | 正常 `aa2 run` 返回已完成状态，新增模型调用 0，预算、源码和回执不变 |

原始 [后端 JUnit](evidence/recovery-lifecycle-final-1.xml)、[前端 JUnit](evidence/recovery-lifecycle-final-2.xml)、[E2E JUnit](evidence/recovery-lifecycle-final-3.xml) 及 [交付后 E2E 复验](evidence/recovery-lifecycle-repeat-3.xml) 可直接核对；其余任务子集及重复报告引用包含在 JSON 中，不重复计入 294 项。原 B07 键盘用例、失败反馈、开发代理、后端交付用例及前端 API/标题测试文件均核对为未修改。B01 只将即时 `check()/uncheck()` 改为单次点击后等待原状态断言；测试修改均经独立审查。

真实试跑暴露并修复了实现预算耗尽前遗漏诊断、上游恢复覆盖下游待修复状态，以及首个失败隐藏独立检查的问题。T003 新增回归测试还经历过旧 DOM 引用、异步焦点等待及不支持的查询参数修正；用户在本会话明确批准两次单轮增额，最终 T003 累计 6 轮、T004 累计 4 轮。T001/T002 原提交与预算不变，原型批准问题及指纹不变，历史计划停滞仍为 7，计划版本未重置，也没有整轮回退。试跑包含监督暂停、控制器升级及这两次授权，不是无人干预成功率基准。

最终汇总时另发现同一 argv、不同输入或最低测试数的结果索引被混淆：原始报告均通过，但旧摘要将一个后端契约标为待核验。修复按完整 command_hash 关联证据，并防止较弱契约的成功覆盖同批较强契约的失败。交付后实际全量复验确认全部 10 条命令观察有效；在单独数据库副本回放这些真实结果，四个任务的派生证据状态均为 valid，预算不变。原始交付回执保留修复前的派生记录，未为展示结果而改写；回放结果单独记录在验收 JSON 中。

交付主目录源码及 HEAD 与回执一致，Git 干净，仅保留主工作区，无私有引用，`cleanup_pending=false`，运行环境仍有 pin。正式 demo 的源码和全部检查点未被本轮修改；只读核验发现其原终端期间选择了 n 暂停，数据库变化仅涉及运行和问题状态，资源及用量未变。后续应在正式项目重新执行 `aa2 run`，由新控制器迁移续跑。

隔离快照工具不调用模型：

```bash
.conda-env/bin/python tests/manual_recovery_snapshot.py \
  --source /path/to/paused-project --destination /tmp/new-recovery-copy/project
```

对副本运行正常 aa2 CLI 才会产生模型用量。此轮新增记录输入 15,728,244、输出 37,841、合计 15,766,085 token；缓存命中 12,035,968 单列、不重复计入合计。完整性与未报告调用数量见 JSON。

## 统一恢复与局部计划修订（2026-10-15）

本轮在已有构建契约修复上引入故障事实、独立诊断、局部 PlanPatch、任务实现/证据双状态、版本化迁移和恢复日志。恢复测试覆盖 T3 修复保留 T1/T2、T4 仅复验，耗尽实现预算后仍能纯复验，同血缘拆分任务不并发消耗同一轮数，以及任务改名、重启和计划修订均不重置预算。

故障注入覆盖快照、独立审查、日志写入、SQLite 激活及文档更新后的中断，并验证激活事务回滚、幂等续跑、过期版本拒绝、用户文档保护、保留候选及验收义务。报告准备错误改为可裁决的结构化故障；测试仍要求应用候选保留、业务轮数不增加、修复采集契约后无需再调用实现模型。旧测试中直接期待 `Replan` 的入口改为检查原始提案仍存在于 `RecoveryNeeded`，并未删除门禁断言。

补充的真实 Git 工作区测试覆盖：上游修复后保留下游未完成源码并更新依赖基线、无冲突时仅复验、冲突时保留原候选并按原范围在新基线上修复、Git 合并完成而 SQLite 尚未写入时续跑不重复合并、部分补丁恢复及未知用户修改保护。纯改名的已验收任务继承实现并保留耗尽预算；Git 基础设施错误不会被当作代码冲突派发修复。此类分支由故障注入测试验证，与下述原生模型主流程的实跑记录分别保留。

最终全量回归 **446 项通过，0 失败、0 跳过**（344.68 秒），ruff 和 `git diff --check` 通过。[完整 JUnit](evidence/recovery-suite.xml) 已归档。使用项目 Conda 环境执行：`.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-final-all-contracts-suite.xml`。真实设计门禁回退也会在事务内归档旧结构化计划、未应用提案及任务状态，并仅递增一次计划版本。

[真实旧状态迁移核验](evidence/recovery-migration.json) 使用 demo 的独立快照副本。迁移执行两次后，T001/T002/T003 已完成状态、T004 原预算和累计 7 次停滞不变；原回退提案中的 README replace 仍未应用且完整保留。最近一次实测的 4 个 Playwright 失败继续保留，没有将旧构建问题修复等同于产品通过。未修改 live demo 的数据库或源码。

真实模型试跑使用隔离项目 `/tmp/aa2-recovery-native-00w026ql/project`，沿用 demo 用户配置中的 provider/profile，不降低推理或执行权限。需求是 Python 3.12 + FastAPI/SQLite + React/Vite 的最小创建/勾选任务应用。试跑暴露了空项目被标记为 unknown、随后尝试执行未来测试的问题；控制器现在让空项目重新澄清为新需求，并在环境安装前验证覆盖检查契约。同一个 run `run_10130fdc659d490a` 经实际暂停、修复及续跑完成交付，原始失败和恢复记录均保留。

原型检查覆盖 1440/390 两种视口、14 个场景、124 次操作，错误为空；用户随后明确批准，记录绑定问题 `question_bb5df0a0929346b0` 及对应原型指纹。四项实现任务、最终独立审查和交付后运行检查均通过，最终状态为 **SUCCEEDED / DELIVERED**，交付到临时项目的 `main`，提交 `7d0a5fd651b3103ab6cae1446b2f7a54c108ac2a`。[完整原生试跑证据](evidence/recovery-native.json) 保存批准来源、恢复动作、预算、原始退出码、报告、用量及完成回执摘要。

最终候选实际执行后端 pytest 5 项、前端 Vitest 合并套件 8 项、静态资源集成 pytest 2 项和 Playwright 2 项，全部通过且无跳过；任务子集的重复复验不重复计入这些数量。[后端 JUnit](evidence/recovery-native-backend.xml)、[前端 JUnit](evidence/recovery-native-frontend.xml)、[集成 JUnit](evidence/recovery-native-integration.xml)、[Playwright JUnit](evidence/recovery-native-playwright.xml) 均已归档。生产构建、TypeScript 检查及 Python/Node 运行探针通过，实际运行环境为 Python 3.12.15、Node 22.23.2。候选和交付后的真实应用分别在 1440/390 两种视口执行 40 次操作，均无浏览器错误。

已核验完成回执摘要、主目录 HEAD/源码指纹一致且 Git 状态干净，工作区和本轮私有引用已回收，运行环境仍有 pin，`cleanup_pending=false`。试跑期间有人工监督的暂停续跑和控制器修复；这份记录证明修复后的路径完成真实交付，不代表无人干预成功率。用量账本记录至少 13,906,101 token（输入 13,599,328、输出 306,773），缓存命中 10,797,568 单列、不重复加入 total；部分被中断调用的用量不完整。

试跑的 Node 显式 Vitest 入口曾出现“进程退出 0，但控制器不识别运行器、未采集报告”。verification-v6 修复入口识别和参数注入后，在同一源码指纹上重新执行原命令，实际采集 4 个通过用例，未改断言、未增加实现轮数；见 [前后证据](evidence/node-entrypoint-recheck.json) 和 [实际 JUnit](evidence/node-entrypoint-vitest.xml)。独立诊断引用过窄的问题通过控制器证据目录及有预算的无效诊断修正解决，未知引用仍被拒绝。未开始的后继任务保持待实现，不因上游复测而提前检查不存在的代码。

前端验收后，旧私有 Git exclude 只匹配根目录的 node_modules，导致已验收工作区无法回收。现在恢复时补齐嵌套生成目录规则，保留源码未归档检查。[真实回收核验](evidence/nested-generated-cleanup.json) 确认源码指纹未变、提交已集成且工作区已回收；故障注入测试另证实未知文件和已跟踪源码修改仍会阻止回收。新 npm 锁文件的生成职责也已明确归属于真实 npm 解析，避免模型输出大段手写锁文件。

## 2026-10-15 构建依赖、产物证据与验证恢复修复

demo 的 T004 检查声明读取 `frontend/dist`，但没有构建前置；最终检查又先跑后端测试。T003 的生成产物不会随 Git 集成进入 T004 工作区，原报告因此为后端 18 项中的 8 项失败。控制器现在从已接受的运行准备解析生产者，在实际消费工作区先构建；最终验收、复用交付、重集成和主目录运行检查使用统一顺序。`Command.outputs` 不能扩大源码写权限，构建使用空输出目录，成功后才发布并记录指纹；输出丢失、篡改和失败都不能继续复用旧成功记录。另修复后端预览无法读取前端兄弟目录，以及本机地址被代理导致 Playwright 报“端口占用”的问题。

先行回归 154 项通过，随后构建、浏览器与环境相关 41 项通过。新增覆盖生产者顺序、重复声明合并、循环和冲突、源码/链接保护、失败保留旧产物、发布回滚、产物变化使缓存失效、真实沙箱中的兄弟目录预览、代理兼容、旧回退精确匹配、未应用的附带提案保留、用户改动及事务中断保护。阻塞提示优先展示当前失败用例，原始回退请求和完整提案仍保留。

最终源码完整回归 **388 项通过，0 失败、0 跳过**（344.62 秒），较上一版新增 29 项；ruff 与 `git diff --check` 通过。[完整 JUnit](evidence/build-contract-suite.xml) 已归档。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-build-contract-delivery-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

对原 T004 源码快照（`79b94fdb5899c2a57cf88a32e6d1e21742bd9c5a3bd7238916043caf6fadb370`）使用原项目 Python 3.12 环境和完整 Node 依赖，在独立副本真实执行：

| 检查 | 实际结果 |
|---|---|
| `npm run build` | 通过，生成 `frontend/dist` 并记录产物指纹 |
| 原后端交付 pytest | 18 项全部通过；原有 8 项缺少构建失败消除 |
| 原 Playwright E2E | 16 项实际执行，12 通过、4 失败，无跳过 |
| 原最终应用预览 | 2 个路由 × 2 种视口，84 次动作通过，无浏览器错误 |

剩余浏览器失败是两个视口下的删除弹窗 Shift+Tab 焦点断言，以及复选框 `check()` 的即时状态断言。既有设计要求完成切换在服务器成功后更新，需在后续修复与独立审查中核对测试等待方式；不能把这些结果写成整轮通过。浏览器用例在单独回环网络命名空间运行，以排除当前诊断环境对固定空闲端口的连接超时；应用预览使用普通沙箱和动态端口。未修改 demo 的产品源码、原断言、活动状态或审批，未调用真实模型续跑。

使用复制的真实数据库和工作区复跑恢复逻辑，确认它保存最新失败及报告，并保留 T001/T002/T003、原计划、候选源码、审批和预算历史；因完整检查仍未通过，待执行回退也保留。[恢复摘要](evidence/build-recovery.json)、[后端 JUnit](evidence/build-recovery-backend.xml) 和 [浏览器 JUnit](evidence/build-recovery-browser.xml) 已归档。成功恢复与保留失败的两条路径均由自动测试覆盖。

## 2026-10-14 原型人工审批与任务恢复修复

demo 的 `prototype-approval` 问题 `question_635a858c6e3845c7` 已记录用户选择 `1`（通过），但任务上下文未携带该审批，且旧记录使用内容指纹、新控制器使用包含执行输入身份的指纹。缓存阶段跳过迁移，生成时 `DESIGN.md` 的“待人工批准”文字又被任务模型当作当前状态，最终触发原型回退。修复统一核验真实问答、当前内容与交互动作，迁移时重采自动证据并独立审查，在任务实现、审查、最终验收、复用交付及重集成中提供审批凭据；不改写已批准的设计来伪造状态。

相关子系统 90 项测试先行通过，后补充交付修复上下文测试。新增覆盖旧/新指纹、拒绝/未回答/作废/错误版本、内容与动作变化、缺失图片、历史审批来源、缓存恢复等待、事务中断、自动检查失败、恢复后再次提出真实回退，以及已完成任务和预算保留。原有浏览器替身补全逐动作证据，没有删除或弱化验收断言。

最终源码完整回归 **359 项通过，0 失败、0 跳过**（365.74 秒），较上一版新增 31 项，ruff 与 `git diff --check` 通过。[完整 JUnit](evidence/prototype-approval-suite.xml) 已归档。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-prototype-approval-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

在 demo 原型的独立副本上，真实 Chromium 验证 5 个场景、1440/390 两种视口，99 个声明动作共执行 198 次，全部通过。另复制故障数据库及工作区，复用上述真实浏览器报告并使用受控独立审查响应，验证 T003 的旧回退可恢复，同时保留 T001/T002、完整计划、候选源码、轮数及回退历史。[隔离恢复摘要](evidence/prototype-approval-recovery.json) 已归档。未修改 live demo、代替用户批准或执行 T003 的真实模型调用；真实模型端到端结果需要用户用新进程续跑后确认。

## 2026-10-14 命令合成、报告身份与浏览器运行契约修复

以 demo 的 T002 暂停状态和原始检查命令为复现基线。直接原因是控制器向已有 `--run` 的命令再次追加单值选项，Vitest 4.0.18 在收集测试前退出。进一步执行原命令发现两个参数化用例同名，旧报告解析器又将有效的 67 用例报告判为损坏。后续真浏览器验证还确认了空临时浏览器路径，以及 WSLg 桌面会话变量干扰无头 Chromium 渲染帧的问题。

本轮将原 argv、npm script 和控制器参数共同纳入组装契约：按工具语义合成单值/多值选项，重复组装幂等，报告写入独占目录；保留文件过滤、名称表达式、npm 前后置钩子、环境和原检查义务。复杂 shell、显式 watch、禁用 JUnit 或无法安全重定向的脚本内固定 Vitest 输出路径在启动前报告准备错误，保留已应用候选和轮数。重名用例在计数一致时获得明确身份，进展仅按整组通过计算，不能靠重排或改变组大小制造改善。

最终源码完整回归 **328 项通过，0 失败、0 跳过**（314.84 秒），较上一版新增 58 项覆盖；ruff 与 `git diff --check` 通过。[完整 JUnit](evidence/command-contract-suite.xml) 已归档。受影响子系统先行测试，最后全套回归使用包含桌面环境隔离和脚本解析修复的最终源码。

真实执行证据见 [命令与恢复实跑摘要](evidence/command-contract-smoke.json)：

- 原 T002 的 `npm run test -- --run src/domain/title.test.ts src/api/tasks.test.ts` 实际采集 **67 个通过用例**；原 `npm run typecheck` 退出码 **0**。计划文件、过滤条件、expected_tests 和 inputs 保持原值。
- Vitest **3.2.4 / 4.0.18**、Playwright **1.55.0 / 1.58.2** 的 **34 种不同组合**实际通过判定，包括已有 run 参数、脚本内 run、报告选项、过滤、直接调用和 npm 调用。真实失败和全部跳过被拒绝；posttest 退出 17 即使用例通过也不能通过验收。另一次重复执行的 17 组结果保留在原记录中，不计为新增覆盖。
- 真实 T002 暂停状态的隔离副本，复用已验证运行时并执行原两条检查后恢复成功。计划与历史预算保持一致，旧回退请求归档，后续仍须独立模型审查；没有调用真实 provider 或执行发布。
- 用项目锁定的 Playwright CLI 安装本地 Chromium，再在原 bubblewrap 权限下实际访问回环 HTTP 页面、普通点击、断言和截图。默认无头 Chromium 使用项目内匹配版本的浏览器；去掉继承的桌面会话变量后渲染帧恢复。[归档截图](evidence/node-browser-smoke.png) 在临时输出目录清理后仍可读取。浏览器实跑覆盖 Chromium。

Node 就绪记录在浏览器安装成功后发布，浏览器内容纳入完整性校验和检查输入身份。旧 v2/v3 证据需重新采集；若复测仍失败，最新结果随待回退状态和规划反馈保留，避免继续依据过时诊断修正计划。

复现入口使用项目 Conda Python 及已安装的、锁定版本的 Node 依赖；脚本复制输入到自身临时项目，不安装到全局环境：

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-command-final-suite.xml
.conda-env/bin/python -m ruff check src tests
.conda-env/bin/python tests/manual_command_matrix.py CONDA_PREFIX NODE_MODULES_V3 NODE_MODULES_V4
.conda-env/bin/python tests/manual_node_browser.py CONDA_PREFIX NODE_MODULES_V4
```

## 2026-10-14 检查采集与恢复链路修复

demo 的原检查命令是 `python -B -m pytest`，旧检查器只识别紧邻解释器的 `-m pytest`，因此漏采 JUnit；随后又把用例数不足写入 `exit_code`，丢失原始进程状态。现按 Python 选项语法识别模块，记录 `process_exit_code`、验收 `exit_code` 与明确失败原因；新增 Vitest/Playwright 的 JUnit 采集，校验报告完整性并归档 Playwright 输出文件。真实前端执行还暴露并修复了 Vite `.vite-temp` 写入范围和 Playwright 删除输出挂载点的问题，预览与检查共用 Node 缓存目录声明。

自动测试 **270 项通过，0 失败、0 跳过**（205.59 秒），ruff 与 `git diff --check` 通过。新增 42 项覆盖解释器选项、npm 脚本、实际沙箱中的命令执行、原始退出码、缺失/损坏/空/全部跳过报告、计数与身份冲突、缓存、附件、旧证据失效，以及重采事务中断、用户改动和无关回退保护。原验收断言保留，并补充旧检查器与输入变化不得计算进展的断言。

使用 demo 候选的隔离副本、原 Python 3.12 环境及原验收命令，实际采集 **171 个通过用例、有效 JUnit、进程退出码 0、验收退出码 0**。Vitest 3.2.4 / Vite 7.3.7 和 Playwright 1.55.0 通过真实 npm 安装及 bubblewrap 验证：通过、真实失败、全部跳过共六种情形均正确判定；Playwright 文件附件在临时目录清理后仍可从归档读取。[实跑摘要](evidence/test-report-smoke.json) 保留版本、命令、结果与报告引用。

旧现场恢复先核对候选与待回退响应，重新执行全部任务检查；全部通过才以事务替换反馈并归档对应回退请求。原回退理由继续交给模型复核，独立审查照常执行，轮数与停滞历史保留。本次未运行 demo 的真实模型续跑或完成其整轮交付；原项目状态保持在用户控制的暂停/等待边界。

```bash
.conda-env/bin/python -m pytest -q
.conda-env/bin/python -m ruff check src tests
.conda-env/bin/python tests/manual_test_reports.py /path/to/project-conda-prefix-with-node
```

## 2026-10-14 清单路径契约修复

修复规划字段缺少路径说明与预检，导致文件正文被当作文件名、直到开发完成才出现 `Invalid environment manifest` 的问题。schema 和规划指令明确区分路径与正文；规划校验失败进入规划修正，环境准备再次校验并报告具体字段，错误不回显输入正文。允许规划声明尚未生成的清单文件，仍在实际准备时核验存在性和边界。

新增 9 项回归覆盖正文误填、绝对路径与越界路径、尚待实现的相对路径、安装前拒绝错误选择器、规划自动修正，以及修复保存的选择器后从已应用任务继续而不重复实现或增加轮数。先运行受影响子系统，再执行完整套件：**213 项通过，0 失败、0 跳过**（157.69 秒）；ruff 和 `git diff --check` 通过。[JUnit 报告](evidence/manifest-reference-suite.xml) 已归档。

用户暂停 demo 后，在控制器锁内备份数据库与原计划，仅修正已保存计划的两个选择字段。只读校验确认 Conda 依赖和渠道一致、实际固定版本满足原规划约束且与后端元数据一致；其他检查点、问题记录、运行状态和历史计数保持一致。未改写依赖文件或自动批准任务，实际安装与验收仍由续跑执行。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-manifest-reference-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

## 2026-10-14 规划来源审查修复

修复首次规划已登记任务来源、但独立审查仍收到登记前空映射的问题。规划与审查现在同时收到当前来源映射和已有任务预算；审查另收到控制器本次来源校验结果。历史错误反馈不会代替当前状态，来源校验通过也不代替独立质量审查。后续规划携带完整开放问题及证据入口，拒绝提示保留缺陷说明、证据和修复要求。已有预算而来源注册表缺失或不完整时仍阻塞，累计预算不重置。

新增 4 项回归覆盖旧反馈与首次多任务登记、拒绝后再次规划的完整问题传递与预算保留、来源注册表缺失及部分缺失。相关规划与回退测试先通过，再执行完整套件：**204 项通过，0 失败、0 跳过**（180.13 秒）；ruff 和 `git diff --check` 通过。[JUnit 报告](evidence/plan-lineage-suite.xml) 已归档。未修改用户 demo 的数据库，也未代替用户确认续跑；此处验证的是控制器材料和门禁行为，尚未重跑该 demo 的真实模型审查。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-plan-lineage-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

## 2026-10-14 质量优先修复验证

本轮最终代码在项目 Conda 环境运行完整套件：**200 项通过，0 失败、0 跳过**（145.15 秒）。相对基线新增 56 项行为与故障注入用例，原有测试没有删除；少量旧断言更新为新契约，例如 NO_CHANGES 必须重新核验当前环境、环境夹具必须提供完整性证明。

```bash
.conda-env/bin/python -m pytest -q --junitxml=/tmp/aa2-final-suite.xml
.conda-env/bin/python -m ruff check src tests
git diff --check
```

ruff 与差异格式检查通过。完整 [JUnit 报告](evidence/quality-first-suite.xml) 已归档。新增回归覆盖路径和权限、CRLF/混合换行、敏感输入和实际 bwrap 隔离、依赖聚合与环境损坏、固定义务兼容、运行变量失效、日志脱敏、产物引用、材料原生协议读取、原型与交付恢复，以及不接受 stdout 自报测试数。

另完成一次真实环境冒烟，并在新控制器进程复核：在 `/tmp` 独立项目创建 Conda 环境，使用实际安装器安装本地 wheel，检查就绪实例可复用，再由该环境的 `python -m pytest` 通过 1 项包行为测试；未向系统 Python 或 Conda base 安装包。[真实环境报告及日志/JUnit 引用](evidence/quality-first-smoke.json) 已归档，包含实际 argv、安装身份和运行配置身份。该冒烟不计入上面的 200 项，也不代表全部 Conda/Node 平台组合已验收。

正文去重与诊断生命周期另覆盖精确重建、版本隔离、全部声明保留、暂停/等待/开放问题保护、安装后置条件失败、临时采集文件清理、读者占用、跨运行共享、删除失败恢复、回收期间新消费者、旧嵌套引用、主动释放与意外丢失区分、模型材料的释放标记，以及正式报告缺失时拒绝复用。针对性测试先通过，再执行上述完整套件。

真实模型正文呈现对照使用已有用户原生 `balanced` profile：Codex CLI 0.162.1、`gpt-6.1-sol`、`medium`，固定两份虚构 API 版本快照，各有 4 项声明。A 使用测试脚本内的旧正文重复格式，B 使用当前请求内去重格式；角色指令和 schema 相同，每次独立会话，按 A/B、B/A、A/B 交错运行。六次调用均满足固定 oracle：全部 8 个声明 ID 恰好出现一次，支持/不支持判断全部正确，包括文末权限条件、版本冲突及不能凭文档证明实测成功。没有自动升级模型或降低推理设置。

| 指标 | A：旧格式 | B：请求内正文去重 |
|---|---:|---:|
| 单次编译 prompt 字符数（不含 schema） | 38,752 | 17,178 |
| 三次原生输入 token 合计 | 64,692 | 54,894 |
| 三次原生输入与输出 token 合计 | 65,743 | 55,809 |
| 固定判断检查通过 | 3/3 | 3/3 |

该样例 prompt 字符数减少约 55.7%，输入 token 减少约 15.1%；六次调用合计 121,552 token，用量完整，缓存计数为 0。[完整对照结果](evidence/research-presentation-comparison.json) 包含每次回答、实际用量及身份摘要。成功后已清理临时会话与调用目录。测试脚本 [manual_research_presentation.py](../tests/manual_research_presentation.py) 独立于业务路径；重跑需指定已配置用户 profile 的专用测试工程，会产生真实模型用量：

```bash
.conda-env/bin/python tests/manual_research_presentation.py \
  --project /path/to/configured-evaluation-project \
  --output /tmp/aa2-research-presentation-comparison.json
```

这只验证固定样例中的内联正文呈现，不是完整业务任务质量等价测试，不验证其他模型、全部上下文长度或附件读取效果。原生附件链路另由子进程夹具及真实 bwrap 验证；可还原性、字符数下降和读取探针都不能代替质量门禁。目录分页、历史摘要等已移出当前范围，并非等待开启的另一套压缩逻辑。见 [实施与审核项对照](quality-first-implementation.md)。

以下章节保留历史验证记录，不与本轮计数混算。


本轮验证日期：2026-10-13；下方真实 provider 记录来自 2026-10-12。环境：WSL2 / Linux、项目内 Conda、Python 3.11.15。模拟 provider 用于检验编排语义，真实 CLI 与浏览器验证另外执行。

本轮自动测试共 64 项通过，新增自动交付与故障注入覆盖。原有 36 项覆盖包含真实 Git、子进程、bubblewrap 权限探针和 Chromium。覆盖迭代准入、同文/同义需求去重、部分差异规划、实现/审查恢复、部分修改幂等应用、测试弱化拦截、原型版本审批、跨终端回复、并发 provider 切换、持久重试预算、任务改名计数与回退工作区回收、token 缺失与去重、依赖指纹共享/隔离、超时进程回收、临时资源与恢复资源区分，以及完成后清理中断的恢复。

`ruff check src tests` 通过；项目内 editable 安装、wheel 构建及 `pip check` 通过。真实前端复跑的摘要保存在 [frontend-smoke.json](evidence/frontend-smoke.json)。

## 自动合并与主目录交接验证（2026-10-13）

覆盖新空仓库与 main/master/自定义分支、歧义选择、worktree 占用、未暂存/暂存/未跟踪修改保护、被忽略文件冲突、已跟踪 .env 保留、发布日志各边界中断、部分 Git 更新恢复、未知索引和锁保护、冲突修复与独立复审、跨重启重集成预算、发布后暂停和用户新提交保护、运行检查失败后的隔离修复、回收失败重试、运行环境 pin、src 布局与声明 cwd 的实际 shell 启动、产物引用和用量账本保留，以及 CLI 忙碌时的新迭代准入。

真实 Conda/npm/浏览器交接实跑通过，未调用模型：

| 目标 | 实际核验 |
|---|---|
| Python | 项目内 Conda、隔离 pytest、主目录 import 探针、删除 worktree 后继续执行 |
| React | 隔离生产构建、主目录 frozen npm ci 与重新构建、桌面/手机交互断言、GC 后依赖仍可用 |
| Next.js | 隔离类型生成与构建、主目录 frozen npm ci 与生产构建、生产服务桌面/手机交互断言、GC 后依赖仍可用 |

三个目标均核验主目录源码与 HEAD 一致、无残留私有 Git 引用、无开发 worktree，且当前运行环境在零缓存 TTL 的 GC 下仍保留。测试结束后脚本显式解除自己临时工程的环境引用以释放磁盘；这不属于生产交付回收行为。摘要及实际命令输出见 [delivery-smoke.json](evidence/delivery-smoke.json)。

```bash
AA2_BROWSER_CACHE="$PWD/.cache/browsers" .conda-env/bin/python tests/manual_delivery.py
```

这组早期交付检查使用可控 provider 验证门禁语义，并用真实 Git、依赖安装、构建和浏览器验证交接。后续完整原生模型交付已补充在本文开头的统一恢复验证记录中；两组记录的执行范围分别保留。

## 真实集成验证

| 路径 | 实际结果 |
|---|---|
| Codex CLI 0.162.1 | 结构化调用、原生会话恢复、完整 Python 样例交付通过；生成的五个 pytest 用例通过，交付到本地分支 |
| Antigravity CLI 1.3.1 | 结构化输出、标准输入流、实际工具读取工作区文件与截图识别通过 |
| React 19.3.0 / Vite 8.3.4 / TypeScript 5.9.3 | 项目内 npm 安装、生产构建、1440/390 两种视口的点击及文本断言通过 |
| Next.js 16.4.0 App Router / TypeScript | 受控类型生成、生产构建、生产服务器及两种视口的交互断言通过 |
| 目标环境 | Node 22.23.2；相同依赖复用环境；Python editable 安装绑定环境内私有源码副本，原工作区没有生成 egg-info |
| 设计目录 | 通过 Git 固定提交 `13be5c05c63be24b57581162364167028020f043`，列出 74 个候选并实际获取 DESIGN.md 文本 |

完整 Codex 样例 run 为 `run_3631541c759743e8`，结果 `DELIVERED`，包含中断恢复和模型容量错误后的人工重试。开发联调中修复过规划审查角色与原生认证目录问题，因此这次记录不是一次零干预成功率基准。用量账本记录至少 814,042 token，部分失败调用没有完整用量，标记 `complete=false`；缓存命中 377,472 单列，不再加到 total。

前端框架验证采用固定测试工程；原型人工门禁与迭代范围由自动测试覆盖。尚未进行大型真实业务项目的长期稳定性评估。Antigravity 目前验证到调用契约、工具读取与截图审查，完整业务迭代尚待更多实跑。

执行命令：

```bash
python -m pytest -q
python -m ruff check src tests
aa2 doctor --project /path/to/scratch-project
aa2 doctor --project /path/to/scratch-project --live
```

实际 npm/Conda/浏览器集成测试可单独运行，需联网下载依赖，不调用模型：

```bash
AA2_BROWSER_CACHE="$PWD/.cache/browsers" python tests/manual_frontend.py
```

该脚本创建自己的临时项目，保存源码、锁文件、检查结果与截图，结束后回收依赖和构建目录。无 Chromium 或系统沙箱条件时，相应自动测试会明确 skip；上面的本机记录中这些检查实际执行。
