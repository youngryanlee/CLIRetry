# 兼容性与能力探测记录

## 最新进展：2026-10-07 全新目标审批探针

2026-10-07，iTerm tab 6映射到的原生Codex Session UUID 已脱敏。在Codex 0.160.0 / iTerm2 3.7.3 / SDK 2.25中，对全新唯一标记和采集前不存在的目标成功通过原生进程身份校验及同帧标记/审批文案双门控。捕获时审批仍待处理；采集没有发送按键或作出批准/拒绝。之后用户手动批准，独立只读核验确认目标成为普通0字节文件、非符号链接。原始捕获为`local-artifacts/native-approval-current-tab6.jsonl`（0600），记录批准前状态；脱敏回归样本为`tests/fixtures/native_codex_0_160_iterm_3_7_3/approval_cases/fresh_target_probe`，manifest如实记录`target_state=new`和`approval_decisions=0`。profile对该254列画面安全分类为`UNKNOWN/GEOMETRY_UNSUPPORTED`、`ready=false`且无错误类别，不会进入重试。此结果验证了全新目标触发真实审批及用户批准后的空文件创建，但不证明profile支持该布局或生产AUTO安全；生产`VERIFIED_COMBINATIONS`仍为空。

## 历史进展：2026-10-05 审批采集门控

已于2026-10-05在真实原生Codex 0.160.0 / iTerm2 3.7.3 会话中捕获到待处理的人工审批卡。iTerm tab 5由API映射到的Session UUID已脱敏；进程参数确认`workspace-write`、`on-request`和单次`approvals_reviewer="user"`覆盖生效。只读捕获的同一帧包含唯一标记及“Would you like to run the following command?” / “Do you want to allow me...”提示，卡片在捕获时仍待处理，未批准或拒绝。该次误复用了先前标记，目标文件已存在，因此只证明原生审批界面和门控采集可用，不证明首次创建新文件流程。原始捕获在`local-artifacts/native-approval-current-tab5.jsonl`（0600）；脱敏fixture位于`tests/fixtures/native_codex_0_160_iterm_3_7_3/approval_cases`。当前屏幕宽254列，超过profile默认上限240，分类保持`UNKNOWN/GEOMETRY_UNSUPPORTED`且不可重试。`tools/capture_fixture.py`的显式`--approval-marker`仍要求原生身份与同帧标记/提示双门控；`tools/export_native_fixture.py --kind approval`将样本等宽遮盖本机目录并导出。tab 6的全新目标探针已于2026-10-07完成，见上方最新进展；生产`VERIFIED_COMBINATIONS`仍为空。

## 最新检查：2026-10-04 升级后

实际宿主已为3.7.3（运行PID 82727），SDK 2.25经Unix socket握手成功。本机原生Codex已是0.160.0，原0.156.0路径不存在；新二进制SHA-256为 `5383ef71dd1bd8d2f3658c04a219e2cf165c7969aebd0cceced1bc9f0f68877f`。

