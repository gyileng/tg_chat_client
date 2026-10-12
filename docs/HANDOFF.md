# 会话交接

## 设置、程序菜单点击无响应修复（2026-10-12）

用户确认顶部“设置、程序”点击后下拉菜单完全不出现。根因是 `ShortcutFilter` 继承 `QWidget`：作为主窗口子控件随窗口显示，默认区域覆盖左上角菜单栏，鼠标点击命中过滤器控件而不是菜单。新增四项回归在修复前全部失败，`QApplication.widgetAt` 均返回 `ShortcutFilter`。此前增加菜单栏将连接设置按钮下移，避开遮挡，却没有移除遮挡本身。

改为继承 `QObject`，保留应用级快捷键过滤和父子生命周期，避免生成界面控件；原菜单栏、连接设置按钮与快捷键保持原行为。新增测试按窗口坐标派发鼠标事件，覆盖两个菜单反复展开、托盘隐藏恢复后展开、设置子项打开与重开、程序子项保存草稿并真正退出。旧测试直接向目标控件发送点击，会绕过遮挡命中，未能发现此问题。

macOS Python 3.12、PySide6 6.12.0、Qt offscreen 下完整 **154 项测试及 2 项子测试通过**；Ruff 静态、格式和 `git diff --check` 通过。验证命令：`rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest -q`，Ruff 使用同一临时环境。未连接 Telegram、未构建或发布 Windows 包；需新版 Windows 包复核顶部菜单点击与快捷键，源码修复不等同于目标环境验收。

## 消息缓存、媒体占位与后台资源优化（2026-10-10）

按用户确认继续使用 Python/PySide6/Telethon，所有媒体显示类型占位及附带文字，不下载、解码或缩放预览。移除真实/模拟 preview 操作、ChatMessage.preview_path、预览请求与计时器、启动图片缓存扫描；旧版缓存不读取、不自动删除，账号移除仍按原有规则清理其本地文件。无需数据库迁移，不影响独立登录会话。

新增 `tg_chat_client/message_cache.py`：每聊天最近 500 条、历史浏览窗口 500 条，最多 10 个最近访问聊天，非当前聊天按访问顺序淘汰，实时消息不改变访问顺序；未访问且未缓存的群不积累消息。历史分页每次 50 条，前插时从尾部裁剪；新消息只进入最近缓存，不挤掉历史。淘汰同时清理浏览锚点和请求标识，不删草稿、引用与计数。回到底部切回最近窗口，并忽略被放弃的旧历史回执。

新增 `tg_chat_client/message_view.py`：QListView、增量消息模型和绘制委托，移除逐消息 QWidget。保留左右气泡、姓名固定配色、纯文本昵称/正文、附带文字、媒体占位及双击/右键引用。文字布局最多 500 项，随消息淘汰与宽度变化释放；消息合并刷新为前台 16 ms 单次计时器。按消息 ID 与行内偏移保持浏览位置，几何变化导致的滚动条变化不误判为手动浏览；宽窄截图已检查。

托盘隐藏/最小化停止模型刷新、绘制、排版与倒计时；连接、回执、缓存与登录继续处理。后台失败先恢复 SQLite 草稿，延后输入框刷新，直接退出也不允许旧输入覆盖已恢复内容。状态提示、账号/短句面板延后刷新，恢复时一次同步；发送成功仍回到底部。Store.counts_for_accounts 单次查询返回账号/群计数快照；普通消息不查询，启动/切换/账号变更/发送成功/北京时间跨日标记重载，前台限流才开启每秒倒计时。

macOS Python 3.12、Qt offscreen 完整 **150 项测试及 2 项子测试通过**；Ruff 静态、格式和 git diff --check 检查通过。新增覆盖五账号各 10,000 条消息的上限、LRU/未缓存群/去重、历史窗口与锚点、迟到分页回执、模型增量通知、全部媒体占位、隐藏/最小化不绘制不查询、倒计时、跨日、后台失败恢复及直接退出、缩放时底部跟随。应用子进程测试检查旧缓存不被删除，原预览取消测试改为历史请求取消，保留账号清理保护。

