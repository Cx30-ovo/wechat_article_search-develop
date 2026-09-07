# Docker 混合部署

Docker 承载 MongoDB 和宿主机 `control-api`。微信客户端、本地 OCR、桌面采集器和需要 Win32 API 的控制台仍运行在 Windows 交互式桌面中。

## 1. 服务

| 服务 | 端口 | 用途 |
|---|---:|---|
| `mongodb` | 默认宿主机 `27019` | 文章、账号、运行事件和心跳存储 |
| `control-api` | 默认宿主机 `8020` | 接收 Windows Agent 数据并提供账号接口 |

Compose 文件位于 `deploy/docker/compose.yaml`。

## 2. 启动

Windows 兼容脚本仍可直接使用：

```powershell
.\mongodb.bat setup
.\mongodb.bat start
.\mongodb.bat status
```

`setup` 会在 `.env.mongo` 中生成 MongoDB 两套密码和 `CONTROL_API_TOKEN`。已有配置缺少 Token 时会安全追加，不覆盖数据库凭据。
`start` 和 `restart` 会构建最新的 control-api 镜像，并同时等待 MongoDB 与 API 健康。

也可以直接执行：

```powershell
docker compose --env-file .env.mongo -f deploy/docker/compose.yaml up -d --build
```

## 3. 健康检查

```powershell
docker compose --env-file .env.mongo -f deploy/docker/compose.yaml ps
curl http://127.0.0.1:8020/health
```

## 4. Windows VM 访问 control-api

默认 API 只绑定宿主机 `127.0.0.1`。Windows 虚拟机需要访问时，在 `.env.mongo` 中设置：

```dotenv
CONTROL_API_BIND_IP=0.0.0.0
CONTROL_API_PORT=8020
```

随后重启容器，并通过宿主机防火墙只允许采集 VM IP 访问 8020。请求使用：

```text
Authorization: Bearer <CONTROL_API_TOKEN>
Content-Type: application/json
```

## 5. API

- `GET /health`：容器健康检查；
- `GET /api/v1/accounts`：读取账号配置；
- `POST /api/v1/articles`：按规范化 URL 幂等接收文章；
- `POST /api/v1/agent/heartbeat`：Agent 心跳；
- `POST /api/v1/runs/events`：运行事件。

文章请求沿用桌面采集器导出的字段：

```json
{
  "url": "https://mp.weixin.qq.com/s/example",
  "account_name": "厦门日报",
  "title": "文章标题",
  "publish_time": "2026-08-31 10:30",
  "content": "文章正文",
  "interaction": {"shareCount": 3}
}
```

API 会写入现有的 `account.*`、`article.*`、`source.*` 和 `interactionHistory` 结构，不会创建另一套扁平文章字段。

## 6. 数据持久化与维护

MongoDB 数据保存在命名卷 `wechat_article_mongo_data`。普通 `stop` 不删除数据卷。

```powershell
.\mongodb.bat logs
.\mongodb.bat backup
.\mongodb.bat restart
.\mongodb.bat stop
```

不要运行 `docker compose down --volumes`，除非明确要永久删除数据库。

备份文件默认写入 `output/mongodb-backups/`：

```powershell
.\mongodb.bat backup
.\mongodb.ps1 restore -BackupFile .\output\mongodb-backups\weixin-时间.archive.gz -Force
```

恢复会覆盖备份中的同名集合，执行前应停止采集并再做一次备份。`.\mongodb.bat connection` 可以显示本机连接参数，其中包含应用密码，不得复制到日志、Issue 或截图。

`deploy/docker/mongo/init.js` 只在数据卷首次创建时执行，负责创建应用账号和索引。已有数据卷不会因修改初始化脚本而自动重放；需要迁移时应先备份，不得直接删除数据卷。

## 7. 当前限制

- Desktop Agent 尚未自动把本地结果投递到 control-api；后续需接入 SQLite outbox。
- 容器 API 不控制微信窗口，也不替代 Windows 原生管理控制台。
- 静态管理页面已归类到 `services/control_api/static/`，当前仍由 Windows 原生控制台提供完整业务接口。