- `local-artifacts/post-upgrade-read-probe-1`：隔离合成TUI读屏成功，79个单元格返回样式。
- `local-artifacts/post-upgrade-native-error-2`：原生Codex本地503样本通过身份校验；识别BUSY后READY_ERROR，输入区为PLACEHOLDER，无blocker。旧版的样式阻塞在该样本中不再出现。
- `local-artifacts/post-upgrade-broadcast-2`：真实SDK＋合成TUI两阶段发送成功，精确收到继续＋单次CR，旁观Tab零输入，RESUMED。第一轮被外部Enter干扰，未调用自动输入，不计通过。
- `local-artifacts/post-upgrade-type-loss-1`、`post-upgrade-submit-loss-1`：真实送达后注入回执丢失，未补发，重开Store仍PAUSED_UNCONFIRMED/OBSERVE。
- `local-artifacts/post-upgrade-native-continue-1`：原生本地503手动测试路径核对中文后提交单次CR，请求数2→4；不是生产AUTO完整验收。
- `local-artifacts/post-upgrade-keyboard-before-1`：真实人工`x`发生在输入前，收到字节仅`78`，文字/Enter均未发送，最终PAUSED_USER。
- `local-artifacts/post-upgrade-keyboard-after-1`：真实人工`x`发生在“继续”已输入后，收到`e7bba7e7bbad78`，未发送Enter，最终PAUSED_USER。
- 两轮按键测试均收到一次真实SDK键盘通知，工具未模拟按键；操作者已于本轮明确确认两次均为本人在物理键盘按下。
- `local-artifacts/post-upgrade-native-auto-2`：原生0.160.0在真实时钟/default recovery参数下建立BUSY基线，释放本地503，新故障经daemon reducer/退避/Sender/真实身份校验完成一次文字与单次CR，结果FINISHED/RESUMED，请求数2→4，键盘事件0。仅在工具拥有的Session＋identity＋epoch范围内替代发布认证门，未改生产注册表，不是云端故障测试。第一轮因测试工具查询不存在的reason列失败且零输入，已修复并保留失败记录。
- `post-upgrade-native-429-1`、`capacity-2`、`502-2`、`504-2`、`stream-drop-1`：隔离本地fixture响应分别映射到`RATE_LIMIT_TRANSIENT`、`CAPACITY`、`UPSTREAM_TRANSIENT`、`UPSTREAM_TRANSIENT`、`CONNECTION_TRANSIENT`；均为原生终态帧，未输入。原生TCP断开场景最后显示Codex重试后的`502 Bad Gateway`，因此属于`UPSTREAM_TRANSIENT`证据；目前仍没有独立的`STREAM_TRANSIENT`原生呈现证据。`auth-priority-1`的503 `auth_not_found`被优先分类为`AUTH`并阻塞。
- `local-artifacts/post-upgrade-native-response-failed-2`及`tests/fixtures/native_codex_0_160_iterm_3_7_3/stream_failed_cases`：本地loopback发送标准`response.failed`事件，携带`Internal streaming error, please retry.`；原生Codex把它显示为`stream disconnected before completion: ...`，profile分类为`READY_ERROR + PLACEHOLDER + CONNECTION_TRANSIENT`，而不是`STREAM_TRANSIENT`。零自动输入、零键盘事件、窗口已关闭。它说明这条失败事件路径仍应保持连接错误分类，不提供独立`STREAM_TRANSIENT`证据。
- `post-upgrade-native-menu-2`及`tests/fixtures/native_codex_0_160_iterm_3_7_3`：原生启动帧、两帧稳定空闲和输入`/`后的命令菜单已带版本/hash/profile revision归档。只调用一次输入、键盘事件为0、未按Enter；菜单分类为`MENU`/`MENU_PRESENT`，不具备重试资格。首次尝试`menu-1`在欢迎页过早检查，零输入并保留失败记录。此证据是slash命令菜单，不代表审批对话框。
- `tests/fixtures/native_codex_0_160_iterm_3_7_3/error_cases`：带版本/hash/profile revision归档的本地模拟终态帧覆盖`RATE_LIMIT_TRANSIENT`、`CAPACITY`、`UPSTREAM_TRANSIENT`（502/504）、`CONNECTION_TRANSIENT`和阻塞性的`AUTH`。仅验证原生布局/profile分类，不等价于这些类别均已完成AUTO闭环；无`STREAM_TRANSIENT`样本。
- `local-artifacts/post-upgrade-native-auto-idle-1`：当前0.160.0原生空会话在测试身份/epoch门下AUTO观察约12秒、45样本，全部为IDLE+空编辑区；0 attempts、0文字/Enter调用、0 provider请求、0键盘事件，窗口已关闭。
- `local-artifacts/post-upgrade-native-auto-complete-1`、`auto-quoted-error-1`及`tests/fixtures/native_codex_0_160_iterm_3_7_3/response_cases`：本地loopback成功SSE生成原生Codex正常回答和引用错误文本两种终态；各自以AUTO观察12秒（48/49样本），无attempt、无自动文字/Enter、无键盘事件，测试窗口关闭。普通回答与缩进的`■ unexpected status 503...`引用均分类为`IDLE + PLACEHOLDER`、`normal_completion=true`、无错误类别。过程中确认当前Codex完成标记为`Worked for 2s • 15:36`，profile已增加该样式的精确识别；样本不是云端请求或真实工具执行。
- `local-artifacts/post-upgrade-native-tool-output-2`及`tests/fixtures/native_codex_0_160_iterm_3_7_3/tool_output_cases`：loopback fixture返回`exec_command`调用，原生Codex实际执行固定的只读`printf`，终端工具输出含`■ unexpected status 503...`和专用标记，随后收到本地正常完成响应。最终帧分类为`IDLE + PLACEHOLDER`、`normal_completion=true`、`category=""`、`ready=false`；1次工具输出回传、0脚本输入、0键盘事件，测试窗口关闭。它覆盖了原生shell工具输出负例，不是云端或真实供应商执行。
- `local-artifacts/post-upgrade-native-approval-1`和`approval-2`：历史两次探针均未捕获审批卡片。后续`native-approval-current-tab5`在tab 5通过原生身份与双门控捕获到待处理卡片；marker被复用且目标已存在，因此归档仅作审批布局/失败关闭分类证据，不计首次创建新文件验收。
- `local-artifacts/post-upgrade-native-observe-2h-1`：新版四个原生Codex隔离会话完整运行7200.042秒、1440样本；input_calls、disconnected_samples、paused_samples、unverifiable_samples均为0，全部会话保持OBSERVING，最终`complete=true`/`duration_gate=true`，测试窗口已关闭，观察进程已退出。
- 资源人工复核：RSS 14.05–33.92 MiB，中位28.52 MiB，首末差−4.05 MiB；CPU中位1.8%、P95 2.7%，最大89.5%仅出现在第一个预热采样，后续P95为2.7%。SQLite `integrity_check=ok`，4个绑定、0个attempt；日志339字节/2行、0条ERROR/CRITICAL。探测确认3.7.3仍运行，观察测试窗口ID已不在活动Session列表。
- 以上短测试窗口均已清理；生产兼容性注册表仍为空。长时间观察通过只证明四会话只读运行及资源稳定，不替代生产AUTO发布认证或真实供应商故障测试。