验证命令：`rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest -q -o faulthandler_timeout=30`；截图 `/private/tmp/tg-chat-optimized.png`、`/private/tmp/tg-chat-optimized-narrow.png`。离线压力脚本 `scripts/benchmark.py` 支持 Windows 进程内存及包大小、前台/后台 CPU、切换中位数/P95；同脚本可用 --source-root 对照旧版。Windows workflow 构建后运行并上传 performance-after.json。本机模拟数据不代表 Windows 成品或真实网络性能；未连接 Telegram、未构建或发布 Windows 包。

同机五账号各 1,000 条模拟消息：旧版/新版接收后 RSS 197.9/127.9 MB，30 次切换中位数 2,602.5/54.6 ms。新版每账号 10,000 条时 RSS 在 128.05–128.13 MB 稳定，最近缓存合计 2,500 条；旧版万条对照未完成，不用于提升比例。详见 [性能记录](PERFORMANCE.md)，包含原始结果位置、环境、复现命令与局限。

## 系统托盘常驻（2026-10-10）

按用户确认增加系统托盘：启动显示主窗口与本地绘制的蓝色纸飞机图标，悬停区分模拟/真实模式。× 保存草稿与浏览位置并隐藏，账号连接、消息接收及已提交发送继续；托盘单击、双击或右键“显示主窗口”恢复。最小化仍留在任务栏。设置与登录窗口同步隐藏恢复，隐藏期间继续处理登录回执，登录成功后不重新打开已完成的登录窗口。

托盘右键“退出”和主窗口“程序 → 退出”共用真正退出路径，保存草稿、停止计时器、断开后台事件、移除快捷键过滤器和托盘图标、关闭服务，不撤销 Telegram 登录。清理幂等，迟到事件保护保留；发送中退出沿用现有不确定结果处理。无托盘时 × 正常退出；首次隐藏提示操作方法，以 settings 中 tray_hint_seen 持久化，模拟与真实数据分隔，无数据库迁移。

应用处理 Quit 事件时先标记真正退出，再允许 Qt 关闭窗口；仅连接 aboutToQuit 会被隐藏到托盘逻辑拦截，新增子进程测试已复现并验证修复。同时连接系统会话提交与 aboutToQuit 清理，--smoke-test 使用真正退出入口。已有界面测试改用 request_exit 清理；默认禁用宿主托盘检测，新增托盘测试独立模拟可用性，保证 Windows CI 可重复。

macOS Python 3.12、Qt offscreen 完整 **133 项测试及 2 项子测试通过**。覆盖草稿/引用/滚动恢复、后台发送与新草稿保留、对话框恢复、隐藏期间登录完成、首次提示、托盘不可用及退出清理；子进程测试覆盖带托盘的 smoke test、外部 QApplication.quit 与同目录再次启动。检查了图标预览 `/private/tmp/tg-chat-tray-icon.png`。验证命令为 `rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest -q -o faulthandler_timeout=30`；同环境 Ruff 静态、格式及 git diff --check 检查。

未连接 Telegram、未构建或发布 Windows 包。下一步构建新版 Windows 包，实测右下角托盘图标与菜单、点击恢复、设置/登录窗口隐藏恢复、发送中隐藏以及退出后重新启动；不把本机模拟通过标记为 Windows 验收完成。本轮不增加开机启动、全局快捷键或新消息通知。README、开发计划与包内说明已同步。

## 消息姓名配色与左右气泡（2026-10-10）

按用户确认实现：当前账号发出的消息靠右、浅绿色气泡；其他消息靠左、中性背景。姓名加粗，按 Telegram 发言人 ID 使用本地固定 12 色调色板，跨账号与重启保持一致；缺失 ID 使用灰色。自己的姓名后标注“（我）”，姓名已为“我”不重复标注。时间与回复编号在气泡底部显示为灰色。

ChatMessage 末尾增加可选 sender_id，历史、实时与发送回执读取消息对象身份，不增加发送后的网络查询，无数据库迁移。气泡按内容收缩，最大为行内可用宽度的 75%；长文本与昵称换行，预览随窗口缩放。行内气泡及空白处点击由列表处理，保留双击、右键引用；窗口缩放保留底部跟随或当前滚动值，关闭时停止新增排版计时器。模拟模式增加多位固定身份群友及自己消息。

