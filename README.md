# auto-agents-2 (aa2)

可恢复的软件开发编排 CLI。使用 Codex CLI 或 Antigravity CLI 完成需求澄清、外部研究、原型、设计、任务规划、实现、独立审查、验证和本地 Git 交付。

## 安装

支持 Linux / WSL2、Python 3.11+、Git 和 Conda。使用独立环境，避免系统 Python、Conda base 和全局 npm 安装。

```bash
CONDA_PKGS_DIRS="$PWD/.cache/conda/pkgs" conda env create --prefix ./.conda-env --file environment.yml
conda activate ./.conda-env
python -m pip install --cache-dir ./.cache/pip -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation --cache-dir ./.cache/pip -e .
```

也可在已有 `autoagents` 环境安装。首次安装浏览器时，将 `PLAYWRIGHT_BROWSERS_PATH` 指向目标项目内的浏览器缓存目录；不要使用安装系统包的 `--with-deps`。

## 使用

```bash
aa2 init --project ./demo
aa2 doctor --project ./demo
aa2 run --project ./demo --requirements ./requirements.md

# 中断后继续：不要再次传入 requirements
aa2 run --project ./demo
aa2 status --project ./demo
aa2 logs --project ./demo
```

已有同格式 provider 配置时，可使用 `aa2 init --project ./demo --from-config /path/to/config.json` 导入实例与 effort 映射；原配置不会被修改。

`--requirements` 明确表示新的迭代。上一轮未完成时立即返回 `ITERATION_INCOMPLETE`，不读取新需求或切换 provider。没有 `--requirements` 时恢复已有迭代；如果命令启动时上一轮已经成功完成，交互终端会在显示运行指引后询问是否开始新迭代。没有 `--new` 或待启动队列。

已完成项目可直接执行 `aa2 run --project ./demo`，回答 `y` 后描述新需求，无需先创建需求文件。支持多行编辑，回车换行、`Ctrl+D` 提交；空输入不提交，需求上限为 4 MiB。aa2 自动保存原始需求，再通过现有多轮澄清确定规格并进入开发。回答 `n` 或在提交前按 `Ctrl+C` 会退出，不创建新迭代；提交后的暂停与续跑沿用现有机制。一次命令最多以此方式新建一轮，新一轮完成后正常退出。

启动时尚未完成的迭代继续续跑，即使本次完成也不询问新建。显式 `--requirements`、`aa2 resume`、无历史迭代、已取消迭代及非交互终端保持原有行为。

原始文档可以很粗略。aa2 保存原始快照，对话澄清后保存用于设计与开发的规格。接入评估会对照已有规格、源码和证据；全部已满足时返回 `NO_CHANGES`，部分满足时只规划差异。文字相同本身不构成“已实现”的证据。

