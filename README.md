# TG Chat Client

独立 Windows 多账号 Telegram 聊天工具，面向手动高频短句输入场景。

## 当前阶段

第一版源码、核心测试与 Windows 构建流程已建立。采用 Python 3.12、PySide6、Telethon 和 SQLite；界面实测、真实 Telegram 验收及 Windows 打包尚未完成。

后续会话先阅读 [开发计划](docs/DEVELOPMENT_PLAN.md)、[任务状态](docs/TASKS.md) 和 [会话交接](docs/HANDOFF.md)。原始 [调研与需求记录](docs/RESEARCH.md) 保留为背景资料，当前决定以开发计划为准。

## 开发与运行

```bash
rtk uv sync
rtk uv run python app.py --mock
rtk uv run python app.py
rtk uv run pytest -q
rtk uv run ruff check .
rtk uv run ruff format --check .
```

`--mock` 使用五个模拟账号，不连接 Telegram，数据与真实模式分开。真实模式首次填写 API ID、API Hash、代理，再添加账号。所有账号共用 SOCKS5/HTTP 代理；连接失败不切换直连。

源码位于 `tg_chat_client/`，测试位于 `tests/`，打包配置与说明位于 `packaging/`。默认 Windows 私有数据路径为 `%LOCALAPPDATA%/TGChatClient/real`。开发可用 `--data-dir <目录>` 指定数据位置，仍分隔模拟/真实模式。

Windows 包通过 `.github/workflows/windows.yml` 在 Windows x64 环境构建。须解压并保留整个程序目录；无需安装 Python。当前尚未产出已验收的 Windows 包。

## 操作

- `Ctrl+1…5` 切换账号；`Alt+1…9` 直接发送前九条短句。
- `Enter` 发送自由输入，`Shift+Enter` 换行；快捷键仅在主窗口前台生效。
- 双击消息或右键选择引用回复；短句也会使用当前引用对象。
- 管理短句窗口支持添加、编辑、删除、拖动排序，所有账号共用。
- 向上浏览暂停自动滚动；点击“回到底部”查看新消息。
- 今日条数仅统计本工具发送成功的消息，按北京时间零点划分，不代表群内认可数量。

## 已确定方向

- 一个窗口管理多个账号；账号保持连接，支持快速切换。
- 支持查看群消息、快捷短句，以及自由输入、编辑和手动发送。
- 每次发送由用户主动操作触发；自动循环、定时发送和大模型生成不属于当前第一版目标。
- 使用独立登录会话，与相邻的 `tg_bot` 服务分开管理。

## 文档

- [调研与需求记录](docs/RESEARCH.md)：场景、已确认需求、交互建议、已有代码参考和待验证问题。

## 本地数据

后续登录凭据、会话文件、账号信息和真实聊天内容必须作为私有本地数据管理，不进入版本库或调研文档。