macOS Python 3.12、Qt offscreen 完整 123 项测试及 2 项子测试通过；Ruff 静态、格式与 git diff --check 通过。新增检查覆盖左右布局、纯文本昵称、双击/右键引用、窄窗口长消息与预览、滚动保持、缺失身份及姓名查询失败、发送回执不增加网络查询。使用 `/private/tmp/tg-chat-venv/bin/python -m pytest -q -o faulthandler_timeout=30`，环境变量 QT_QPA_PLATFORM=offscreen，所有命令经 rtk 执行。

已查看模拟界面截图 `/private/tmp/tg-chat-bubbles.png`，左右布局、姓名颜色和长消息换行正常。未连接 Telegram、未构建 Windows 包；仍需新版 Windows 实测气泡、引用和真实身份配色。

## Windows CI 关闭窗口迟到事件修复（2026-10-10）

用户提供 Windows CI：114 项通过、启动已有账号测试失败，群选择回调在服务关闭后提交历史请求，抛出“服务已经关闭”。本机新增确定性回归复现同样堆栈，另复现窗口立即关闭后延迟启动回调提交请求的同类错误。

窗口关闭前标记 closing，断开后台事件连接并停止计时器；已排队的后台回执、延迟启动/设置回调、账号/群切换和加载回调在关闭后直接返回，重复关闭不重复清理。保留 ServiceBridge 对关闭后提交请求的异常保护，不吞掉错误，也不靠延长测试等待规避竞态。

macOS Python 3.12、Qt offscreen 下完整 117 项测试及 2 项子测试通过；Ruff 静态与格式检查、`git diff --check` 通过。命令：`rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest -q`，Ruff 使用同一临时环境。待推送后的 Windows CI 验证，尚未构建本次 Windows 包。

## 连接设置临时诊断清理（2026-10-10）

用户确认连接设置点击问题已修复，移除按钮按下、鼠标命中区域、界面启用/焦点状态和设置窗口请求/阻止/打开/关闭的临时日志；同时删除对应鼠标诊断分支及无用导入。保留实际修复、菜单及 Ctrl+, 入口、键盘焦点、启动日志、脱敏异常日志与错误弹窗。设置窗口测试继续验证点击打开、可见、复用、Tab 与快捷键行为，去掉临时日志断言。历史日志文件未删除。

macOS Python 3.12、Qt offscreen 下 `tests/test_ui.py tests/test_app.py` 共 75 项测试通过（`rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest tests/test_ui.py tests/test_app.py -q`）；同环境 Ruff 静态与格式检查、`git diff --check` 通过。未构建新版 Windows 包。

## 回车发送残留修复（2026-10-10）

用户反馈回车发送反应慢，连续短句会残留。原实现等待网络成功才清空输入，期间新输入会接在旧文本后；再次回车在发送未结束时被静默忽略。

改为手动提交后立即清空输入与引用，成功回执不重载新草稿，连续输入相同文本也不误清除。保持同一账号和群仅一个未完成发送，再按 Enter 明确提示保留草稿，完成后需手动再次发送，不排队。失败及不确定结果将原文本恢复到对应聊天草稿开头，保留后续输入及新引用，提示先核对群消息，不自动重发。无新草稿时恢复原引用；切换账号后仅恢复原聊天，不改变当前输入。过期回执不能解除下一条请求的发送保护。

macOS Python 3.12、Qt offscreen 下 115 项测试及 2 项子测试通过，Ruff 静态与格式检查、补丁空白检查通过。新增回归覆盖即时清空、相同下一句及引用保留、等待时提示、连续手动触发、失败恢复、跨账号恢复与重复结果。验证使用 `/private/tmp/tg-chat-venv/bin/python -m pytest -q`（`QT_QPA_PLATFORM=offscreen`）、同环境 Ruff 和 `git diff --check`，均通过 `rtk` 执行。未连接 Telegram，未构建 Windows 包；仍需新版 Windows 实测网络发送及输入法。

## 设置鼠标路径与微信输入法会话清理（2026-10-10，最新反馈）

