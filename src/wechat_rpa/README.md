# Python 包边界

- `desktop/`：Windows 交互式桌面和微信标签适配，只能在登录用户会话运行。
- `vision/`：本地 OCR、OpenCV 和内网视觉模型适配。
- `content/`：文章网页解析和内容规范化。
- `scheduler/`：资料页池、轮询和时间窗口；当前行为仍由 `runtime/collector_runtime.py` 提供，后续按测试逐步迁出。
- `storage/`：状态文件、本地导出、MongoDB 与可靠发件箱。
- `observability/`：结构化日志和运行指标。
- `runtime/`：当前有效采集器和控制台编排入口，采集行为仍以 `docs/WECHAT_SEARCH_COLLECTION_FLOW.md` 为唯一依据。

根目录不再保留旧 Python 模块名；所有运行和导入统一使用 `wechat_rpa` 包。
