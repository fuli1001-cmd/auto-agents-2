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

`--requirements` 明确表示新的迭代。上一轮未完成时立即返回 `ITERATION_INCOMPLETE`，不读取新需求或切换 provider。没有 `--requirements` 时恢复已有迭代。没有 `--new` 或待启动队列。

原始文档可以很粗略。aa2 保存原始快照，对话澄清后保存用于设计与开发的规格。接入评估会对照已有规格、源码和证据；全部已满足时返回 `NO_CHANGES`，部分满足时只规划差异。文字相同本身不构成“已实现”的证据。

外部研究支持控制器进程环境中的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`，兼容小写变量（小写优先）；代理地址须使用 HTTP(S)，并支持向目标端口建立 CONNECT 隧道。研究资料域名必须解析为真实公网 IP，代理连接固定到已验证的 IP。使用 Clash/Mihomo 的 Fake-IP DNS 时，请改用 `redir-host` 或对资料域名设置 Fake-IP 排除规则。出现 `Research destination must resolve exclusively to public addresses` 时，检查运行 aa2 的环境中的 DNS 结果；仅恢复浏览器上网或反复输入 `y` 不会解除地址限制。

代理变量必须存在于启动 aa2 的终端环境中；其它终端、工具进程或 provider 的环境配置不能替代控制器环境。可使用 `conda env config vars set --prefix ./.conda-env` 将代理变量保存在项目 Conda 环境，随后重新激活环境。修改变量或升级 aa2 后，先输入 `n` 暂停，再用不带 `--requirements` 的 `aa2 run` 续跑。下载超过总时限时，错误包含操作、域名、时限和代理环境检查提示；其它无文字异常至少显示异常类型。

模型补丁中的替换文本匹配不到、为空或出现多次时，aa2 会向模型提供失败位置和当前公开文件，最多自动纠正两次，并重新校验整批补丁。原型、文档、任务实现和交付修复均使用此流程；纠错不修改文件范围、验收条件或审批，也不放宽精确匹配。原候选和纠错响应保留，重启继续已有记录。超过次数、文件不可读取或源码在纠错期间改变时明确暂停；重复输入 `y` 不会重置纠错额度。正在等待旧匹配错误的进程需先输入 `n`，再重新运行 aa2 加载新代码，保留本轮进度。

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

新项目及旧项目续跑默认启用自动决策，复用当前 provider，无需新增 API 账号。以下字段可加入 `.aa2/config.json`：

```json
{
  "automation": {
    "enabled": true,
    "max_implementation_rounds": 12,
    "max_diagnosis_rounds": 4
  }
}
```

每个任务按原始任务血缘累计：`execution.max_rounds` 默认 4，是触发诊断及追加授权的阈值；自动授权每次只增加 1 轮，累计最多 12 轮。诊断的原阈值 `execution.max_diagnosis_rounds` 默认 2，自动追加后累计最多 4 次。达到自动上限后等待用户，人工也只能明确批准一次操作。额度不因重启、改名、切换 provider 或修改配置而清零。

控制器比较固定验收项的历史最好成绩。首次有效观察建立基线；只有固定项新增通过且已通过项没有退步，才算进展。其后连续两次未满足这个条件，就进入等待。诊断、换措辞、增加简单测试、退步后恢复都不算进展。缺少有效证据最多自动补采一次。停止后不会再自动调用实现、诊断或决策代理。人工批准一次后再次评估，不重置历史。

自动处理范围包括限定修复/诊断额度、规则明确的复验、既有需求内通过审查的局部计划修订，以及引用既定配置/计划技术事实的澄清。业务取舍、原型人工审核、凭据、资源上限、发布后的例外处理仍等待用户。决策不会跳过类型检查、测试、独立审查或受保护文件规则；自动授权会单独记录为 `agent`，不伪造人工回答。无法给出有效决策时转人工，最多一次格式修正。

设置 `automation.enabled=false` 可关闭新的代理决策，保留原有人审流程和进度停止规则。旧配置读取时补充缺失默认字段，保留显式设置、累计计数和有效产物。升级程序后，等待中的技术授权会在续跑时重新核对；显式暂停的任务需要用户执行 `aa2 run --project ...` 续跑。正在运行的进程不会热加载新代码，请在暂停后重新启动。

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

默认两个并行开发任务；单任务四轮后需要诊断及追加授权，自动累计最多 12 轮。连续两次未证实进展直接停止并等待处理。实际计划回退另按固定验收项比较进展，其停滞阈值为 `execution.max_stalled_replans`（默认 `2`）；任务实现/复验的阈值为 `execution.max_stalled_rounds`（默认 `2`）。完整规则见上方“自动恢复与停止”。

进展比较实际测试通过项、浏览器断言及控制器契约检查；模型自称改进、改名、减少检查或重复恢复到曾经通过的状态不计功。缺少可比较证据也会触发停滞。`status` 显示待回退阶段和评估结果，日志显示连续无改进次数。用户确认可再尝试一次，历史计数保留。恢复、任务改名或切换 provider 不清零历史消耗；实现轮数限制仍独立生效。累计 token 只计量，不再触发停止；旧 `execution.max_tokens` 字段可读但不生效。基础设施重试也持久计数；用户确认重试后明确授予新一组重试机会。

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