### 发布准入复核

**本次不把 `(3.7.3, 2.25, 5383ef71dd1bd8d2f3658c04a219e2cf165c7969aebd0cceced1bc9f0f68877f, codex-local-v1.1)` 写入生产注册表。** 虽然当前版本的本地模拟原生终态帧已覆盖`CAPACITY`、`RATE_LIMIT_TRANSIENT`、`UPSTREAM_TRANSIENT`和`CONNECTION_TRANSIENT`，原生AUTO故障恢复闭环仍只对`UPSTREAM_TRANSIENT`实测；`STREAM_TRANSIENT`缺少独立原生呈现证据。AUTO普通空闲、正常回答完成、回答正文引用503以及本地loopback驱动的原生shell工具输出负例均已证明零重试；真实审批UI的全新目标探针及用户批准后的空文件结果均已验证，但profile对该布局仍安全失败关闭。若当前放行，版本门仍会开放未完成原生AUTO闭环或审批布局验收的类别和布局。

已通过的目标组合证据：3.7.3/SDK2.25连接和样式读取、0.160.0身份与错误/占位/草稿样本、UPSTREAM_TRANSIENT单次自动恢复、两阶段物理键盘撤销、广播隔离、不确定发送不重放、四会话真实两小时只读观察。它们足以证明该组合具备运行基础，不足以证明所有默认重试类别均已完成发布验证。

解除该结论前仍需完成：

1. 评估是否存在独立的`STREAM_TRANSIENT`原生终态；中途SSE断流和标准`response.failed`事件都被渲染为`CONNECTION_TRANSIENT`。类别不匹配时必须保持UNKNOWN/暂停。
2. 全新目标审批卡片、失败关闭分类、用户手动批准及目标空文件结果均已验证并归档。该254列布局仍是`UNKNOWN/GEOMETRY_UNSUPPORTED`，不支持自动判断审批卡；助手/采集器不代替用户批准或拒绝。原生shell工具输出中含错误文字的本地loopback负例已通过并归档；它不能代替真实供应商会话。
3. 审阅更新后的fixture及测试报告，再决定是否登记该精确版本组合；真实供应商故障仍须与本地模拟证据分开报告，不应为验收故意对真实供应商制造429。

