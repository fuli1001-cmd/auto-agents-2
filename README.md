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

每次迭代以目标分支的已提交版本为基线。验收通过后自动快进合并到目标分支，并更新项目主目录、准备运行依赖。新空仓库使用 `main`；已有仓库优先采用本地存在的远端默认分支，其次唯一的 `main`/`master` 或唯一分支；有歧义时询问一次并保存选择。

无需切换到 run 分支。交付成功后：

```bash
cd demo
source .aa2/runtime.sh
# 按生成项目的 README 执行启动命令
```

配置可显式指定目标和主分支并发变化的重集成上限：

```json
"delivery": {"target_branch": "main", "max_reintegrations": 2}
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
    "review": "balanced", "verify": "balanced", "readme": "balanced"
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

`environment` 中 `null` 移除继承变量，空字符串保留。`extra_args` 是 argv 数组，支持模型、effort 和兼容的模型配置覆盖；输出协议和权限参数受保护。

```bash
aa2 run --project ./demo --provider antigravity
aa2 provider use codex --project ./demo
```

显式切换和成功故障切换会保存 active provider。模型从配置获得，不自动升级。审查默认使用当前 provider 的新会话。原生 provider 的配置及认证输入在受控会话目录中使用，不改写全局模型设置。

填写明确的 `model` 可启用对应模型家族的 prompt 适配；模型留空时遵从 CLI 默认，使用通用 prompt。两种 CLI 都从标准输入接收需求，避免较长文档超过进程参数长度限制。

## 对话与审批

交互终端直接回复问题。非交互运行保持等待，可在另一终端操作：

```bash
aa2 status --project ./demo
aa2 respond --project ./demo --answer '补充的业务规则'
aa2 respond --project ./demo --request QUESTION_ID --artifact ARTIFACT_HASH --answer approve
```

原型通过自动检查后提供本地预览地址。审批绑定内容指纹，修改原型后必须重新批准。多个待处理问题时需要指定 `--request`。

## 环境、验证和权限

- Git worktree 隔离任务代码；相同依赖配置共享项目内只读 Conda 环境。
- agent 使用只读沙箱检查代码，以结构化候选变更返回实现；控制器验证路径、保存变更日志后应用。
- 修改已有测试和验证配置需要独立审查，不由实现者自行改变通过条件。
- 安装、构建与测试在明确的运行环境中执行。缺少系统库时应解决环境问题，不自动修改系统包。
- 必须实际执行检查；provider 的成功退出或通过声明不能代替验证证据。
- 本地虚拟环境不等同于完整机器安全边界。bubblewrap 的实际权限探针必须通过。

pytest 验证保存控制器生成的 JUnit 报告与用例清单；只跳过测试、零测试、超时均不能通过。浏览器验证覆盖桌面和手机视口，并执行规划中的交互断言。原型仅使用本地或内联资源；正式页面会与审批过的原型一起交给独立审查。

首期 Node 安装器支持 npm 与 `package-lock.json`。已有 pnpm/yarn 项目会明确提示需要相应安装器，保留其包管理器。Next.js 15.5+ 的类型声明在受控准备步骤生成，业务源码和测试在正式验证期间保持只读。Python 的本地/editable 依赖使用独立环境与源码副本，通过 requirements 文件声明，不在共享环境中绑定任务工作区。

默认两个并行开发任务；单任务最多四轮实现；连续无进展触发诊断；每轮迭代最多两次自动回退。配置中的 `execution` 可调整上限，恢复、任务改名或切换 provider 不清零历史消耗。基础设施重试也持久计数；人工回复 retry 明确授予新一组重试机会。

## 自动清理和统计

ResourceManager 默认自动回收，`aa2 gc` 只是在同样保护规则下立即触发一次检查：

```bash
aa2 gc --project ./demo --dry-run
aa2 gc --project ./demo
```

只清理登记的 aa2 资源。运行中的文件、恢复数据、审批和正式证据不会作为临时缓存删除。默认每 60 秒检查，缓存上限 10 GiB，闲置期限七天，最低可用磁盘 5 GiB。

当前交付环境具有独立引用，不受缓存过期、磁盘压力或运行引用释放影响。下一次成功交付后，未再使用的旧环境才按缓存策略回收。

测试报告归档后立即清理临时目录；任务集成并保存 Git 恢复引用后即可删除其 worktree。环境按依赖指纹复用，解析结果保存在 `.aa2/environment-records`，交付时将锁定清单导出到 `docs/aa2/environments`。

日志显示时间戳、阶段、provider、本次调用的模型和推理深度（包含 `extra_args` 覆盖）、真实任务计数和耗时。模型或推理深度未明确配置时显示“CLI 默认（未知）”。结束时汇总整个 run 的 token，包括失败、审查和重试；未收到完整原生用量时明确标记不完整。

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
