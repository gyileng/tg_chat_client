# 资源优化验证记录

日期：2026-10-10。基线为 `fe876f0`，新版为本轮优化源码。环境：同一台 macOS ARM64 机器、Python 3.12.13、Qt offscreen；相同离线脚本和消息内容，分别启动独立进程，使用临时数据库，不连接 Telegram。

## 同负载对照：五账号各 1,000 条文字消息

每次发送事件注入五账号，前台显示一个聊天，30 次账号切换。新版实际保留每账号最近 500 条，这是优化策略的一部分；此对照反映缓存限制、控件绘制和刷新策略的共同收益，不是单一算法或语言的对比。Qt 手动事件泵显式处理 DeferredDelete，避免把延迟释放计入人为积累。

| 指标 | 旧版 | 新版 |
| --- | ---: | ---: |
| 初始 RSS | 110.8 MB | 111.8 MB |
| 接收完成后的 RSS | 197.9 MB | 127.9 MB |
| 30 次切换后的 RSS | 371.3 MB | 128.8 MB |
| 最近消息缓存总数 | 5,000 | 2,500 |
| 切换耗时中位数 | 2,602.5 ms | 54.6 ms |
| 切换耗时 P95 | 2,615.2 ms | 58.8 ms |
| 前台空闲 CPU（单核百分比） | 1.25% | 0.70% |
| 后台空闲 CPU（单核百分比） | 1.32% | 0.71% |
| Qt 导入至主窗口初始化（单次） | 406.7 ms | 354.5 ms |

空闲 CPU 包含手动事件泵的开销，采样仅 3 秒，不据此承诺改善比例。初始化不包含解释器启动，不等同于打包程序冷启动。上述数值只反映这次离线模拟，不能直接外推 Windows 或真实网络场景。

## 新版压力：五账号各 10,000 条

| 每账号累计接收 | 进程 RSS |
| --- | ---: |
| 2,000 | 128.047 MB |
| 4,000 | 128.063 MB |
| 6,000 | 128.094 MB |
| 8,000 | 128.125 MB |
| 10,000 | 128.125 MB |

最近消息缓存合计始终为 2,500 条；30 次切换后 RSS 为 128.875 MB，中位数 54.4 ms、P95 58.8 ms。自动测试另覆盖独立历史窗口、十聊天 LRU、未缓存群、后台暂停、失败草稿和直接退出。

旧版万条负载的完整对照耗时过长，主动停止，未完成采样不用于改善比例。仅把新版万条结果作为缓存与内存稳定性验证。

## 复现与目标环境验收

```bash
rtk uv run python scripts/benchmark.py --output performance-after.json
rtk uv run python scripts/benchmark.py --messages-per-account 1000 --output performance-after-1000.json
rtk uv run python scripts/benchmark.py --source-root <旧版源码目录> --messages-per-account 1000 --output performance-before-1000.json
```

本机原始结果位于 `/private/tmp/tg-chat-performance-before-1000.json`、`/private/tmp/tg-chat-performance-after-1000.json`、`/private/tmp/tg-chat-performance-after.json`。测试：150 项及 2 项子测试通过，Ruff 静态、格式和补丁检查通过；宽窄模拟截图已查看。

Windows workflow 已增加新版五账号万条模拟测量，报告随构建产物上传；报告记录 Working Set、Private Bytes 和压缩包字节数。当前未运行该 workflow、未产出本轮 Windows 包。仍需在 Windows 验证成品冷启动、媒体占位、分页位置、中文输入、托盘恢复及真实五账号收发，不把 macOS 模拟通过标记为目标环境验收完成。
