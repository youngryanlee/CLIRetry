# 测试报告

## 2026-10-07 全新目标原生审批探针

- tab 6映射到的真实 iTerm Session UUID 已脱敏；Codex 0.160.0身份SHA-256为`5383ef71dd1bd8d2f3658c04a219e2cf165c7969aebd0cceced1bc9f0f68877f`，iTerm2 3.7.3 / SDK 2.25。新标记与白名单审批文案同帧命中，捕获时审批待处理；采集过程没有按键或审批决策。
- 原始捕获`local-artifacts/native-approval-current-tab6.jsonl`权限0600；路径经等宽遮盖的fixture位于`tests/fixtures/native_codex_0_160_iterm_3_7_3/approval_cases/fresh_target_probe`，manifest记录捕获时的`target_state=new`、`approval_decisions=0`。捕获后用户手动批准；只读检查确认目标是普通0字节文件、非符号链接。分类为`UNKNOWN/GEOMETRY_UNSUPPORTED`、`ready=false`且无错误类别；生产注册表保持为空。
- 增加fresh-target原生fixture回归；完整测试`.venv/bin/python -m pytest -q`为`174 passed, 17 warnings in 20.75s`。`.venv/bin/ruff check src tests tools`及`.venv/bin/python -m compileall -q src tests tools`均通过；警告仍来自上游`websockets`和iTerm2 SDK弃用提示。

## 2026-10-05 审批采集门控

- `capture_fixture.py --approval-marker`要求明确Session、成功的原生Codex身份校验，以及同帧同时出现指定标记和白名单审批提示。标记限定为至少16位ASCII字母/数字/`-_`。只将满足双重屏幕门控的帧写入；普通会话/不命中时在创建任何capture或manifest之前退出。新增8个回归用例覆盖纯门控、标记格式、普通帧零落盘、仅写入命中帧及未验证身份不读屏。
- tab 5经iTerm2 API映射到的真实 Session UUID 已脱敏。进程参数确认`workspace-write`、`on-request`和`approvals_reviewer="user"`。同帧原生文案为“Would you like to run the following command?”及“Do you want to allow me...”，捕获时审批仍待处理，未批准/拒绝；身份SHA-256为`5383ef71dd1bd8d2f3658c04a219e2cf165c7969aebd0cceced1bc9f0f68877f`。
- `local-artifacts/native-approval-current-tab5.jsonl`为权限0600的原始捕获；`tools/export_native_fixture.py --kind approval`导出等宽路径遮盖后的`approval_cases` fixture。此探针误用了旧标记且目标文件此前已存在，因此只证明人工审批UI真实呈现和采集器门控通过，不证明新文件创建流程。默认profile对此254列画面给出`UNKNOWN/GEOMETRY_UNSUPPORTED`，`ready=false`、无错误类别；严格失败关闭。
- 新增原生审批fixture回归后，全套离线测试`173 passed, 17 warnings`；`.venv/bin/ruff check src tests tools`和`.venv/bin/python -m compileall -q src tests tools`通过。17项警告仍来自上游`websockets`与iTerm2 SDK弃用提示。生产兼容性注册表保持为空；不要批准当前卡片。

## 2026-10-04 最新进展

- 人工确认两轮物理x；两阶段撤销均PAUSED_USER且零Enter，见COMPATIBILITY产物索引。
- 新增`tools/native_auto_validation.py`及`capture_native_isolated.py --mock-error --auto-recover`：仅在工具新建原生Session使用临时Session/identity/epoch认证门，生产注册表不变。初始请求在本地等待BUSY基线，之后释放503，真实时钟按默认配置退避并通过生产daemon/Sender恢复一次，结果RESUMED。`post-upgrade-native-auto-2`完整成功且窗口关闭；第一轮测试查询列名错误，零输入，失败产物保留。
- 新增`capture_native_isolated.py --mock-tool-output`及`tools/export_native_fixture.py --kind tool-output`：loopback响应调用原生`exec_command`执行固定只读`printf`，输出中包含`■ unexpected status 503...`；最终Codex完成帧分类为IDLE/PLACEHOLDER、`normal_completion=true`、空错误类别、`ready=false`。一次本地工具输出回传，脚本输入/键盘事件均为0，测试窗口已关闭；fixture位于`tests/fixtures/native_codex_0_160_iterm_3_7_3/tool_output_cases`。此项是本地shell真实输出，不是云端供应商故障。
- 新增`capture_native_isolated.py --mock-stream-failed`：通过本地loopback发送Responses `response.failed`事件，错误文字为`Internal streaming error, please retry.`；原生Codex显示`stream disconnected before completion: ...`并分类`CONNECTION_TRANSIENT`，没有呈现独立的`STREAM_TRANSIENT`。零脚本输入/键盘事件；frame已归档到`stream_failed_cases`，生产注册表仍为空。
- 审批卡片仍未验收：两个loopback探针都没有出现Codex审批UI，不能计通过。首轮只为观察工作区外访问而创建的空`/tmp/cliretry-approval-probe-20261004`已确认归属本次测试并删除；第二轮目标文件未创建。两轮测试窗均关闭，未发送批准输入；不使用宿主级`require_escalated`来制造提示。
- 本轮完整离线测试`164 passed, 17 warnings`；`.venv/bin/ruff check src tests tools`和`.venv/bin/python -m compileall -q src tests tools`均通过。警告仍来自上游`websockets`与iTerm2 SDK枚举弃用。
- `tests/integration/test_native_auto_harness.py`覆盖该验收工具成功/回执丢失停止、资源清理和认证门恢复；这两项是模拟终端回归，不冒充实测。
- 新版四会话7200秒只读观察已启动：`local-artifacts/post-upgrade-native-observe-2h-1`，PID 91217。初始四会话均OBSERVING，无不可验证原因，仍须等最终产物和资源审阅。
- 更新：该新版观察现已完成7200.042秒/1440样本；输入、断连、暂停、不可验证均为0，SQLite integrity_check=ok，4个绑定/0次attempt，日志339字节且无ERROR/CRITICAL。RSS中位28.52 MiB，首末下降4.05 MiB；CPU中位1.8%、P95 2.7%，首个采样的89.5%为最大值。测试窗口已关闭，观察进程已退出。自动发送注册表仍为空。
- 以下2026-09-28记录为历史结果，不代表当前进程或最新构建。

