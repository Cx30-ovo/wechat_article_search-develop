# 文档状态与使用规则

## 当前有效文档

| 文档 | 状态 | 用途 |
|---|---|---|
| `../README.md` | 当前有效 | 安装、命令入口和能力概览 |
| `WECHAT_SEARCH_COLLECTION_FLOW.md` | 当前有效、唯一流程基线 | Win11 搜一搜、资料页缓存池、多账号轮询、文章采集、恢复和测试 |
| `VM_DEPLOYMENT.md` | 当前有效 | Windows 生产部署、Linux/macOS 适配边界和多虚拟机迁移建议 |
| `DATABASE.md` | 当前有效 | 可选 MongoDB 数据结构 |
| `DOCKER.md` | 当前有效 | MongoDB 与宿主机 control-api 混合容器部署 |
| `../ARCHITECTURE.md` | 当前有效 | 当前目录、运行单元、容器边界和迁移状态；采集执行仍以流程基线为准 |
| `../SECURITY.md` | 当前有效 | 安全边界 |

## 已归档文档

| 文档 | 状态 | 限制 |
|---|---|---|
| `../DEPLOY.md` | 已归档 | 旧 MongoDB 批处理启动方式，不得作为当前部署步骤 |
| `PRODUCTIZATION.md` | 已归档 | 历史产品化路线，不得作为当前能力清单或优先级依据 |

归档文档默认不阅读、不引用、不作为实现与测试依据。需要恢复时，必须明确完成以下事项：

1. 移除文件顶部的归档标记并注明恢复日期；
2. 按当前代码完整重写过期内容；
3. 同步更新 `WECHAT_SEARCH_COLLECTION_FLOW.md`；
4. 更新或新增对应测试并完成 Win11 冒烟验证。
