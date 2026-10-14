# 验证记录

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