各阶段只提供当前任务需要的正文、源码和证据，历史记录及大材料通过明确引用按需读取。研究优先提供 URL 锚点对应章节及必要的短链接定义，完整网页仍保留为证据。短必要文本直接内联，避免无必要的补读；适用的共享约束、审批和验收条件始终保留。首包字符数与真实模型用量分别记录，减少字符不代表 token 一定同比下降。相关验证见 [输入选择验收](docs/validation.md#全阶段模型输入选择验收)。

恢复请求的完整引用目录在校验时生成，不重复塞入候选动作和历史许可。旧的大目录、逐用例进度明细和历史观察按需读取，当前故障、审查缺项、允许范围与验收要求完整保留。升级保留原始材料、已完成决策和累计次数；若旧决策已经转人工，续跑仍需回答原有恢复确认，输入优化不会替用户批准。

外部研究支持控制器进程环境中的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`，兼容小写变量（小写优先）；代理地址须使用 HTTP(S)，并支持向目标端口建立 CONNECT 隧道。研究资料域名必须解析为真实公网 IP，代理连接固定到已验证的 IP。使用 Clash/Mihomo 的 Fake-IP DNS 时，请改用 `redir-host` 或对资料域名设置 Fake-IP 排除规则。出现 `Research destination must resolve exclusively to public addresses` 时，检查运行 aa2 的环境中的 DNS 结果；仅恢复浏览器上网或反复输入 `y` 不会解除地址限制。

代理变量必须存在于启动 aa2 的终端环境中；其它终端、工具进程或 provider 的环境配置不能替代控制器环境。可使用 `conda env config vars set --prefix ./.conda-env` 将代理变量保存在项目 Conda 环境，随后重新激活环境。修改变量或升级 aa2 后，先输入 `n` 暂停，再用不带 `--requirements` 的 `aa2 run` 续跑。下载超过总时限时，错误包含操作、域名、时限和代理环境检查提示；其它无文字异常至少显示异常类型。

模型补丁中的替换文本匹配不到、为空或出现多次时，aa2 会向模型提供失败位置和当前公开文件，最多自动纠正两次，并重新校验整批补丁。原型、文档、任务实现和交付修复均使用此流程；纠错不修改文件范围、验收条件或审批，也不放宽精确匹配。原候选和纠错响应保留，重启继续已有记录。一次纠错耗尽后，任务实现会把保留的失败候选交给恢复决策，决定是否生成新的候选；同一候选的纠错次数不会因重试清零。文件不可读取、provider 无法使用或源码在纠错期间改变时保留现场并等待核对。正在等待旧匹配错误的进程需先输入 `n`，再重新运行 aa2 加载新代码，保留本轮进度。

每次迭代以目标分支的已提交版本为基线。验收通过后自动快进合并到目标分支，并更新项目主目录、准备运行依赖。新空仓库使用 `main`；已有仓库优先采用本地存在的远端默认分支，其次唯一的 `main`/`master` 或唯一分支；有歧义时询问一次并保存选择。

无需切换到 run 分支。交付成功后：

```bash
cd demo
source .aa2/runtime.sh
# 按生成项目的 README 执行启动命令
```

配置可显式指定目标和主分支并发变化的重集成上限：

```json
"delivery": {"target_branch": "main", "max_reintegrations": 2, "max_runtime_retries": 3}
```

工作目录有未提交或未跟踪修改时会等待，请自行提交或移到其它目录后继续。aa2 不自动暂存、重置或包含这些修改。成功后回收内部工作区和 Git 引用，不保留 `aa2/<run-id>` 分支；不会推送远端。当前运行环境、正式规格和验收证据保留。

若源码已合并但环境准备中断，`aa2 run --project ./demo` 继续完成交接。此时 `aa2 cancel` 表示暂停收尾，不回滚主分支。`status` 可查看目标分支、提交、交付阶段和 `cleanup_pending`；清理失败不会撤销成功状态，下次 run 或 gc 会重试清理。

## Provider 配置

编辑目标项目的 `.aa2/config.json`。`providers` 是允许切换的实例集合；`active_provider` 是当前实例。相同 kind 可有多个独立实例。

```json
{
  "active_provider": "codex",
  "efforts": {
    "clarify": "deep", "provider_research": "deep", "prototype": "max",
    "design": "deep", "plan": "max", "implement": "deep",
    "review": "balanced", "verify": "balanced", "readme": "balanced",
    "recovery_diagnosis": "max", "decision": "deep"
  },
  "providers": {
    "codex": {
      "kind": "codex",
      "binary": "codex",
      "environment": {
        "CODEX_HOME": "~/.codex",
        "CODEX_API_KEY": null,
        "OPENAI_API_KEY": null
      },
      "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
      "extra_args": []
    },
    "antigravity": {
      "kind": "antigravity",
      "binary": "agy",
      "profile_map": {
        "balanced": {"model": "YOUR_MODEL", "effort": "medium"},
        "deep": {"file": "profiles/agy-deep.json"},
        "max": {"model": "YOUR_MODEL", "effort": "high"}
      },
      "extra_args": []
    }
  }
}
```

Codex 字符串 profile 使用 `$CODEX_HOME/<name>.config.toml`。其他 provider 可以使用内联 profile 或相对配置目录的 JSON 文件。level 是任意配置名称，不会被 aa2 擅自转换成某个模型。

故障独立诊断使用 `efforts.recovery_diagnosis`，默认映射到 `max`；决策代理使用 `efforts.decision`，默认映射到 `deep`。具体模型和实际思考深度以对应用户 profile 为准。普通审查仍用 `review`。缺少所选 profile 时报告配置错误，不回退到普通审查深度。

`environment` 中 `null` 移除继承变量，空字符串保留。`extra_args` 是 argv 数组，支持模型、effort 和兼容的模型配置覆盖；输出协议和权限参数受保护。

```bash
aa2 run --project ./demo --provider antigravity
aa2 provider use codex --project ./demo
```

显式切换和成功故障切换会保存 active provider。模型从配置获得，不自动升级。审查默认使用当前 provider 的新会话。原生 provider 的配置及认证输入在受控会话目录中使用，不改写全局模型设置。

填写明确的 `model` 可启用对应模型家族的 prompt 适配；模型留空时遵从 CLI 默认，使用通用 prompt。两种 CLI 都从标准输入接收需求，避免较长文档超过进程参数长度限制。

## 自动恢复与停止

新项目及旧项目续跑默认启用自动决策，复用当前 provider，无需新增 API 账号。`.aa2/config.json` 可保留以下设置：

```json
{
  "automation": {
    "enabled": true
  }
}
```

每次失败都进入恢复评估。控制器先处理规则明确的首次补采、构建复验及已校验的局部恢复；需要权衡或重复尝试时，由决策代理根据故障、历次方法及结果、业务与工程进展和累计消耗选择下一步。可选动作包括诊断、复验、范围内修复和局部计划修订。每份授权只覆盖具体动作及必要验证；结果出来后重新评估。

生成及验收阶段不再按累计次数强制停止。`execution.max_rounds`、`execution.max_diagnosis_rounds`、`execution.max_stalled_rounds`、`execution.max_stalled_replans`，以及 `automation.max_implementation_rounds`、`automation.max_diagnosis_rounds` 兼容读取并保留原值，但不再作为这些阶段的停止条件，配置中显式填写的数值也一样。第 21 轮仍可由决策系统批准；没有进展也不直接转人工。累计次数与 token 持续记录，不保证在固定消耗内结束。发布后恢复保留自己的日志、预算和人工边界。

任务阶段遵循共同合同：必要材料与真实授权 → 只读生成候选 → 适用的受保护测试/配置独立审查 → 应用 → 环境准备 → 执行检查 → 实现验收。自然语言计划不能把候选审查结果提前到候选生成之前，也不能新增控制器没有的回执门禁。必要历史材料缺失仍须解决；未知阻塞先诊断。已批准条款若确有时序错误，可通过 `timing_corrections` 正式局部修订：精确对应旧条款，结构化检查、命令、用例下限、范围及最终合同保持不变，独立审查逐项判断实质义务和用户要求的顺序是否保留。语义判断可能出错，不是控制器对任意文字等价的证明；实际候选和最终验收仍须完成原有检查。

同一故障、源码和实际材料下，两次通过协议校验但没有形成可执行结论的诊断，或两次被拒的时序修订，会排除对应自动动作。决策仍可选择其他有效动作；若无法选择则保存原因并暂停，不生成一个可批准无限重复的通用确认。改写理由、调用次数和重启不刷新这条路径；真实依据变化才可重新评估。已有许可和人工问题保持原义，关闭自动决策时的真实人工授权边界不受替代。诊断格式、引用或范围校验失败继续使用原来的纠正和恢复授权流程。这是针对相同依据的重复路径限制，不是恢复旧的累计轮次上限。

业务进展比较固定验收项的历史最好成绩；工程进展分别记录候选补丁应用、环境准备、构建产物及检查证据是否有效。同一里程碑不重复记功。诊断完成、改换措辞、增加简单测试、退步后恢复都不能伪造业务进展；工程进展也不能代替测试、类型检查和独立审查。环境或补丁阶段的失败不再算作缺少检查报告。

决策可以批准相同输入的新一次复验，但必须形成新的许可；重启同一动作复用已有许可及结果。自动授权标记为 `rule` 或 `agent`，不伪造人工回答。`aa2 status` 显示当前策略、旧次数配置不生效的标记、恢复轮、动作许可、历史计数及工程进展。

生产检查通过但原型图片仍有待补证事项时，恢复决策可选择 `prototype_recheck`。控制器重采已批准原型的浏览器证据并独立复核，再验收保留的任务候选；内容与动作不变时沿用原始人工批准。截图保持原视口，页面较长时附加带滚动位置的完整截图。补证失败仍保留现场，不凭生产测试数量关闭原型问题。

需要业务取舍、原型人工审核、凭据、资源或权限变化、发布后的例外处理，或决策无法给出有效下一步时转人工。决策请求最多一次响应加一次格式修正，不递归授权自身重试。新的恢复确认展示任务及路径范围，一次 `y` 可覆盖所展示的一轮诊断、限定修复或局部修订及验证；旧版已回答的 `y` 仍只批准原动作。

`automation.enabled=false` 关闭新的模型决策，需要判断的恢复转人工，确定性规则仍可执行。升级到状态版本 7 时先备份数据库，保留历史计数、候选、人工记录和有效结果；旧程序拒绝处理新状态版本。旧的待答停滞问题在新的有效授权提交后失效。显式暂停需要用户续跑；运行中的进程不会热加载代码，请先暂停，再执行不带 `--requirements` 的 `aa2 run --project ...`。

## 对话与审批

选择题只接受提示中的 `y/n` 或数字，按回车确认；空输入和非法选项不会推进流程，批准没有默认选项。长文本按回车换行、`Ctrl+D` 提交，支持多行粘贴、方向键移动、Home/End、Backspace/Delete 和跨行删除。输入期间动态日志暂停刷新。

非交互运行保持等待，可在另一终端操作（选项以当前问题为准）：

```bash
aa2 status --project ./demo
aa2 respond --project ./demo --answer '补充的业务规则'
aa2 respond --project ./demo --answer-file ./feedback.txt
aa2 respond --project ./demo --request QUESTION_ID --artifact ARTIFACT_HASH --answer 1
```

原型文件生成并通过浏览器检查与独立审查后，才请求人工审核，并提供各 HTML 页面的本地预览地址。输入 `1` 通过，输入 `2` 后另行填写修改意见；拒绝当前原型会退回修改，不会取消整个迭代。等待期间预览保持可用。审批绑定内容指纹；外部回复必须携带当前 `--artifact`，修改原型后必须重新批准。多个待处理问题时需要指定 `--request`。`aa2 status` 显示待回答问题、选项和审批版本。

架构评审原始决定和历史测试差异属于控制器证据。设计阶段会先读取匹配的评审记录、基线和恢复快照；没有现成决定时由控制器执行独立评审。无需用户手填这些内部证明，实际产品选择仍会正常提问。

任务审查要求先取得生产预览证据时，恢复可选择 `preview_recheck`：读取归档的原始动作合同并独立比较，核验构建后执行完整隔离预览和清理，再审查原任务。通过的单元测试、端到端测试或原型批准不能代替这份证据。升级后续跑会为依据未变且尚未回答的恢复请求补充此动作，已有回答和累计次数保留。

按一次 `Ctrl+C` 暂停，等待保存提示并返回命令行，然后使用 `aa2 run --project ./demo` 续跑，不再传入 `--requirements`。恢复保留已保存候选、检查和审核版本，重新启动预览；模型请求可能重发，未提交的输入不会保存。旧版提前出现的原型审批问题保留为历史，恢复后先准备可查看的原型。

## 环境、验证和权限

- Git worktree 隔离任务代码；相同依赖配置共享项目内只读 Conda 环境。
- agent 使用只读沙箱检查代码，以结构化候选变更返回实现；控制器验证路径、保存变更日志后应用。
- 修改已有测试和验证配置需要独立审查，不由实现者自行改变通过条件。
- 安装、构建与测试在明确的运行环境中执行。缺少系统库时应解决环境问题，不自动修改系统包。
- 必须实际执行检查；provider 的成功退出或通过声明不能代替验证证据。
- 本地虚拟环境不等同于完整机器安全边界。bubblewrap 的实际权限探针必须通过。

pytest 验证保存控制器生成的 JUnit 报告与用例清单；只跳过测试、零测试、超时均不能通过。浏览器验证覆盖桌面和手机视口，并执行规划中的交互断言。原型仅使用本地或内联资源；正式页面会与审批过的原型一起交给独立审查。

首期 Node 安装器支持 npm 与 `package-lock.json`。已有 pnpm/yarn 项目会明确提示需要相应安装器，保留其包管理器。Next.js 15.5+ 的类型声明在受控准备步骤生成，业务源码和测试在正式验证期间保持只读。Python 的本地/editable 依赖使用独立环境与源码副本，通过 requirements 文件声明，不在共享环境中绑定任务工作区。

默认两个并行开发任务。实现、诊断和实际计划修订次数按原始任务血缘累计，任务改名、拆分或切换 provider 不清零。次数和无进展历史用于选择恢复动作，不再直接触发人工确认。完整规则见上方“自动恢复与停止”。

进展依据实际测试用例、浏览器断言和控制器契约检查。累计 token 只计量；旧 `execution.max_tokens` 字段可读但不生效。基础设施单次重试结束后，环境准备可交给恢复决策判断是否再执行一次，原有重试历史保留。任何重试许可都不扩大环境或源码写入权限。

模型调用不设总时长限制。连接或进程明确失败仍及时处理，基础设施重试仍有上限。测试、构建、环境安装等命令保留各自超时；人工等待不属于模型调用。长时间没有输出仅提示状态待确认，用户可通过 `Ctrl+C` 暂停。

## 自动清理和统计

ResourceManager 默认自动回收，`aa2 gc` 只是在同样保护规则下立即触发一次检查：

```bash
aa2 gc --project ./demo --dry-run
aa2 gc --project ./demo
```

只清理登记的 aa2 资源。运行中的文件、恢复数据、审批和正式证据不会作为临时缓存删除。默认每 60 秒检查，缓存上限 10 GiB，闲置期限七天，最低可用磁盘 5 GiB。

当前交付环境具有独立引用，不受缓存过期、磁盘压力或运行引用释放影响。下一次成功交付后，未再使用的旧环境才按缓存策略回收。

测试报告归档后立即清理临时目录；任务集成并保存 Git 恢复引用后即可删除其 worktree。环境按依赖指纹复用，解析结果保存在 `.aa2/environment-records`，交付时将锁定清单导出到 `docs/aa2/environments`。

诊断日志的临时副本归档后即清理，归档正文失去最后消费者后自动回收。活动调用、未完成阶段、开放问题和恢复必需日志继续保留；正式测试报告及结构化验收证据保留。已回收日志明确标记为不可用，需要重新诊断时重跑检查，不以文件缺失推定通过。

日志显示时间戳、阶段、provider、本次调用的模型和推理深度（包含 `extra_args` 覆盖）、真实任务计数和耗时。模型或推理深度未明确配置时显示“CLI 默认（未知）”。结束时汇总整个 run 的 token，包括失败、审查和重试；未收到完整原生用量时明确标记不完整。

任务执行前显示总任务数和已验收数量；派发时显示“正在执行第 N/M 个任务”、任务 ID 和标题，并行任务分别列出。重试和续跑保留当前计划中的序号。成功结束后显示项目目录、`source .aa2/runtime.sh` 和 README 路径；有应用预览契约时同时给出预览启动命令和访问地址。预览可能使用临时数据，日常运行及数据保存方式请按目标项目 README 的说明。再次执行已完成项目的 `aa2 run --project ...` 也会显示运行指引。

每个步骤独立留下一条完成、失败或暂停记录及最终耗时；活动步骤原行更新运行时间、真实进度及“最近输出 N 秒前”，并行任务分别显示。未收到输出时明确显示“尚未收到输出”。界面刷新不算 provider 输出，输出也不等于验收进展。超过五分钟没有输出时在当前行提示“长时间无输出，状态待确认”，不会因此终止调用。文件日志只追加步骤和状态变化，不追加周期心跳。诊断输出仅记录安全分类，不打印原生配置或认证内容。

## 开发验证

```bash
conda run --no-capture-output -p ./.conda-env python -m pytest -q
conda run --no-capture-output -p ./.conda-env python -m ruff check src tests
```

`aa2 doctor --live` 会调用真实 provider，产生实际用量；普通 doctor 只检查配置与沙箱。自动测试使用可控 provider 和真实 Git / 子进程，避免依赖模型输出偶然性。

运行真实浏览器测试前，可将 Chromium 安装到本项目目录：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/browsers" python -m playwright install chromium
python -m pytest -q
```

详见 [设计文档](docs/design.md)、[运维与故障恢复](docs/operations.md) 和 [验证记录](docs/validation.md)。
