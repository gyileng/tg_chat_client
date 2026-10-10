# 会话交接

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
