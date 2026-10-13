# 验证记录

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

交付流程的真实 provider 全流程尚未重复调用；本轮使用可控 provider 验证门禁语义，并用真实 Git、依赖安装、构建和浏览器验证交接。旧版 provider 验证不能当作新版自动合并的模型端到端成功率基准。

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
