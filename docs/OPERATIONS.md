# 操作手册

## 首次使用

1. 按README建立venv并安装。
2. 开启iTerm2 Python API并完成首次授权；不要升级Codex/iTerm2来“试一下”而不记录版本。
3. 在一个终端运行`.venv/bin/cliretry daemon`；保持该终端和原有Codex会话运行。
4. 从另一个终端运行`.venv/bin/cliretry watch --tabs 1,3,5`，按当前最前面的iTerm窗口从左到右选择tab。
5. 用`.venv/bin/cliretry status`查看绑定和状态；需要判定详情时使用`inspect --session '<UUID>' --json`（UUID可从watch返回结果复制）。
6. 当前仅开放只读观察；生产自动发送兼容性门保持关闭。状态见[COMPATIBILITY](COMPATIBILITY.md)。

`--tabs`接受逗号分隔的正整数，例如`--tabs 2,4,5`。tab 序号只在本次选择时解析；解析结果固定到Session UUID。tab重排不会重新指向别的会话。存在多个iTerm窗口时以当前最前面的窗口为准；选中的tab里必须能唯一识别一个原生Codex进程，多Codex分屏会失败关闭。最多绑定4个会话；整批身份核验成功后才会写入绑定。候选二进制由进程信息发现，但仍进行原生身份检查，自动发送仍受独立兼容性注册表门控。

所有客户端和daemon应使用同一个`--config`文件。配置修改后重启daemon，旧绑定的模式回到OBSERVE，已有暂停会保留，需检查现场后resume。

## 状态解释

| 状态/原因 | 意义和下一步 |
| --- | --- |
| BASELINING | 建立当前历史基线，不处理已有错误 |
| OBSERVING | 只读观察；mode=AUTO也不会对普通空闲输入 |
| CANDIDATE/BACKOFF | 确认新错误、等待重试时刻 |
| VERIFYING_TEXT | 已输入文字，正在确认编辑区；不要假定已提交 |
| WAIT_ACK | 已提交一次，等待新执行证据 |
| PAUSED_USER | 用户按键/手动暂停；检查现场后resume，再决定enable |
| PAUSED_UNCONFIRMED | 输入/提交/恢复结果不确定；不要盲目再发继续 |
| COMPOSER_UNVERIFIABLE | 当前样式或输入区无法验证；保持观察并校准profile |
| CAPABILITY_NOT_VALIDATED | 当前版本组合还没有真实验收证据 |
| API_DISCONNECTED | 开启API、检查授权与iTerm2是否运行 |
| TARGET_GONE/IDENTITY_CHANGED | 原进程/Session已变；unwatch旧绑定，再watch新目标 |
| AUTH/USAGE_LIMIT/QUOTA_OR_BILLING | 由用户处理认证或额度，本工具不会切换账号 |
| *_BUDGET_EXHAUSTED/RUN_EXPIRED | 查看logs；明确需要新的运行预算时reset-budget，再enable |

## 已经中断的会话

启动时已有错误会被基线忽略。要处理这一条：

```bash
cliretry inspect --session '<ID>' --json
cliretry retry-current --session '<ID>' --evidence '<TOKEN>'
```

token在60秒内提交一次有效；接受后仍遵守退避。此命令只授权当前事件的一次恢复，不开启后续AUTO。token过期、界面改变、用户活动、预算/兼容性不通过均拒绝。

## 暂停、退出与不确定结果

```bash
cliretry pause --all
cliretry status --json
cliretry logs --limit 30
cliretry resume --session '<ID>'
cliretry unwatch --session '<ID>'
cliretry shutdown
```

pause会先撤销未来发送；已经送出的字节无法撤回。查看`in_flight_attempt_id`和当前状态。resume是用户确认已检查现场后解除暂停的操作，仍回到只观察，并保留去重记录和预算。

若客户端等待超时，会打印request ID。使用`request-result --id ID`查结果，不自动重新执行同一操作；明确返回未知时先查看status/输入区。

停止daemon不会停止Codex。关闭iTerm2/注销/电脑睡眠可能中断任务；CLIRetry不能在睡眠期间运行，也不会修改电源设置。

## 文件

默认目录：`~/Library/Application Support/CLIRetry`，目录0700。

