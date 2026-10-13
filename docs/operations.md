# 运行与故障处理

## 启动前

运行 `aa2 doctor --project PATH`，确认 profile 存在、模型参数符合本机 CLI、bubblewrap 的实际写入隔离检查通过。`--live` 会消耗 provider 用量，需有可用认证。

保持项目状态目录的私有权限，不将其提交到 Git。配置里的秘密使用受控环境变量或本机 provider 的认证能力。日志不得包含 API key。

## 恢复

中断后运行 `aa2 run --project PATH`。未完成迭代存在时，带需求参数的新命令会直接报错。`status` 显示待处理问题，`respond` 支持从另一终端回复。

控制进程保存原始输入、规格、阶段输出、实现轮次、provider 会话 ID 和验证结果。不要手工删除未完成 run 的状态目录。原始需求文件在快照保存后不再是续跑前提。

自动回退使用 `execution.max_stalled_replans`（默认 2）。首次回退建立验证基线，之后有新增通过的固定验收项且没有退步/检查缺失就继续；连续两次没有可比较的改进则等待用户处理。审查说法改变、源码修改、只打开浏览器页面都不构成进展。查看 `status` 的 `replanning` 和 `logs` 中 `replan.assessed` 可了解回退原因、目标及停滞次数。

停滞提示中的 `y` 只授予再试一次，不清零计数；`n` 保存现场并退出。仅重启或更换 provider 不会重新调用模型绕过等待。人工解决阻塞后可再次 `run` 并确认重试，也可明确调整停滞阈值。

新迭代固定目标分支及其已提交基线。主目录有修改时，先自行提交或保存到其它目录再回复 retry。目标被其它 worktree 占用时先释放。恢复不重新选择基线；中途更改 delivery.target_branch 不改变当前迭代的目标。

主分支在开发期间更新时，会在隔离工作区集成、必要时修复并重新验收。默认最多两次；耗尽后可稳定分支并明确增加 delivery.max_reintegrations，已用次数保持不变。

交付阶段依次是 ready、reintegrating（需要时）、publishing、published、runtime、complete。发布前中断不重复已验收任务；发布后中断仅继续 Git 恢复与环境交接。Git 的未知锁、未知索引或用户修改会阻止自动修复。不要删除未知 index.lock；先确认其它 Git 进程是否运行。若发布后又创建了用户提交，aa2 保留它并等待人工核对，不会 reset。

发布开始后的 cancel 会暂停收尾。普通 run 会清除这次停止请求并续跑。只有交付回执和当前环境引用一起持久化后，迭代才算成功。

## 环境问题

共享 Conda 环境只允许 EnvironmentManager 安装。依赖更新产生新指纹。失败的环境不会发布 ready 标识，下次按锁定输入重建。Conda 缓存通过子进程配置定向到项目，不修改全局 .condarc。

浏览器启动缺少系统库时优先解决已安装工具链条件；不要执行全局 apt/sudo 安装来绕过项目隔离。可以先在受支持的 Linux/WSL2 主机完成环境准备。

## 清理与空间

自动 GC 在运行中执行。`aa2 gc --dry-run` 可检查候选资源。空间不足时优先删除可再生缓存并降低并发，不能删除活跃工作区或尚未保存的变更。

正式代码位于目标分支和主目录。交付回执（status 可见）关联提交、规格、审批、测试、用量及运行环境。成功后回收工作区、私有 refs/aa2 引用、临时会话、无引用的中间产物；保留回执、用量账本和必要验收证据。

当前运行环境具有交付引用，gc 不会按 TTL 删除它。不要手动删除 .aa2/environments 中当前使用的环境。source .aa2/runtime.sh 使用已准备环境；主目录的 node_modules 和必要构建输出也会保留。

cleanup_pending=true 表示交付已成功但回收需要重试。普通 run 和 gc 均会重试，不再次调用模型。gc --dry-run 只报告普通资源候选，不修改状态；私有 Git 引用和产物压缩的实际回收在持有 controller.lock 时执行。

## Provider 配置变化

使用 `aa2 provider use NAME` 或 `run --provider NAME`。切换后的选择保存在项目配置；未知实例无法使用。环境变量 null 表示移除继承值，常用于选择 CLI 自身已保存的登录认证。

模型和 effort 从用户 profile 读取。更新用户 profile 后，后续调用记录新指纹；aa2 不自动追踪所谓最新模型。
