# demo4 npm 求解失败的有限恢复修复

基线：aa2 `5c0ab5d`。原 run `run_9ccfd5e078b74be2` / revision 1，0/2；控制器已退出，保留原候选和 incident `incident_e395241fb58c474f`。

## 已确认根因

1. 原 `npm install --package-lock-only --ignore-scripts` 使用 Node 22.23.4、npm 10.9.9、Arborist 8.0.5。没有初始锁文件；registry 请求成功后在 `build-ideal-tree.js:1289` 对空 `node.parent` 读取 `edgesOut`。
2. 保留的 registry 元数据和原日志共同显示：候选 `vitest:^4.0.18` 选择 4.1.12，其可选 peer `@vitest/browser-webdriverio:^4.1.11` 选择 4.1.11，该包反向要求 `vitest:4.1.11`。用原 Arborist 与只读缓存替代网络取元数据的隔离回放，单独保留 `vitest:^4.0.18` 已在同一源码行复现空指针。它是外部求解器崩溃，不能据此认定业务代码错误或断言依赖必然无解。
3. aa2 `_command` 只为 Conda 显式求解拒绝提供 `DependencyResolutionError`。npm 非零退出落入普通 `AA2Error`；`_task` 的现有环境诊断路径未收到类型，顶层按未分类内部异常暂停。新回归在未修改生产代码上复现此分类失败。

上游参考：https://github.com/npm/cli/issues/9787 。具体本机证据优先于类似问题报告。

## 最小改动与边界

- 现有 npm 解锁解析和 `npm ci` 调用显式声明求解上下文。仅识别明确 `ERESOLVE` / `ETARGET` 和本次已复现 `edgesOut` 崩溃；后者有独立类型 `DependencySolverError`，作为已有依赖解析失败类型的子类进入原任务诊断路径。
- 保持网络超时分类；权限、认证、完整性、磁盘、未知输出及 aa2 自身 Python 异常不被泛化为可修依赖错误。不引入盲重试、不清缓存、不升级工具、不加 `force` / `legacy-peer-deps`。
- `_task` 复用既有失败候选归档、诊断、授权、受保护文件审查和真实检查。提示规则说明求解器崩溃不等于 manifest 无效。模型仍需依据和原范围提出方案。
- 核查到最终审核/覆盖复验等直接调用者不具备任务修改权限；如果类型化求解错误逃出任务适配器，公共异常路由显示具体外部依赖前提，不能声称 aa2 内部崩溃或自动授予改写权限。交付处理器现有失败保留流程不变。
- 不修改 demo4 源码、计划、锁或运行数据库；不重新批准原型。恢复后由 aa2 自行处理具体依赖选择并重新准备、检查及审查。

## 验证

- 旧代码红灯：真实安装入口的 npm 崩溃输出错误地抛出普通 AA2Error。
- 缓存启用/关闭，首份锁解析/已有锁 ci，原崩溃和明确依赖拒绝；保留输入/完整诊断、无成功 marker。
- 网络/权限/认证/完整性/磁盘/未知输出/伪提示、非 npm 上下文、成功退出反例。
- 真实 Engine `_environment` → installer subprocess → task failure → 诊断 → 限域候选 → 实际 pytest 验证；含中断恢复、旧失败保留、计数不清零。
- 任务外类型化边界与未知内部异常区分，Conda 回归及相关共享缓存、恢复回归，然后全量 pytest 与静态检查。

## 验证限制与回退

离线 npm CLI 完整复现受只读 cache 匹配限制返回 ENOTCACHED，此结果保留且不冒称成功；另一个精确版本回放因未缓存后续包未完成，不作为可安装证明。只读元数据回放已复现原 range 的 Arborist 崩溃；未证明某个替代依赖方案可安装，须由原 aa2 流程正式验证。

代码修复不修补 npm 本体；可能仍出现其它尚未分类的外部错误。回退仅回退独立源码提交并使用原 run 正式续跑；不删除历史、修改状态或清零累计次数。