- `daemon.lock`：进程锁。
- `control.sock`：同用户控制socket。
- `state.sqlite3`：绑定、预算、事件、发送意图与审计。
- `logs/cliretry.jsonl`：元数据日志，5MiB轮转、最多5份备份。

daemon不记录终端全文、API凭据或进程环境。显式capture工具会记录目标屏幕，分享前需脱敏。不要删除数据库来解除重试限制，使用reset-budget并保留审计。

capture输出和旁边的`.jsonl.manifest.json`从创建时即为0600，并拒绝覆盖。manifest中的`complete=false`表示捕获未完成；只有指定`--executable`或配置了允许路径，才会同时验证并记录原生进程身份/SHA-256。屏幕脱敏后应重新计算校验值，并记录脱敏方法，不能沿用原始hash声称未改动。

## 开发验收：原生审批卡片采集

真实原生审批卡片已捕获。早期tab 5样本复用了旧标记且目标文件已存在，只算界面/采集门控证据；2026-10-07又从tab 6（iTerm API Session UUID已脱敏）以新标记和全新目标完成探针。新样本在捕获时仍待处理，目标在触发前不存在；只读采集没有发送按键，也没有批准/拒绝。之后用户自行批准，助手只读确认目标为普通0字节文件且非符号链接。原始捕获`local-artifacts/native-approval-current-tab6.jsonl`权限0600，脱敏fixture位于`tests/fixtures/native_codex_0_160_iterm_3_7_3/approval_cases/fresh_target_probe`。profile对该254列画面安全失败关闭为`UNKNOWN/GEOMETRY_UNSUPPORTED`、不可重试；生产AUTO仍未放行。测试工具和助手不得代为批准/拒绝。

在专用临时工作目录中启动独立原生Codex；使用当前0.160.0二进制路径（通常为`/usr/local/Caskroom/codex/0.160.0/bin/codex`），明确设置`on-request`和`workspace-write`：

```bash
approval_workspace="$(mktemp -d /tmp/cliretry-approval.XXXXXX)"
/usr/local/Caskroom/codex/0.160.0/bin/codex --no-daemon --no-alt-screen \
  --sandbox workspace-write --ask-for-approval on-request \
  -c 'approvals_reviewer="user"' \
  -c check_for_update_on_startup=false --cd "$approval_workspace"
```

在新TUI中要求它只调用shell工具执行一个唯一标记的`touch`命令，目标是本项目`local-artifacts/`中尚不存在的临时文件。不要重用已执行过的标记（例如`CLIRetryApproval20261005R9W4X2`）；每次测试都换新标记。该路径在此Codex的临时workspace之外，正常情况下应触发人工审批；命令若被执行也只会创建空文件。若它直接执行而没有显示审批卡片，立即停止，不要再次尝试；此情况不算通过，应检查是否有额外可写目录配置。审批卡片出现后保持原样，不要批准也不要拒绝。使用`cliretry sessions`从iTerm2 API会话列表取得完整Session UUID（不要使用Codex `/status`对话ID或iTerm标签序号），并从另一终端只读采集：

```bash
.venv/bin/python tools/capture_fixture.py --session '<SESSION_ID>' --executable '<CURRENT_NATIVE_CODEX_PATH>' --approval-marker '<SAME_MARKER>' --output 'local-artifacts/native-approval-<UNIQUE_SUFFIX>.jsonl'
```

确认捕获帧确为原生审批卡后，可用导出器创建脱敏回归fixture；`--target-state`应按采集前核对的文件状态填写，不能把旧目标写成`new`：

```bash
.venv/bin/python tools/export_native_fixture.py --kind approval \
  --source 'local-artifacts/native-approval-<UNIQUE_SUFFIX>.jsonl' \
  --approval-marker '<SAME_MARKER>' --approval-reviewer user \
  --redact-prefix '<ABSOLUTE_TARGET_DIRECTORY_PREFIX>' \
  --codex-version '<CODEX_VERSION>' --target-state new \
  --output 'tests/fixtures/native_codex_<VERSION>/approval_cases/<CASE>'
```

`--approval-marker`模式要求原生Codex进程身份校验成功；只有同一屏同时包含标记和当前识别的审批提示文案（例如“Would you like to run”）才会写入。普通对话、只有标记、只有审批提示或身份不符时均拒绝并且不创建capture/manifest。采集器只调用iTerm2屏幕读取接口，不发送输入。输出含终端全文且权限为0600，分享前先脱敏。若真实UI文案不在白名单中，工具会失败关闭；保留失败原因，先人工核对屏幕再审慎更新提示模式，不能通过批准操作测试。