旧版第二批四会话观察已完成7200.036秒，1440样本、零输入/断连/暂停；但全部样本不可验证，只说明旧版观察时长完成，不替代新版验收。下方2026-09-28内容为历史记录。

日期：2026-09-28。状态：真实自动输入验收未完成。

| 项目 | 当前证据 | 结论 |
| --- | --- | --- |
| Python | 专用venv使用3.12.4 | 安装和离线测试可运行 |
| iTerm2宿主 | `/Applications/iTerm.app` Info.plist为3.4.23；进程列表确认该副本运行 | 未升级/修改设置 |
| Python SDK | venv安装iterm2 2.25 | SDK可导入；源码接口已检查 |
| API连接 | SDK实测connected=true、transport=unix，枚举7个现有Session（不读取屏幕） | 当前连接与认证已通过 |
| SDK样式接口 | 2.25具有style_at/faint；当前3.4.23隔离合成TUI返回styled_cells=0 | 暂无有效样式证据，不能依赖占位文字外观 |
| jobPid/TTY/前台组 | 原生Codex0.156.0 PID/TTY/前台组/二进制hash实测通过 | Darwin对非控制终端返回ENOTTY时使用/bin/ps的内核tpgid |
| iTerm2 Transaction | 隔离窗口读取成功；真实SDK中的合成TUI Sender两阶段输入成功 | 读取和发送事务已通过；原生AUTO仍待验证 |
| 键盘monitor来源 | 真实订阅成功；脚本发送中文/Enter未产生键盘事件 | 人工物理按键仍待验证；不使用屏蔽窗口 |
| 广播抑制 | 同一测试窗口两个Tab建立真实广播域；目标收到中文/CR，旁观Tab零输入 | 已通过，测试广播域已移除、测试窗口已关闭 |
| Codex UI profile | 已有0.156.0真实错误/输入样本；修正绝对光标坐标、footer和错误URL续行 | 3.4.23缺少样式，原生占位输入区仍为COMPOSER_UNVERIFIABLE |
| 自动发送 | `VERIFIED_COMBINATIONS`当前为空 | `enable/retry-current`明确拒绝 |

## 当前连接方式与授权

用户已经开启Python API；读取EnableAPIServer得到1。实际SDK连接成功，使用`~/Library/Application Support/iTerm2/private/socket`，而非TCP 1912。之前用TCP端口拒绝连接推断API未开启是诊断错误，该结论已撤回。请以SDK实际握手为准：

```bash
.venv/bin/python tools/probe_iterm.py
```

该探测只枚举Session ID，不读屏幕、不发送输入。返回`connected=true`、`transport=unix`、`iterm_version=3.4.23`。目前无需追加人工授权；SDK已能完成认证。若以后授权失效，再根据实际错误处理首次授权提示。

额外真实证据：`tools/probe_isolated_iterm.py`创建并关闭了自己拥有的临时测试窗口，读取屏幕、光标、jobPid/TTY成功。该窗口运行合成TUI，不代表真实Codex恢复通过。一次探测收到3个外部DEL字节，解析器正确判定USER_TEXT；脚本自身未调用输入API，未把该样本当成空输入框成功证据。

`tools/live_sender_smoke.py --broadcast`在真实SDK中跑完整Sender：恰好收到`e7bba7e7bbad0d`（继续＋CR），持久化结果RESUMED；旁观Tab零输入。该测试使用受限测试身份注入，生产进程检查仍拒绝Python。

原生Codex测试使用本地HTTP 503模拟供应商故障：手动测试路径输入“继续”，两次核对编辑区后提交单次CR，原生客户端发起新请求并显示新的失败。该结果证明中文输入与提交可用，**不证明生产AUTO的空输入保护已通过**。样本在`tests/fixtures/native_codex_0_156_iterm_3_4_23`，provider_source明确为local_simulated_503。

当前下一步需要在提供样式的宿主版本上继续校准。官方3.7.3已下载并验签，尚未安装；见[升级准备](ITERM_UPGRADE_PLAN.md)。