日期：2026-09-28。这里只记录实际执行结果，不把计划或模拟等同于真实iTerm2验收。

## 已执行

- 专用venv安装：Python 3.12.4；iterm2 2.25、psutil 7.2.2、pytest 9.1.1。
- 最新完整测试：`145 passed, 17 warnings in 12.44s`。覆盖Darwin前台组、buffer绝对坐标、重连回调清理、真实原生fixture，以及新增9项认证刷新/等待取消回归；警告来自上游SDK枚举嵌套及websockets旧接口弃用。
- `ruff check src tests tools`和`python -m compileall -q src tools`通过。
- 实际Unix domain socket请求验证：pause、status、重复request_id、冲突request_id、存储失效后shutdown。
- 四会话虚拟8小时：48轮/会话、总计192次would_retry，OBSERVE零输入。使用FakeClock与预先分类的合成快照；不代表真实iTerm2已经运行8小时。
- 另一组加速故障回放覆盖8个虚拟小时：每小时断线基线失效、用户暂停、显式恢复/预算重置、两个成功恢复后链预算耗尽；跨7次存储关闭/重新打开保留计数，共16个attempt。此测试不代表真实进程崩溃或资源耐久结果。
- 16组合成fixture：空闲、忙碌、正常完成、容量不足、429、认证优先级、菜单、引用/工具/代码块、占位样式缺失、中文/emoji输入及软换行；全部经OBSERVE集成路径，输入调用为0。
- 发送阶段休眠/墙钟跳跃、用户撤销、5种未决阶段重启、监听异常/意外EOF/订阅失败、历史完成标记不重置故障链均有回归。
- 早期SDK探测确实返回过connection refused；后续仅检查TCP1912所得的“API仍未开启”结论已撤回。当前设置EnableAPIServer=1，SDK实际通过Unix socket连接并枚举7个Session，未读取现有工作会话的屏幕。
- 修复后的`tools/probe_iterm.py`输出connected=true、transport=unix、iterm_version=3.4.23、sdk_version=2.25；正常关闭不再打印连接异常堆栈。
- 新建的隔离测试窗口中，100×24屏幕、光标、硬换行和jobPid/TTY等变量读取成功，读取事务成功，测试窗口已关闭。合成TUI收到3个外部DEL字节，判定为USER_TEXT；脚本自身输入API调用为0。该数据仅是合成TUI在真实iTerm2中的采样，不是原生Codex fixture。
- API开启后的实际daemon smoke再次通过：启动、状态、重复实例拒绝、关闭均通过，connected=true，输入调用为0。

## 真实隔离测试新增证据

