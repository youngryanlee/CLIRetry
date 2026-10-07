# CLIRetry 工作状态与交接记录

最后更新：2026-10-07（Asia/Shanghai）

## 本轮实施进度

- 第 1 步（接口与安全边界梳理）已完成：`watch --tabs 1,3,5` 按当前前台 iTerm 窗口中从左到右的 1-based tab 顺序解析；每个 tab 必须唯一对应一个会话，分屏/窗口/会话映射不明确时整体拒绝。解析成功后固定绑定 UUID，不跟随重排。
- 通过 tab 入口时可从该 tab 的原生进程信息发现 Codex 可执行文件，仍需完整进程身份校验；版本/hash 兼容性门继续独立控制所有发送，不能因自动发现路径而绕过发布门。
- 第 2 步（iTerm 适配层）已完成：新增当前前台窗口的 tab 位置解析、分屏 session 枚举及基于原生 `jobPid` 发现 Codex 可执行文件候选路径。适配层回归 `26 passed`；该层已接入 daemon/CLI。
- 第 3 步（CLI/daemon 批量绑定）已完成：新增 `watch --tabs 1,3,5`，序号是当前前台 iTerm 窗口内从左到右的 1-based 位置。每个 tab 需唯一解析到一个原生 Codex 会话；无 Codex、多 Codex 分屏、编号失效或预检期间映射变化都会拒绝整批请求。原生路径可从 `jobPid` 候选发现，但仍经过完整身份校验；多个绑定在单个 SQLite 事务中提交，绑定后固定 UUID。聚焦 adapter/CLI/daemon 测试 `54 passed`。
- 第 4 步（回归验证）已完成并于 2026-10-07 复核：全套 `193 passed, 17 warnings`（19.18 秒）；Ruff 和 compileall 均通过；`cliretry watch --help` 已显示 `--tabs TABS` 及示例格式。尚未对真实 iTerm 发起任何绑定或输入操作。
- 第 5 步（真实 API 映射检查）已完成：通过只读 iTerm API 查询当前窗口第 6 个 tab，得到 1 个可见 session，并与此前已知 tab 6 的 Session UUID 相符。未建立 watch、未发送输入、未更改 iTerm 状态。
- 第 6 步（README/操作手册同步）已完成：README 与 `docs/OPERATIONS.md` 已记录 `watch --tabs 1,3,5` 用法、编号作用域、固定 UUID 绑定、分屏/歧义拒绝及默认 `OBSERVE`。本状态文件此前未同步这一步，现已补齐。
- 当前结论：tab 序号入口的实现、回归和用户文档已完成；尚未对真实 tab 建立 watch 或发送输入。该入口默认建立 `OBSERVE`，不会解除 AUTO 发布门。

## GitHub 发布进度

- 2026-10-07：当前目录原先没有 `.git` 元数据。对 `https://github.com/youngryanlee/CLIRetry` 的匿名 HTTPS `ls-remote` 已成功且没有分支引用，SSH 认证识别为 `youngryanlee`，远端目前为空。不会从其他项目读取凭证；现有 SSH 身份足以访问目标仓库。
- 发布前检查未发现常见 GitHub/OpenAI/AWS token 或私钥内容；`local-artifacts/` 已在 `.gitignore` 中排除。公开文档中出现的真实 iTerm Session UUID 已脱敏；新增 `*.egg-info/` 与 `.DS_Store` 忽略规则。
- 暂存复核发现若干原生回放 fixture 仍标记为 `redacted=false`，帧中含真实 iTerm Session UUID 和本机工作目录。已将 6 组、共 17 帧中的 Session ID、运行时序号/时间和本机 home 路径脱敏；路径使用等宽遮盖，清单标为 `redacted=true` 并重算 frame hash。已有审批 fixture 原本已脱敏。
- fixture 脱敏后重新验证：全套 `193 passed, 17 warnings`（24.75 秒），Ruff 与 compileall 均通过；弃用警告来自上游 websockets/iTerm2 SDK。
- 已初始化本地 `main` 分支并添加 SSH 远端。暂存区共 87 个源码、测试、fixture 和文档文件；`.venv/`、构建产物、`*.egg-info/`、`local-artifacts/` 均未纳入。最终扫描确认文档/fixture 无 UUID-shaped 值、fixture 无本机 home 路径、全部 fixture manifest 标记脱敏，且未发现常见 token/私钥模式。
- `git diff --cached --check` 仅报告原有文件中的尾随空格及文件末尾额外空行；未为发布而重排无关的设计文档/源码格式。下一步创建初始提交并推送 `main`，然后记录远端 commit hash。

## 一句话状态

CLIRetry 的只读观察、诊断与核心保护逻辑已实现并经真实 iTerm2/Codex 场景验收；tab 序号批量入口 `watch --tabs 1,3,5` 已实现并有回归测试，但尚未对真实 tab 建立绑定。项目目前**尚未达到可生产使用的自动重试状态**：生产兼容性注册表为空，`enable` 会被 `CAPABILITY_NOT_VALIDATED` 安全拦截。

## 用户目标与当前差距

用户希望只提供若干 iTerm tab 序号，由 CLIRetry 自动绑定并监控这些 Codex 会话，在指定的暂时性错误出现时发送一次“继续”。目前：