## 获得自动模式支持的验收顺序

1. API连接、会话枚举与原生进程身份已通过；新版上的完整doctor仍需验证。
2. 只在明确指定的测试Session采集真实Codex空闲/忙碌/错误/用户输入/菜单样本。
3. 校准当前UI profile；验证样式缺失时的保守降级，不能凭占位文字内容判断空输入。
4. 在可丢弃的iTerm2测试会话中运行`tools/fake_codex_tui.py`，通过依赖注入测试发送；生产进程允许列表继续拒绝该Python模拟器。
5. 验证广播不会波及另一个测试会话、真实用户活动会撤销待输入、事务不会死锁。
6. 在专用原生Codex会话验证中文短提示词与单次CR；记录实际二进制SHA-256、SDK、iTerm2版本和profile revision。
7. 测试报告具备上述证据后，开发者才把准确组合加入`src/cliretry/compatibility.py`。不得只为了通过enable而填入未经验证的组合。

## 四会话资源观察

API开启后，在明确指定的四个隔离原生Codex会话中运行；其中的可执行文件必须匹配`--executable`。输出目录必须尚不存在。该工具从adapter边界拒绝输入，记录CPU/RSS和状态，不保存屏幕全文：

```bash
.venv/bin/python tools/soak_observe.py \
  --session '<TEST_SESSION_1>' --session '<TEST_SESSION_2>' \
  --session '<TEST_SESSION_3>' --session '<TEST_SESSION_4>' \
  --executable '<NATIVE_CODEX_PATH>' --duration 7200 \
  --output-dir local-artifacts/live-observe-2h
```

产物为`summary.json`、`resources.jsonl`及隔离的状态/日志目录。检查两小时完整时长、输入调用为0、monitor和连接状态、SQLite完整性、日志大小，以及CPU/RSS趋势。`duration_gate=true`只代表数量与时长满足，不能自动判定所有资源指标通过。第一批于160秒发现回调问题后停止，修复后第二批正在运行；详见TEST_REPORT。

## 实现与设计的接口说明

- iTerm2 SDK 2.25的`Connection.async_create()`在async函数内部同步执行AppleScript认证，可能阻塞daemon控制接口。adapter把认证放入单独线程，并用已检查的`_get_connect_coro`与`_async_dispatch_forever`建立传输/通知循环；因此固定SDK版本。未自行实现认证、未记录cookie。
- 传输建立沿用SDK的有界刷新语义：旧cookie遇到401时调用authenticate(True)，成功后仅再连接一次；新cookie被拒绝、刷新失败和非401均直接交由daemon处理。认证等待取消/超时不重复创建仍在运行的线程。该路径有离线回归；本机Unix socket实测未拒绝测试cookie，尚无真实401证据。
- daemon在每次重连时重建App singleton和monitor，不复用旧Session对象；宿主版本优先从App的appBundlePath读取。旧版宿主不提供该变量时，仅在系统恰有一个运行中的iTerm2应用bundle时读取其Info.plist；多副本无法消歧时保留unknown。
- 主动关闭连接时移除当前连接的SDK全局App回调，取消并等待接收/通知任务，再关闭WebSocket。真实四会话试跑发现旧回调会在重连后访问已关闭的连接；修正后短时四会话创建/重连/清理无此异常，随后重新开始两小时观察。
- SDK通知任务异常会使connected变为false，交由daemon撤销待发送并重连，避免任务异常无人处理。
- mutable-area快照的cursor是绝对buffer坐标，需减去windowed_coord_range.start.y。旧宿主的num_lines_above_screen字段缺失时为0，不能用它判定屏幕顶端或滚动。
- 生产实现将发送effect视为该Session状态的临时唯一所有者；polling在发送期间不修改该绑定，控制/键盘回调可立即提升revocation latch。发送每个await后检查latch；不使用会吞掉真实按键的时间窗口。
- 自定义错误规则使用受限正则：`^`和`$`锚定、普通文字、转义标点、最多一个`.*`或`.+`。不支持组/回溯引用/交替，避免本地规则让监控线程长时间卡住。