| 产物目录（均在local-artifacts下） | 实际结果 |
| --- | --- |
| live-sender-1 | 真正调用SDK输入，合成TUI收到e7bba7e7bbad0d（继续＋CR），Sender结果RESUMED |
| live-sender-broadcast-2 | 同一窗口两个Tab建立真实广播域；目标精确字节、旁观零输入；测试域和窗口已清理 |
| live-type-reply-loss-1 | 实际文字送达后模拟丢回复；仅收到中文，无CR；重开Store仍PAUSED_UNCONFIRMED/OBSERVE |
| live-submit-reply-loss-1 | 实际Enter送达后模拟丢回复；只收到一次中文＋CR；重开Store仍PAUSED_UNCONFIRMED/OBSERVE |
| native-capture-4 | 本机原生Codex0.156.0身份通过，Darwin ENOTTY已用内核tpgid方案处理 |
| native-manual-continue-1 | 本地HTTP503模拟故障；显式手动测试输入中文、两帧核对、单次CR后，原生客户端发起新请求；测试窗口已关闭 |
| live-native-observe-2h-1 | 发现SDK旧回调残留，于160秒停止，保留失败记录；不能算两小时通过 |
| live-native-observe-reconnect-check | 修复后4会话15秒创建/连接切换/清理完成，无旧回调异常；不是两小时门槛 |
| live-native-observe-2h-2 | 修复后重新开始4个原生会话的7200秒观察，尚在运行，待检查最终summary |
| live-stale-cookie-refresh.json | 真实Unix socket接受测试用无效cookie，直接返回101；连接成功、零输入，但预期401→刷新分支没有触发，因此该探测passed=false，不作为刷新成功的实测证据 |
| live-keyboard-timeout-before-1 | 人工验收工具1秒无人响应：零字节、零submit，HUMAN_INPUT_TIMEOUT，complete=false，窗口已关闭；不是人工按键通过 |
| live-keyboard-timeout-after-1 | 文字送达后1秒无人响应：仅“继续”、零submit，HUMAN_INPUT_TIMEOUT，complete=false，窗口已关闭；不是人工按键通过 |
| live-sender-keyboard-harness-regression-1 | 增加人工验收入口后，普通Sender真实SDK回归仍为精确中文＋单次CR、RESUMED；窗口已关闭 |

原生二进制SHA-256：`937b80b2bc2e8d0de6672ce98043d514b74dd53db7491e9771de02c091d924f9`。三个原生屏幕样本已导出到`tests/fixtures/native_codex_0_156_iterm_3_4_23`，来源标记captured，供应商错误来源另标local_simulated_503。不是实际云端503事件，也不是生产AUTO放行的证明。

当前宿主没有样式的原生空编辑区稳定返回COMPOSER_UNVERIFIABLE；这项保护应继续生效。为验证错误布局而注入样式的额外单测明确属于合成数据，不用于声明新版宿主支持。

## 构建和安装

- wheel构建成功：`local-artifacts/wheels/cliretry-0.1.0-py3-none-any.whl`。
- 最新wheel SHA-256：`37b075d7ec00f3ce4809f8e60ad882b599b0b5ddfc3653ea24d52046530fedb1`；包含Unix socket诊断、Darwin前台组、屏幕坐标、SDK订阅清理及有界401认证刷新修正。
- 全新`local-artifacts/install-smoke`虚拟环境安装成功；`pip check`返回`No broken requirements found.`。
- 从`/tmp`运行安装后的`cliretry --help`成功，未依赖项目目录中的源码导入。
- 最新wheel重新安装到该隔离venv后运行`tools/smoke_daemon.py`：daemon启动、空状态查询、重复实例拒绝、shutdown均通过，退出码0；API连接为true，会话输入调用为0。临时daemon已关闭。

## 当前测试层次

| 层次 | 当前结果 |
| --- | --- |
| 配置/分类/状态机/预算/持久化/发送的离线测试 | 最新145项通过 |
| Unix socket控制集成 | 已通过 |
| wheel构建、全新环境安装和真实daemon启停 | 已通过 |
| macOS进程属性逻辑 | 原生Codex身份和前台组已通过，异常边界仍有模拟测试 |
| 真实iTerm2 API、屏幕/光标/读取事务 | 已在自建隔离窗口实测通过 |
| Unicode发送、广播隔离和键盘monitor | 中文/CR、广播隔离及脚本输入来源已通过；人工按键待验证 |
| 原生Codex临时故障恢复 | 本地503后手动测试提交通过；生产AUTO与真实供应商故障未通过验收 |
| 4个真实只观察会话2小时资源检查 | 第二批正在运行，不能提前标记通过 |
| 真实捕获fixture | 3个原生屏幕样本；另外16组合成样本明确区分来源 |

`tools/soak_observe.py`记录CPU/RSS/状态；缺样式的UNKNOWN计入unverifiable_samples，不能把它解释为自动恢复可用。自动组合注册表为空；不能根据本报告放开AUTO。上述wheel已在本轮重建并重新安装，通过真实daemon启停检查（连接成功、零会话输入）。

运行中的第二批观察在401刷新修正前启动，进程继续使用其已加载代码；该批次不能证明后续认证刷新逻辑。新增离线测试覆盖旧cookie被401拒绝后成功刷新、刷新失败、第二次401、新cookie被拒绝、非401不刷新，以及取消认证等待后复用唯一工作线程。

## 重跑

```bash
.venv/bin/ruff check src tests tools
.venv/bin/pytest -q --disable-warnings
.venv/bin/python -m compileall -q src tools
```

自动模式保持关闭，直到COMPATIBILITY列出的真实验收完成。测试数量不代表设计文档T01–T46每一条都已经得到相同强度的证明；逐项差距见IMPLEMENTATION_AUDIT。