- `watch --tabs 1,3,5` 可将当前最前方 iTerm 窗口中从左到右的 tab 一次性解析并绑定（最多 4 个）；daemon 必须预先运行。绑定后使用固定 Session UUID，不会因 tab 重排自动切换目标。
- 每个 tab 必须唯一对应一个原生 Codex 会话；无 Codex、多 Codex 分屏、编号失效或预检期间映射变化时拒绝整批操作。tab 入口可以自动发现原生 Codex 可执行文件候选，但身份验证和兼容性门仍然生效。
- 目前只能安全地 `OBSERVE`；不能生产 AUTO 自动发送。用户尚未进行真实 tab 绑定及发送验收。
- daemon 的启动仍是额外步骤；自动启动 daemon 尚未实现。

## 已完成并有证据的工作

- 目标宿主组合：iTerm2 3.7.3、iTerm2 Python SDK 2.25、Codex 0.160.0，二进制 SHA-256 `5383ef71dd1bd8d2f3658c04a219e2cf165c7969aebd0cceced1bc9f0f68877f`，profile `codex-local-v1.1`。
- iTerm API 连接、Session 枚举、原生进程身份/TTY 校验、只读读屏、daemon 管理及 session 绑定已实测。
- 发送器两阶段输入/确认、真实物理按键撤销、广播隔离及不确定发送结果不重放已有实测；人工键盘事件由用户确认是真实物理按键。
- 一个 UPSTREAM_TRANSIENT 本地模拟故障在隔离测试身份门下完成原生 Codex 自动恢复闭环；这不是生产兼容性放行，也不是真实供应商故障测试。
- 新版四会话只读观察完成 7200.042 秒、1440 个样本，零输入/断连/暂停/不可验证；资源及数据库检查通过。
- 2026-10-07 的全新目标审批探针已通过原生身份及标记/审批文案双门控。用户随后手动批准，助手只读确认目标是普通 0 字节文件、非符号链接。捕获时仍待审批的原始样本为 `local-artifacts/native-approval-current-tab6.jsonl`（权限 0600）；脱敏 fixture 为 `tests/fixtures/native_codex_0_160_iterm_3_7_3/approval_cases/fresh_target_probe/`。
- tab 入口最新代码验证：全套 `193 passed, 17 warnings`；Ruff 与 compileall 通过。警告来自上游 websockets / iTerm2 SDK 弃用提示。README/操作手册在上述回归后同步了用法说明。

## 仍未完成的发布门槛

- `src/cliretry/compatibility.py` 中 `VERIFIED_COMBINATIONS` 仍为空；不可为了让 `enable` 成功而直接登记版本组合。
- 没有独立原生证据支持 `STREAM_TRANSIENT`；标准 `response.failed` 路径当前渲染为 `CONNECTION_TRANSIENT`。目前完整 AUTO 恢复闭环仅对隔离的本地 UPSTREAM_TRANSIENT 样本实测。
- 254 列的审批画面超过 profile 的 240 列上限，被安全分类为 `UNKNOWN/GEOMETRY_UNSUPPORTED`、不可重试。审批后的 touch 成功不等于 profile 能安全识别该布局。
- 兼容性证据以本地模拟为主，不代表真实云端供应商故障测试。

## 建议下一步

1. 如用户确认，可在 daemon 运行后先用 `watch --tabs N,...` 对目标 tab 建立 `OBSERVE` 绑定，并核实输出的 tab→Session 映射；本轮尚未执行该真实绑定。
2. 决定是否要实现 daemon 自动启动，以进一步缩短日常命令流程；当前仍要求 daemon 预先运行。
3. 补齐或明确排除 `STREAM_TRANSIENT` 的验收范围，再根据实际证据审阅精确版本组合是否可加入兼容性注册表。没有完成此前不要开放 AUTO；tab CLI 本身不会开放发送门。

## 主要命令（现状，不代表 AUTO 已放行）

```bash
.venv/bin/cliretry daemon --config config.example.toml
.venv/bin/cliretry watch --config config.example.toml --tabs 1,3,5
```

启动 daemon 后，在**当前最前方 iTerm 窗口**执行 `watch --tabs 1,3,5`，序号从左至右、从 1 开始。CLI 会先整体预检并显示映射，再将会话绑定为 `OBSERVE`；内部使用固定 Session UUID，因此不需要用户查询或输入 UUID。该命令不会启用自动发送。Codex `/status` 对话 ID 与 tab 序号都不是 Session UUID。

## 交接入口与注意事项

- 当前状态/详细证据：[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md)、[`docs/TEST_REPORT.md`](docs/TEST_REPORT.md)、[`docs/IMPLEMENTATION_AUDIT.md`](docs/IMPLEMENTATION_AUDIT.md)。
- 操作流程：[`docs/OPERATIONS.md`](docs/OPERATIONS.md)。
- 下次接手先读本文件，再读兼容性记录；不要覆盖/删除已有本地 artifacts 或此前改动。
- 本工作区没有检测到 `.git` 目录，`git status` 不可用；不能假定当前改动已提交或可从版本库恢复。
- 本文件应在每次有意义的实现/验收进展后更新：记录日期、事实证据、测试结果、未完成门槛和下一项工作。
