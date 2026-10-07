# CLIRetry

通过 iTerm2 Python API 监控已运行的 Codex CLI，在明确的临时故障后退避并定向提交“继续”。不需要把原有会话迁移到 tmux。

## 当前状态

截至2026-10-07，iTerm2 3.7.3 / SDK 2.25 / Codex 0.160.0 的只读观察、身份校验和多项隔离发送保护已完成真实验收。`watch --tabs 1,3,5` 已可按当前 iTerm 窗口内的 tab 序号批量绑定会话。

**生产自动发送尚未开放。** 兼容性注册表仍为空，`enable` 会返回 `CAPABILITY_NOT_VALIDATED`；tab 序号入口默认只建立 `OBSERVE` 观察，不会发送“继续”。详情见[兼容性记录](docs/COMPATIBILITY.md)、[测试报告](docs/TEST_REPORT.md)和[实现验收清单](docs/IMPLEMENTATION_AUDIT.md)。

## 安装

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/cliretry --help
```

Python 3.11+；当前开发验证使用3.12。iTerm2 SDK固定为2.25，避免内部连接桥接接口随升级改变。不会修改Codex安装或shell启动文件。

## 使用

先在一个终端启动服务；目标 Codex 仍照常在 iTerm 中运行：

```bash
.venv/bin/cliretry daemon
```

在另一个终端中，选择当前最前面的 iTerm 窗口里的 tab 序号：

```bash
.venv/bin/cliretry watch --tabs 1,3,5
.venv/bin/cliretry status
```

tab 序号从左到右、从 1 开始；`--tabs` 一次最多绑定 4 个。CLIRetry 会自动核对其中的原生 Codex 进程并显示绑定结果；内部固定到解析出的 Session UUID。之后即使 tab 重排，也不会切换监控目标。分屏里若没有 Codex，或有多个 Codex 会话导致目标不唯一，该批绑定会整体拒绝。若有多个 iTerm 窗口，序号按当前最前面的窗口解释。

`watch` 默认只读观察。它从所选会话的原生进程信息发现候选 Codex 路径并执行进程身份校验；一般使用不再需要手工运行 `sessions`、`doctor` 或复制 UUID。若使用自定义配置，daemon 和其他命令必须传同一个 `--config PATH`。

当前兼容性门尚未放行自动发送；不要尝试以手工改配置绕过。查看会话判定可运行：

```bash
.venv/bin/cliretry inspect --session '<SESSION_UUID>' --json
.venv/bin/cliretry logs --limit 30
```

停止监控并解除所有绑定：

```bash
.venv/bin/cliretry unwatch --all
.venv/bin/cliretry shutdown
```

暂停/关闭 CLIRetry 不会停止 Codex 本身。

如果监控开启前就已经卡住：`inspect --session ID`取得60秒内有效的`evidence_token`，使用`retry-current --session ID --evidence TOKEN`请求一次恢复。它仍需通过全部保护条件和兼容性门槛，不会持续开启AUTO。

## 行为

- 支持识别容量不足、明确的429重试耗尽、连接/流断开和临时502/503/504。
- 认证失败、使用额度/余额耗尽、上下文失败、策略提示和审批会暂停。
- 不因正常回合结束或屏幕静止而提交。
- 默认退避30秒起，封顶15分钟，并遵守可解析的服务端等待时间。
- 每条故障链、滚动小时、显式运行窗口都有次数/时间限制；重启不会清零。
- 先输入文字，核对编辑区，再单独提交一次Enter；结果不明时保留现场并暂停。
- 不切换模型/账号、不重启Codex、不点击审批、不使用剪贴板、不抢焦点。
- 仅支持本机原生Codex TUI。Cockpit启动包装器只有在最终前台进程和UI是受支持的Codex时才适用；SSH、tmux和Cockpit自带GUI不支持。

终端文字不是结构化服务端事件。未知布局、无法区分占位文字与用户输入、无法证明恢复成功时，会停止自动输入。操作系统进程切换与终端输入之间也不是跨系统原子事务。

## 开发与验证

```bash
.venv/bin/ruff check src tests tools
.venv/bin/pytest -q
.venv/bin/python tools/probe_iterm.py
```

显式捕获一个选定Session，输出包含终端文字，分享前必须检查：

```bash
.venv/bin/python tools/capture_fixture.py --session '<SESSION_ID>' \
  --executable '<NATIVE_CODEX_PATH>' --output ./capture.jsonl
.venv/bin/python tools/replay_fixtures.py ./capture.jsonl
```

采集审批UI时附加`--approval-marker '<UNIQUE_MARKER>'`启用失败关闭门控：先确认指定Session是原生Codex，再且仅当同一帧同时包含该标记与已知审批提示文案时才保存。没有命中不会创建输出文件。安全步骤见[操作手册](docs/OPERATIONS.md#开发验收原生审批卡片采集)。

仓库内已有16组合成样本，可直接回放：

```bash
.venv/bin/python tools/replay_fixtures.py tests/fixtures/codex_local_v1/frames.jsonl
```

四会话两小时实测命令见[兼容性记录](docs/COMPATIBILITY.md)。合成样本、加速时钟回放和真实会话实测分别记录。

设计基准：[DESIGN_IMPLEMENTATION.md](DESIGN_IMPLEMENTATION.md)。日常运行：[OPERATIONS.md](docs/OPERATIONS.md)。