通过门控仅证明得到一帧可复核的审批界面；2026-10-07的全新目标探针已补为分类fixture与回归，但因宽度超出profile上限，当前分类是安全失败关闭的`UNKNOWN/GEOMETRY_UNSUPPORTED`。这不证明审批决策流程或生产AUTO可用。任何审批探针都不得用宿主级提权弹窗代替原生Codex审批UI。

长用户名导致Unix socket路径过长时，可以在配置中指定一个当前用户拥有的0700短目录作为`daemon.socket_path`的父目录。

## 开发验收：真实人工按键撤销

以下命令只创建工具自己拥有的合成TUI窗口，测试结束后关闭该窗口。它不绑定真实工作会话，也不放开生产版本组合。需要操作者在旁；两个场景分别执行，输出目录必须不存在：

```bash
.venv/bin/python tools/live_sender_smoke.py --human-activity before-type \
  --output-dir local-artifacts/live-keyboard-before
.venv/bin/python tools/live_sender_smoke.py --human-activity after-type \
  --output-dir local-artifacts/live-keyboard-after
```

每次出现`human_input_required=true`后，在标为`CLIRetry isolated Sender test`的新窗口中于10秒内按一次小写`x`，不要按Enter。`after-type`场景需等输入区出现“继续”再按。提示同时保存在产物目录的`human-input-ready.json`，包含准确的Window/Session ID；不要在原工作窗口输入。

成功条件：实际SDK键盘通知撤销latch，生产adapter/Sender检查停止后续发送，最终PAUSED_USER。`before-type`字节记录必须只有`78`（x），`after-type`必须为`e7bba7e7bbad78`（继续x）；两者都不能有CR，submit调用为0。工具不注入按键，不用故障注入替代latch检查；仍需操作者确认确为物理按键，不能仅凭通知声明物理来源。

无人响应时返回非零退出码、`complete=false`和`HUMAN_INPUT_TIMEOUT`，保留已输入文字且不发送Enter，然后清理测试窗口；这是未完成验收，不是成功。等待最多10秒，低于生产默认15秒休眠间隔保护。不与`--broadcast`或`--fault`组合；不要通过自动化按键假冒人工验收。

## 卸载

## 开发验收：原生本地503自动恢复链

仅在新建的隔离原生窗口测试，不绑定用户工作Session，不登记发布认证组合：

```bash
.venv/bin/python tools/capture_native_isolated.py \
  --executable '<CURRENT_NATIVE_CODEX_PATH>' --mock-error --auto-recover \
  --working-dir '<TRUSTED_TEST_DIRECTORY>' --output-dir '<NEW_ARTIFACT_DIRECTORY>'
```

本地HTTP服务器等待BUSY基线建立后返回503；沿用默认退避及生产daemon/Sender，在一次RESUMED/REFAILED后暂停并清理窗口。测试认证仅限该Session、原生身份、连接epoch，原生进程检查不被替换。结果必须同时核对complete、native_auto_complete、auto_attempts、auto_guarded_calls、auto_keyboard_events和window_closed；通过不等于生产发布验收完成。

检查原生错误分类时，可在新的隔离Codex Session中采集HTTP 429/502/503/504及响应文案。此路径只捕获屏幕，不发送恢复提示；每个场景使用新的产物目录：

```bash
.venv/bin/python tools/capture_native_isolated.py \\
  --executable '<CURRENT_NATIVE_CODEX_PATH>' --mock-error --mock-status 429 \\
  --mock-message 'exceeded retry limit, last status: 429 Too Many Requests' \\
  --working-dir '<TRUSTED_TEST_DIRECTORY>' --output-dir '<NEW_ARTIFACT_DIRECTORY>'
```

只有capture结果中profile类别符合预期且无blocker，才记该UI分类为通过；不匹配应保持拒绝自动发送并记录样本，修正规则后重新采集。

## 卸载说明

先shutdown，确认进程退出，再从专用venv卸载cliretry或移除该venv。状态与日志默认保留供审计；由用户决定是否清理。第一版不安装LaunchAgent、不修改shell rc或Codex配置。
