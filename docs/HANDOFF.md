# 会话交接

## 本轮实现

第一版源码已建立：工程入口、Qt 聊天界面、模拟五账号、真实 Telethon 服务、跨线程桥、全局代理与登录设置、短句管理、引用回复、图片/贴纸缩略预览、SQLite 草稿和发送计数、单实例、缓存清理、脱敏日志。

已编写核心和 Qt 测试、Windows GitHub Actions、PyInstaller spec 与包内中文使用说明。既有 AGENTS.md 未修改，未读写相邻项目真实会话或凭据。

## 当前验证

Windows CI 已由用户触发，Python 3.12.10 下 31 项测试中 30 项通过，只有新版数据库保护测试在临时目录清理时失败。原因是测试使用 `with sqlite3.connect(...)` 只结束事务，不关闭连接，Windows 因文件占用拒绝删除。已改用 `Store.connect()` 保证关闭连接，并增加版本仍为 2 的断言；待推送后重新运行 Windows CI。Qt 的六项自动测试已通过，界面视觉、真实账号及打包运行仍待验证。

- 语法检查通过，源码可由本机 Python 解析。
- Ruff 0.16.9 从本机已有缓存执行；`ruff check .` 和 `ruff format --check .` 均通过。
- **25 项核心测试通过**，使用相邻项目已安装的 Python 3.14.4 + Telethon 环境执行，不建立 Telegram 连接。该环境只用作离线逻辑验证，不代表目标 Python 3.12 已验收。
- 本机未执行 Qt 测试和界面视觉检查：没有 PySide6，依赖下载失败。Windows CI 的 Qt 自动测试已通过。
- Windows CI 已运行到测试阶段；构建和真实代理、登录、群收发尚未完成。

## 环境限制

`uv sync` 使用可写临时缓存后仍因 GitHub DNS 解析失败而无法下载 Python。另尝试使用已有 Python 建立临时环境，但 PyPI 同样无法解析，未安装 Qt。默认用户缓存亦不可写；在本环境重试可使用：

```bash
rtk proxy env UV_CACHE_DIR=/private/tmp/tg-chat-uv-cache UV_PYTHON_INSTALL_DIR=/private/tmp/tg-chat-python uv sync
```

`git init` 因 `.git: Operation not permitted` 失败。不要伪造 uv.lock、Git 提交或 Windows 构建产物。

## 继续工作的入口

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