用户给出的真实日志只有启动与原生取消成功，反复点击设置没有按钮按下记录。随后确认从“添加 / 重新登录”按 Shift+Tab 再按空格能打开设置，说明设置窗口与回调可工作，鼠标点击路径仍待定位；不能再将问题归为窗口打开失败。是否被原生输入法窗口遮挡尚无证据。

新增“设置 → 连接设置”菜单和 Ctrl+, 入口，显式设置按钮 StrongFocus，增加界面启动时的启用状态/焦点策略记录（diagnostics=2），应用事件过滤器记录设置按钮区域收到的鼠标事件对象类型与启用/可见状态，不记录文本内容。新增 Tab/Shift+Tab、空格打开、快捷键打开及 Qt widgetAt 命中回归。本机鼠标正常，不代表 Windows 原点击路径已修复。

微信输入法在 CPS_CANCEL 返回成功后仍有候选。现增加 NI_CLOSECANDIDATE，明确关闭候选列表；输入框临时禁用输入法并失焦一轮事件循环，再恢复编辑焦点，整段操作拦截旧候选提交。若用户已切到其他输入框，恢复不抢焦点。依据 [Microsoft 候选列表关闭接口](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immnotifyime) 和 [Qt 输入焦点重置说明](https://doc.qt.io/qt-6/qinputmethod.html#reset)。补充原生双操作调用、失焦时旧提交保护和焦点恢复/不抢焦点回归。

macOS Python 3.12、Qt offscreen 下 **110 项测试及 2 项子测试通过**，Ruff 静态、格式及补丁空白检查通过。待新版 Windows 微信输入法实测，不能将模拟 API 成功和本机焦点测试当成候选清理验收。

## Windows 连接设置与微信输入法候选残留（2026-10-10）

用户在最新 exe 再次反馈“连接设置”点击无反应、日志空白，但主窗口其他操作正常；微信输入法已能即时替换，候选仍残留且空格可再次提交。此前仅处理同步重入，未覆盖替换回调返回后的候选提交；本机未复现 Windows 设置按钮失效。

连接设置按钮改为独立引用与显式 lambda 回调，设置窗口由主窗口持有，使用 `show()` 和窗口模态代替临时 `exec()`，居中并激活；保存、取消与再次打开保留原行为。日志保留第三方 WARNING 限制，仅本程序启用 INFO，强制安装文件处理器，增加启动、按钮按下、请求打开、显示和关闭记录，不含配置值。

输入法清理延迟到当前事件结束后执行，期间拦截旧候选提交。输入框仍有焦点时，执行 Qt reset，再通过 Windows `ImmNotifyIME(NI_COMPOSITIONSTR, CPS_CANCEL)` 取消原生组合状态；不切换输入法或语言。句柄使用 64 位兼容类型，取得的上下文通过 finally 释放。调用依据 [Microsoft 文档](https://learn.microsoft.com/en-us/windows/win32/api/immdev/nf-immdev-immnotifyime)。输入框失焦后不重置其他控件的输入法。

新增窗口持有/可见/日志回归、异步候选提交拦截、清理后普通空格与中文输入，以及原生 API 句柄和资源释放测试。macOS Python 3.12、Qt offscreen 下 **107 项测试及 2 项子测试通过**，Ruff 静态、格式和补丁空白检查通过，源码模拟启动/退出 smoke test 通过且日志有启动记录。当前本机验证不代表 Windows 或微信输入法验收；仍需新版实测。如果设置仍无反应，用新增 `Connection settings` 日志判断点击与显示停在哪一步。

## 中文输入法候选状态即时替换（2026-10-10，当前交互）

用户要求中文输入法候选状态下，完整“符号＋字母缩写”就立即替换，无需切英文或提交候选。原实现只在普通按键路径展开，输入法提交英文缩写不展开已由新增测试复现。现处理输入法候选与提交事件：候选字母结合光标前的触发前缀匹配，完整命中后重置候选状态、插入触发串并替换正文；支持候选字母间的空格和英文单引号分隔，未匹配拼音保持原流程。重置期间忽略重入事件，避免重复提交候选。英文提交路径也可展开。

回归覆盖中英文分号、自定义前缀、大小写、拼音分隔、Unicode 光标、一次撤销恢复触发串、引用与草稿、前缀提交和候选同事件、替换不递归、正常中文继续输入、一次 Enter 发送及重置重入保护。macOS Python 3.12、Qt offscreen 下 **102 项测试及 2 项子测试通过**，Ruff 静态、格式检查通过。README、开发计划、包内说明同步。本轮未构建 Windows exe，微软拼音及用户实际输入法仍需新版实测。下方历史“不在输入法组合期间展开”已由本节替代。

## exe 连接设置无响应的进一步排查（2026-10-10）

用户再次报告运行 exe、填写 API 参数后左上角按钮点击无反应。当前源码按钮名称为“连接设置”，尚未确认用户运行的构建版本。新增首次启动无账号、自动打开设置、保存后重新点击按钮的真实模式界面回归，使用离线桥不连接 Telegram；测试通过，原问题仍未复现。

Windows 无控制台程序的 Qt 回调异常可能只写 stderr，表现为静默失败。现注册界面异常处理器，弹出异常类型及日志位置，将异常类型、源文件名、行号和函数名写入本地日志；不记录异常正文、源码行或局部变量。退出时恢复原异常处理器。新增真实 Qt 按钮抛异常回归，验证弹窗和日志均不泄露模拟私密异常正文。此改动用于排查，不代表已确认此次问题由回调异常导致。

macOS Python 3.12、Qt offscreen 下 **89 项测试及 2 项子测试通过**，Ruff 静态、格式及补丁空白检查通过。未联网、未构建 Windows exe。下一步确认用户 exe 构建版本与顶部提示，构建新版后依据弹窗和 `%LOCALAPPDATA%/TGChatClient/real/logs/client.log` 的脱敏错误位置继续定位。

## 连接设置反馈与端口修改验证（2026-10-10）

用户报告保存配置后再次点击连接设置无反应。本机尚未复现该现象：新增真实模式按钮回归（使用模拟服务、不联网）验证已有配置可重新打开，修改端口并保存会触发 reconnect，再次打开显示新端口，取消不修改数据也不重连。

原代码在模拟模式、发送中或账号管理中只显示顶部提示，现增加明确弹窗；登录窗口打开时也阻止改配置，避免保存成功却被重连保护拦截。新增四种阻止状态回归，README 和包内说明补充修改端口步骤。macOS Python 3.12.13、Qt offscreen 下 **87 项测试及 2 项子测试通过**，Ruff 静态、格式和补丁空白检查通过。未连接 Telegram、未构建 Windows 包；用户实际“点击无反应”的原因仍待其模式、提示或错误信息确认，不应将提示改进描述为已复现并修复原问题。

## 短句改为即时文本替换（2026-10-10，当前交互）

按用户反馈取消空格确认，保留符号前缀＋缩写：键入完整 `;+wz` 等触发串后立即替换正文，仍须 Enter 或发送按钮手动发送。空格正常输入。添加/编辑短句的表单直接拦截重复和互为前缀的缩写（大小写不区分，例如 `wz` 与 `wzd`），允许修正后提交；编辑排除自身，保存整库再次校验且失败不修改原库。

旧库若存在互为前缀的缩写，保留正文与缩写，暂停冲突项的自动替换并在面板提示修改，其他缩写及点击插入照常工作。仍保留中英文分号等价、句中替换、Unicode 光标、一次撤销恢复完整触发串、引用和草稿保护；输入法组合、选区、长按、粘贴、加载草稿及替换正文不触发自动替换。下方空格展开记录为历史，以本节为准。

macOS Python 3.12.13、PySide6 6.12.0、Qt offscreen 下 **82 项测试及 2 项子测试通过**，Ruff 静态、格式检查及补丁空白检查通过。新增即时替换、普通空格、旧库冲突保护、表单当场拦截与编辑自身回归，并更新原展开测试及持久化校验。README、开发计划、任务状态与包内说明已同步。本轮未连接 Telegram、未构建 Windows 包；下一步构建新版后复核 Windows 输入法与添加/编辑提示。验证命令同下节。

## 已保存账号启动时报错修复（2026-10-10）

用户在 Windows 启动真实模式时报告 `MainWindow` 缺少 `preview_timer`。已有账号会在构造窗口的 `refresh_accounts()` 中被立即选中，同步触发 `account_changed()` 和 `render_messages()`，而预览计时器原本在账号刷新之后才创建。现将预览计时器创建及信号连接提前到界面构建和账号刷新之前。

新增预置账号启动回归，修复前复现相同 AttributeError，修复后正常选中账号并启动预览计时器。macOS Python 3.12.13、PySide6 6.12.0、Qt offscreen 下 79 项测试及 2 项子测试通过，Ruff 静态与格式检查、补丁空白检查通过。未连接真实 Telegram、未在 Windows 实测或构建新版包；验证命令同下节。

## 发送后滚动与中文分号修复（2026-10-10）

当前聊天发送成功后追加消息并回到底部；查看历史时收到普通新消息仍保留阅读位置，其他账号或群的发送结果不改变当前聊天滚动。短句前缀中的英文 `;` 与中文 `；` 等价匹配，默认 `;nh` 和 `；nh` 均可按空格展开；自定义组合前缀同样支持分号互换，其他符号保持精确匹配。

新增回归覆盖底部发送、历史位置发送、收到消息保留位置、跨账号发送结果隔离，以及中文分号的展开、未知缩写、Unicode 光标和撤销。README、开发计划及包内说明已同步。本机 macOS Python 3.12.13、PySide6 6.12.0、Qt offscreen 下 78 项测试及 2 项子测试通过，Ruff 静态与格式检查、补丁空白检查通过。首轮已有图片预览测试因文件创建与读取时序失败，复跑整套通过；新增跨账号滚动测试改为在历史加载结束后注入发送结果，避免异步历史刷新干扰断言。本轮未连接 Telegram、未构建 Windows 包；Windows 输入法及新版包仍待实测。

```bash
rtk proxy env QT_QPA_PLATFORM=offscreen /private/tmp/tg-chat-venv/bin/python -m pytest -q
rtk proxy /private/tmp/tg-chat-venv/bin/ruff check .
rtk proxy /private/tmp/tg-chat-venv/bin/ruff format --check .
rtk proxy git diff --check
```

## 验证码请求报错修复（2026-10-10）

用户报告配置本地混合代理端口后，获取验证码显示 `Request was unsuccessful 1 time(s)`。源码将 Telethon 的 `request_retries` 设为 0；Telegram 返回数据中心迁移要求时，Telethon 切换 DC 后无法继续请求，并用通用 ValueError 隐藏原始原因。该缺陷已离线复现，但未确认用户此次失败的底层原因，亦未连接用户代理或真实账号。

保持消息请求不自动重试，新增 `raise_last_call_error=True` 暴露真实 RPC 错误。验证码请求仅在 Telethon 已处理 DC 迁移后继续，最多请求三次；网络异常、限流不自动重试，失败清理待登录状态。真实 Telethon 请求循环回归覆盖迁移后成功，另覆盖网络/限流不重试和迁移次数上限。

本机 Python 3.14.4、Telethon 1.44.0 下 35 项核心与服务测试通过，补丁空白检查通过。此环境缺少 python-socks，测试使用模拟依赖与网络响应，不建立连接；不代表目标 Python 3.12、真实代理或 Windows 登录验收。尚未构建新版 Windows 包。

```bash
rtk proxy env PYTHONPYCACHEPREFIX=/private/tmp/tg-chat-pycache /Users/baiweispace/Documents/self/tg_bot/.venv/bin/python -m unittest tests.test_core tests.test_service -q
rtk proxy git diff --check
```

下一步构建新版 Windows 包；若代理与应用在同一台电脑，确认代理混合端口后使用 SOCKS5、127.0.0.1 和用户配置的端口验证。失败时依据新版具体错误排查，不把通用错误直接认定为代理故障。

## 短句缩写展开（2026-10-10，当前交互）

用户确认取消短句 `Ctrl+1…9`，保留点击插入与 `Alt+1…9` 切账号。管理短句新增共用前缀与每条可选缩写：默认前缀 `;`，输入 `;nh` 后按普通空格精确展开，消耗确认空格，再按 Enter 或发送按钮手动发送。缩写使用英文字母、数字和下划线，不区分大小写且不可重复。前缀可改为一个或多个非空白符号；所有账号共用，重启保留。右侧面板显示触发串及正文，点击仍在光标处插入或替换选区。

只替换光标前的触发串，保留前后文字和引用，一次撤销恢复。无匹配时正常输入空格；输入法组合、选区、长按、粘贴及加载草稿不触发展开。`nh` 与 `nhm` 可共存，光标位于较长缩写内部时不展开。展开后的正文不递归展开。发送中仍可编辑和展开，成功结果不覆盖新草稿。SQLite 从版本 1 事务迁移至版本 2，旧短句正文、排序保持，缩写留空由用户补填；更新后的数据库应使用新版程序打开。

Linux Python 3.12、Qt offscreen 下 **65 项测试通过**，Ruff 静态与格式检查、补丁空白检查通过。测试覆盖迁移、校验失败数据保留、重启、前缀、大小写、长短缩写、句中插入、Unicode 光标、撤销、输入法保护、粘贴、手动发送、账号切换与发送期间草稿。README、开发计划、任务状态及包内说明同步。

本轮未连接 Telegram、未操作真实账号，未构建 Windows 包。下一步用 Windows workflow 构建新版，验证中文输入法空格确认、自定义前缀、短句管理和五账号切换。下方旧 Ctrl 数字行为仅为历史记录，以本节为准。验证命令沿用临时环境：

```bash
rtk proxy env QT_QPA_PLATFORM=offscreen /tmp/tg-chat-venv/bin/python -m pytest -q
rtk proxy /tmp/tg-chat-venv/bin/ruff check .
rtk proxy /tmp/tg-chat-venv/bin/ruff format --check .
rtk proxy git diff --check
```

## 账号管理补充（2026-10-10）

用户要求补上退出登录和删除账号。账号列表右键提供“退出登录”和“移除账号”，均通过确认对话框操作。退出调用 Telethon `log_out` 撤销本工具会话并清理会话文件，保留账号、草稿、常用群和统计；移除先退出，再清除该账号的数据库记录、会话与缓存，不影响其他账号和共享短句。退出失败保留数据，关闭连接并提示重新连接后重试。

界面及服务层保护发送和账号管理并发；退出/移除前取消目标账号的群列表、历史与预览请求。处理移除当前账号、最后一个账号和过期结果；退出账号无法发送。模拟退出状态持久化，全部移除后重启不重新生成账号。模拟模式不能重新登录，真实模式仍通过顶部“添加 / 重新登录”恢复账号。

Linux Python 3.12、Qt offscreen 下 54 项测试通过；Ruff 静态与格式检查、补丁空白检查通过。新增 12 项回归覆盖单账号清理与其他账号隔离、路径保护、退出失败保留数据、会话不自动恢复、发送保护、预览取消、模拟重启、取消确认、删除当前/最后/其他账号和界面失败反馈。README、开发计划与包内说明同步。本轮未连接 Telegram、未操作真实账号、未打包或推送；Windows 右键交互、真实退出撤销及重新登录仍待验收。验证沿用下方临时环境命令。

## 最新调整与验证（2026-10-10）

用户已在 Windows 体验模拟模式，确认改用 `Alt+1…9` 切换账号、`Ctrl+1…9` 插入快捷短句，并将短句面板放在窗口右侧。点击与快捷键均在光标处插入，有选区时替换选区，保留其余草稿和引用，焦点返回输入框；仍由 Enter 或发送按钮手动发送。

右侧面板常驻、可滚动、可拖动分隔线调宽；悬停查看完整内容，超过九条仍可点击。发送中及限流等待时仍可编辑；发送成功仅清除与提交内容一致的草稿。忽略快捷键长按重复、输入法组合及对话框内操作，组合期间避免保留键位的数字落入草稿。

Linux 本机 Python 3.12.3、PySide6 6.12.0 环境下 **42 项测试通过**；Ruff 静态检查、格式检查和补丁空白检查通过。已检查默认窗口及窄窗口长短句的模拟截图。新增回归覆盖光标插入、选区替换、两种触发方式、账号切换草稿、输入法与长按、对话框、第九个账号、短句排序、第十条短句和发送期间编辑。

依赖下载已恢复，uv.lock 由成功解析生成。本轮未连接 Telegram、未读取真实凭据、未创建提交或推送、未构建 Windows 包。README、开发计划与包内说明已同步；原始 RESEARCH 保留为历史调研。

```bash
rtk proxy env UV_CACHE_DIR=/tmp/tg-chat-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/tg-chat-python UV_PROJECT_ENVIRONMENT=/tmp/tg-chat-venv uv sync
rtk proxy env QT_QPA_PLATFORM=offscreen /tmp/tg-chat-venv/bin/python -m pytest -q
rtk proxy env /tmp/tg-chat-venv/bin/ruff check .
rtk proxy env /tmp/tg-chat-venv/bin/ruff format --check .
rtk proxy git diff --check
```

下一步使用 Windows workflow 构建新版包，复核 Windows 快捷键、中文输入、右侧布局及草稿保留。本机 offscreen 检查不代替 Windows 交互验收；真实代理、登录、群收发仍待验收。

## 前轮历史记录

以下保留前轮交接信息；旧快捷键、依赖与本机环境限制以本页最新记录为准。

### 前轮实现

第一版源码已建立：工程入口、Qt 聊天界面、模拟五账号、真实 Telethon 服务、跨线程桥、全局代理与登录设置、短句管理、引用回复、图片/贴纸缩略预览、SQLite 草稿和发送计数、单实例、缓存清理、脱敏日志。

已编写核心和 Qt 测试、Windows GitHub Actions、PyInstaller spec 与包内中文使用说明。既有 AGENTS.md 未修改，未读写相邻项目真实会话或凭据。

### 前轮验证

Windows CI 已由用户触发，Python 3.12.10 下 31 项测试中 30 项通过，只有新版数据库保护测试在临时目录清理时失败。原因是测试使用 `with sqlite3.connect(...)` 只结束事务，不关闭连接，Windows 因文件占用拒绝删除。已改用 `Store.connect()` 保证关闭连接，并增加版本仍为 2 的断言；待推送后重新运行 Windows CI。Qt 的六项自动测试已通过，界面视觉、真实账号及打包运行仍待验证。

- 语法检查通过，源码可由本机 Python 解析。
- Ruff 0.16.9 从本机已有缓存执行；`ruff check .` 和 `ruff format --check .` 均通过。
- **25 项核心测试通过**，使用相邻项目已安装的 Python 3.14.4 + Telethon 环境执行，不建立 Telegram 连接。该环境只用作离线逻辑验证，不代表目标 Python 3.12 已验收。
- 本机未执行 Qt 测试和界面视觉检查：没有 PySide6，依赖下载失败。Windows CI 的 Qt 自动测试已通过。
- Windows CI 已运行到测试阶段；构建和真实代理、登录、群收发尚未完成。

### 前轮环境限制

`uv sync` 使用可写临时缓存后仍因 GitHub DNS 解析失败而无法下载 Python。另尝试使用已有 Python 建立临时环境，但 PyPI 同样无法解析，未安装 Qt。默认用户缓存亦不可写；在本环境重试可使用：

```bash
rtk proxy env UV_CACHE_DIR=/private/tmp/tg-chat-uv-cache UV_PYTHON_INSTALL_DIR=/private/tmp/tg-chat-python uv sync
```

`git init` 因 `.git: Operation not permitted` 失败。不要伪造 uv.lock、Git 提交或 Windows 构建产物。

### 前轮继续工作的入口

先读 DEVELOPMENT_PLAN.md 和 TASKS.md。优先恢复依赖并运行完整测试、模拟界面；记录截图和实际问题后再交付 Windows 包。

常规开发命令：

```bash
rtk uv sync
rtk uv run python app.py --mock
rtk uv run pytest -q
rtk uv run ruff check .
rtk uv run ruff format --check .
```

本机离线核心测试命令：

```bash
rtk proxy env PYTHONPYCACHEPREFIX=/private/tmp/tg-chat-pycache /Users/baiweispace/Documents/self/tg_bot/.venv/bin/python -m unittest tests.test_core tests.test_service -v
```

尚无可下载 Windows 成品；workflow 创建后需在真实远程仓库中运行。CI 首次可解析依赖并在构建产物中附带锁文件；随后将验证过的锁文件加入仓库，并改用锁定安装。
