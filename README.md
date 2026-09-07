# 微信公众号文章采集器

> 在 Windows 微信客户端中按公众号采集文章，提取正文、发布时间与互动数据，并经过校验和去重后写入 MongoDB。
> 参考了项目：https://github.com/Dhaizei/wechat-article-rpa  ，重做了windows微信搜一搜窗口的视觉采集逻辑

> 项目边界：本仓库负责 Windows Desktop Agent、宿主机文章接收 API、管理页面和存储；不包含文章摘要生成、编辑日报或消息发送功能。

## 目录

- [文档状态与使用规则](docs/README.md)
- [微信搜一搜文章采集流程（当前唯一流程基线）](docs/WECHAT_SEARCH_COLLECTION_FLOW.md)
- [当前系统架构](ARCHITECTURE.md)
- [系统能力](#系统能力)
- [运行架构与访问边界](#运行架构与访问边界)
- [快速开始](#快速开始)
- [Docker 宿主机服务](#docker-宿主机服务)
- [微信与桌面前置条件](#微信与桌面前置条件)
- [管理控制台](#管理控制台)
- [定时采集](#定时采集)
- [局域网与虚拟机部署](#局域网与虚拟机部署)
- [采集规则与数据写入](#采集规则与数据写入)
- [MongoDB 数据库](#mongodb-数据库)
- [高级命令](#高级命令)
- [配置项与数据目录](#配置项与数据目录)
- [常见问题](#常见问题)

## 系统能力

### 采集能力

- 从 MongoDB `weixin.collection_target` 读取待采集公众号；也支持本地账号名单或指定单个公众号。
- 通过微信“搜一搜”定位公众号；Win11 新版优先识别默认“全部”页中的“关键词 - 账号”公众号卡片，旧版再使用顶部“账号”分类和“公众号”二级筛选；不要求提前关注公众号。
- 按“今天 / 昨天 / 今天和昨天”采集文章，自动跳过置顶内容、历史内容和招聘等推广卡片。
- 从复制出的微信文章链接解析真实的标题、公众号、发布时间与纯文本正文；图片不写入正文。
- 默认仅识别**转发数**，速度更快；需要时可切换为完整互动指标。
- 互动栏优先用 OpenCV 模板匹配和 RapidOCR 本地识别；本地识别不确定时，才由 Qwen-VL 兜底。
- 搜索提交后先用 OpenCV 等待结果页顶部搜索栏出现；输入框仍聚焦时再发送 `Esc`，必要时点击安全空白区，并连续两次验证失焦后才运行结果 OCR。发送按键不等于关闭成功。连续失败时可用 `--manual-search-fallback` 临时人工完成搜索，再由程序接管。
- 写入前以文章 URL、公众号、正文和发布时间为强校验；资料页卡片标题及文章窗口 OCR 标题作为辅助证据和告警，避免 OCR 截断造成漏抓。

### 管理能力

- 管理员控制台：手动采集、定时任务、启动前检查、实时日志、文章管理、公众号管理、导入导出。

## 运行架构与访问边界

```text
Windows 11 虚拟机（交互式桌面）
  微信客户端 ↔ Desktop Agent ↔ Windows 原生控制台
                         │
                         │ HTTP/JSON（后续由 outbox 可靠投递）
                         ▼
宿主机 Docker：control-api → MongoDB
```

控制台页面包括 `/`、`/accounts.html` 和 `/articles.html`，所有业务接口都需要 HTTP Basic 管理员认证。**不要把管理员控制台直接开放到公网。**

## 快速开始

### 1. 获取项目并准备环境

推荐直接从仓库部署，而不是复制另一台电脑的 `.venv`：虚拟环境中含有机器绝对路径，不能跨电脑复用。

```powershell
git clone git@github.com:Dhaizei/wechat-article-rpa.git
cd .\wechat-article-rpa
```

双击：

```text
setup-env.bat
```

安装脚本会在缺少 `uv` 时安装到项目 `.tools`，自动安装并管理 64 位 Python 3.10，创建 `.venv`，再使用清华 PyPI 镜像安装依赖。默认镜像为：

```text
https://pypi.tuna.tsinghua.edu.cn/simple
```

如需手动安装依赖：

```powershell
cd .\wechat-article-rpa
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

复制 `.env.example` 作为配置参考。项目不会读取或提交 `.env` 中的凭据；在 PowerShell 中启动前至少设置 MongoDB 和管理员密码：

```powershell
$env:MONGO_URI="mongodb://127.0.0.1:27017/"
$env:CONTROL_PANEL_PASSWORD="请替换为强密码"
```

也可以直接双击 `start-control-panel.bat`，启动脚本会在本次运行中安全询问管理员密码。

## Docker 宿主机服务

Docker Desktop 已启动时，可用项目自带脚本准备并启动 MongoDB 和宿主机 `control-api`：

```powershell
.\mongodb.bat setup
.\mongodb.bat start
.\mongodb.bat status
```

MongoDB 默认监听 `127.0.0.1:27019`，control-api 默认监听 `127.0.0.1:8020`。setup 会生成 MongoDB 密码和 API Token，写入被 Git 忽略的 `.env.mongo`。Windows VM 访问 API 时必须修改绑定地址并设置防火墙白名单。

常用维护命令：

```powershell
.\mongodb.bat logs
.\mongodb.bat connection
.\mongodb.bat backup
.\mongodb.bat restart
.\mongodb.bat stop
```

数据库恢复属于覆盖性操作，必须显式确认：

```powershell
.\mongodb.ps1 restore -BackupFile .\output\mongodb-backups\weixin-时间.archive.gz -Force
```

Compose 实现位于 `deploy/docker/`。完整说明见 `docs/DOCKER.md` 和 `docs/DATABASE.md`。

### 2. 启动本机管理控制台

双击：

```text
start-control-panel.bat
```

本机访问：

```text
http://127.0.0.1:8010/
```

首次启动前，请确认微信已登录并已打开“搜一搜”窗口。控制台会执行微信、搜一搜、窗口布局和屏幕缩放的启动前检查。

## 微信与桌面前置条件

这是**桌面视觉采集**，不是无界面 HTTP 爬虫。采集期间需要真实、可交互的 Windows 桌面环境。

### 已验证环境

当前已经完成实际采集验证的旧版桌面组合：

| 项目 | 已验证版本 |
|---|---|
| 操作系统 | Windows 7 64 位 |
| 微信 Windows 客户端 | `4.1.9.57` |

该记录表示微信窗口识别、搜一搜、公众号资料页和文章采集流程已在上述组合中运行验证，并不代表所有 Windows 7 裸机都能直接完成依赖安装。项目的一键安装脚本默认使用 64 位 Python 3.10。

Windows 11 新版微信与上述环境不是同一种 UI 实现：当前实测会使用 `Chrome_WidgetWin_0` 和内置 Chromium 标签页，公众号资料页可能与搜一搜共用同一个窗口句柄。项目通过 `src/wechat_rpa/desktop/win11_adapter.py` 按页面内容激活资料页标签。当前固定适配的主界面是新版左侧功能栏布局。标签顺序不依赖移动快捷键，未知标签保留。Windows 11 适配仍需在固定版本、固定分辨率的 VM 中完成连续闭环验收。

建议生产 VM 固定当前微信版本和桌面配置。记录微信“关于”页面显示的版本号、Windows 版本、屏幕分辨率、缩放比例和显示器数量；微信升级前先创建 VM 快照，升级后重新执行单账号冒烟测试。

1. 微信 Windows 客户端已登录。
2. 微信内的“搜一搜”窗口已打开；控制台可尝试自动拉起，但首次部署建议人工验证一次。
3. 不锁屏、不休眠、不注销，不在采集期间手工切换微信窗口。
4. 采集过程中不要移动鼠标、键盘操作或改变分辨率与缩放比例。
5. 推荐分辨率 `2560×1440`、缩放 `100%`；`1920×1080`、缩放 `100%` 也可使用。控制台会显示当前适配检查结果。
6. 使用远程桌面维护时，避免在采集期间断开/重连导致分辨率变化；优先使用 Hyper-V/VMware 控制台或 Parsec、AnyDesk 等稳定的远程控制方式。

首次适配 Windows 11 时，先在项目根目录执行一行单账号测试命令。建议每次使用新的输出目录，避免历史截图和 JSON 覆盖或混淆本次证据：

```powershell
.venv\Scripts\wechat-rpa.exe --run-search-accounts --live --account-name "厦门日报" --max-articles 1 --scan-range today --metrics share --output-dir ".\output\smoke-test-win11"
```

不要把 Markdown 围栏反引号复制到命令末尾。Win11 的最小路径是：搜索结果默认“全部”页 → “关键词 - 账号”公众号卡片 → 公众号资料页 → 第一篇文章。启动日志的 `run_started` 事件会记录 `source_fingerprint`，用于确认 VM 实际运行的是当前核心源码；搜索按钮点击后的第一帧保存为 `search-after-submit.png`，只有后续出现 `search_result_page_confirmed` 才表示真正进入结果页。失败反馈时请一并保留 `run.log`、`search-after-submit.png`、`search-result.png`、`profile-opened.png`、`profile-window-local-*.png`、`feed.json` 和对应的错误文件。每次测试请使用新的 `--output-dir`，避免历史截图和 JSON 混入本次证据。

## 管理控制台

控制台提供三类日常操作：

| 页面 | 用途 |
|---|---|
| 控制台 `/` | 手动采集、定时计划、启动前检查、运行进度、日志与失败原因 |
| 公众号管理 `/accounts.html` | 查询、筛选、编辑搜索别名、导入/导出账号配置 |
| 文章管理 `/articles.html` | 查询文章、查看正文、按条件导出 CSV 或 JSON |

### 手动执行

在控制台选择：

- **扫描范围**：今天、昨天、今天和昨天；
- **采集指标**：仅转发数（更快）或全部互动指标；
- **每账号上限**：单次最多读取的文章卡片数；
- **起始公众号**：从公众号列表中的指定公众号开始，默认从第一个开始；
- **账号数量**：本次最多采集多少个公众号，0 表示全部。

随后点击“立即开始”。手动任务与定时任务共用同一个进程锁，不能并发启动；需要终止时点击“停止任务”。

### 管理员认证

管理员控制台默认使用 HTTP Basic Auth。部署到局域网前，务必通过环境变量设置强密码：

```powershell
setx CONTROL_PANEL_USERNAME "admin"
setx CONTROL_PANEL_PASSWORD "请设置至少16位的强密码"
```

执行后关闭并重新打开 PowerShell，再重新启动控制台。

> 管理员账号默认是 `admin`，密码没有默认值。启动前必须通过 `CONTROL_PANEL_PASSWORD` 设置强密码。

## 定时采集

控制台中的“定时任务”保存后立即生效，配置文件位于：

```text
config\control_panel.json
```

推荐计划：

| 时间 | 扫描范围 | 目的 |
|---:|---|---|
| `08:00` | 今天和昨天 | 补齐夜间发布和昨日遗漏，供 9 点后的后续 RPA 使用 |
| `22:00` | 今天 | 同步当天晚间新增文章 |

定时器依赖控制台进程常驻。若部署到虚拟机，建议在 Windows“任务计划程序”中创建**用户登录时**启动的任务：

- 操作：运行 `start-control-panel.bat`；
- 选择“仅当用户登录时运行”；
- 不要选择“无论用户是否登录都要运行”，因为微信 UI 自动化需要交互式桌面；
- 设置失败后每 5 分钟重试，最多 3 次；
- 关闭自动睡眠、休眠和锁屏。

## 局域网与虚拟机部署

### 平台支持与多虚拟机建议

当前采集器是**Windows 微信桌面客户端的视觉自动化程序**，生产支持边界仍是 Windows 11 交互式桌面。代码中的窗口句柄、剪贴板、DPI、截图和键鼠控制均依赖 Windows API；Linux 或 macOS 虚拟机不能直接运行当前采集闭环。

| 部署方式 | 当前状态 | 适合场景 |
|---|---|---|
| Windows 11 虚拟机 | 当前生产推荐，兼容性最高 | 立即上线、优先保证采集稳定性 |
| Linux ARM64/x86_64 + X11 虚拟机 | 规划中的低资源方案，需重写桌面适配层并重新验收微信流程 | 同一台宿主机部署较多实例 |
| macOS 虚拟机 | 不作为当前部署目标，需重写适配层并处理辅助功能/屏幕录制权限 | 只有明确要求使用 macOS 微信客户端时考虑 |

在 Apple Silicon 宿主机上，如果暂时不改代码，仍应创建 Windows 11 虚拟机作为生产基线；如果目标是降低多实例负担，应先用一台 ARM64 Linux + X11 虚拟机做移植试验，不能把“能启动 Linux 桌面”当成采集链路已经兼容。Linux 试验必须使用完整的可交互图形桌面和虚拟显示器，Docker 或无界面终端环境不满足当前流程要求。

多虚拟机部署时，每台虚拟机应使用独立的微信登录会话、独立输出目录和独立资料页池。不要让两个实例同时控制同一个微信账号。Linux 适配完成前，建议先按每台 Windows 11 虚拟机不超过 10 个账号进行固定版本实测；这个数量是稳定性初始上限，不是永久硬限制。

### 推荐虚拟机规格

| 项目 | 推荐 |
|---|---|
| 操作系统 | Windows 11 Pro 64 位 |
| CPU | 4 vCPU 起步，建议 6 vCPU |
| 内存 | 8 GB 起步，建议 12–16 GB |
| 磁盘 | 80–120 GB SSD |
| 分辨率 | 2560×1440 / 100% 缩放；最低 1920×1080 / 100% |
| 网络 | 固定局域网 IP，能访问你配置的 MongoDB 服务 |

在 VM 中检查 MongoDB 连通性：

```powershell
Test-NetConnection 127.0.0.1 -Port 27017
```

结果中 `TcpTestSucceeded : True` 表示网络连通。

### 在可信局域网访问管理控制台

默认服务只监听 `127.0.0.1`。若确需在可信局域网管理采集任务，可在 VM 上用以下方式启动：

```powershell
cd .\wechat-article-rpa
.\.venv\Scripts\wechat-rpa-control-panel.exe --host 0.0.0.0 --port 8010
```

查看 VM 的局域网 IP：

```powershell
ipconfig
```

例如 VM IP 为 `192.168.1.88`，管理员访问：

```text
http://192.168.1.88:8010/
```

在 Windows 防火墙中仅允许公司内网网段访问。以下示例仅允许 `192.168.1.*` 访问 8010：

```powershell
New-NetFirewallRule `
  -DisplayName "WeChat RPA Control Panel 8010" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8010 `
  -Profile Private `
  -RemoteAddress 192.168.1.0/24
```

将 `192.168.1.0/24` 替换为你公司的实际内网网段。不要将 8010 端口映射到公网；如确有外网访问需求，应使用公司 VPN 或 HTTPS 反向代理，并额外限制来源地址。

### 迁移清单

| 内容 | 是否迁移 | 说明 |
|---|---|---|
| Git 代码 | 是 | 建议在新 VM 重新克隆远端仓库 |
| `.venv` | 否 | 不可跨电脑复制，必须重新创建 |
| `config/account_aliases.json` | 是 | 保留公众号实际搜索名别名 |
| `config/control_panel.json` | 是 | 保留定时策略与控制台配置 |
| Qwen-VL 环境变量 | 是 | 请在新 VM 独立配置，不要提交到 Git |
| `output` 历史截图和日志 | 按需 | 建议归档，不必作为运行依赖迁移 |
| `config/run_history.json` | 否 | 仅本机历史记录，不影响采集 |
| MongoDB 数据 | 否 | 继续使用部署环境通过 `MONGO_URI` 指定的数据库 |

切换时务必先停止旧机器的控制台与定时任务，再启用 VM 的定时任务，避免两台设备同时操作微信或重复采集。

## 采集规则与数据写入

### 公众号搜索与窗口流程

1. 启动时只确认搜一搜页面，不关闭已有公众号资料页或未知标签；
2. 在微信搜一搜输入目标账号名称；
3. 确认一级分类“账号”已选中；旧版界面再选择二级“公众号”，新版界面直接校验公众号结果卡片；
4. 通过本地 OCR 找到同名公众号结果；若已有资料页，先校验名称并复用，否则点击结果卡片打开资料页；
5. 通过资料页截图顶部名称验证资料页并立即保存基线截图；新版微信资料页可能是搜一搜浏览器中的新标签，窗口标题仍可能是“微信”，后续截图前会按页面内容重新激活资料页标签，不能只依赖窗口句柄；
6. 按时间分组读取文章卡片，向下滚动至“星期 N / 周 N / 明确年月日”等历史边界；资料页头部、纯数字和封面文字会被过滤；本地 OCR 只要识别到日期分组和安全文章候选就先打开文章，“阅读/赞”仅作为列表指标补充，缺失时留空；只有日期或文章卡片本身缺失时才使用 Qwen-VL 复核；
7. 依次打开文章，复制链接解析正文；
8. 校验文章账号、正文、发布时间和当前 URL，再读取转发数；浏览器标签标题、正文标题与卡片标题只作辅助证据；
9. 每篇文章完成后只关闭文章标签；账号完成后只关闭本次确认过的资料页标签或窗口；新版微信的内嵌资料页只发送标签级 `Ctrl+W`；搜一搜不要求位于首标签，恢复阶段通过页面 OCR 选中并保留它；未确认的标签不批量关闭。

如果数据库中的账号名与微信实际展示名称不同，在 `config/account_aliases.json` 设置一对一别名。别名仅改变微信搜索词，不会改写 MongoDB 中已有账号名称。格式参见：

```text
config\account_aliases.example.json
```

### 去重与质量保护

同一公众号的单次任务以文章 URL 为最终去重键：

1. **卡片指纹层**：时间分组、规范化标题、列表阅读数和列表点赞数组成完整卡片指纹；该指纹在本轮已成功采集后再次出现时，视为相邻滚屏重叠的同一卡片并直接跳过。仅标题相同而互动数缺失或变化的候选仍会点击，再由 URL 精确去重，避免不同文章被错误合并。
2. **文章链接层**：打开后将 URL 规范化；若与本次已成功文章完全相同，立即关闭，不再重复解析正文和互动数。

标题 OCR 截断或相似标题只产生告警，不会阻断已经通过 URL、账号、正文和发布时间校验的文章。失败文章允许有限重试。批次内 URL 集合不会跨任务保留；启用 MongoDB 时通过 `article.urlNormalized` 实现跨运行幂等写入，后续运行仍可刷新互动数据。

批量搜索采集可加 `--stop-after-known-url`：程序按最新到最旧检查文章，遇到 Mongo 中已经采集过的 URL 就结束当前公众号并继续下一个，适合定时批量只补最新文章。控制台定时/手动任务默认已开启该行为。批量模式在成功采集、或打开后命中已知 URL 而停止时，都会把该卡片的标题指纹写入 `weixin.card_progress`；后续轮次在点击前若顶部卡片指纹已经命中，会直接结束当前公众号而不打开文章。URL 仍作为最终精确校验。

### MongoDB 写入规则

生产采集默认使用：

```text
MONGO_URI=mongodb://127.0.0.1:27017/
数据库：weixin
账号集合：collection_target
文章集合：article
```

使用 `--write-mongo` 后，正文非空文章会幂等写入 `weixin.article`：

- `article.url`：原始文章链接；
- `article.urlNormalized`：标准化链接，用于辅助去重；
- 首次出现：插入文章正文、标题、发布时间、账号和互动数据；
- 再次采集：保护已有非空正文、标题和发布时间，只补齐空字段并追加互动数据快照；
- `account.id` 优先使用 `collection_target` 中同名账号 ID，缺失时生成稳定兜底 ID；
- 正文为空的文章不会写入 MongoDB。

本地输出始终保存在本次运行的 `output` 目录，包括日志、截图、验证证据、JSONL 和 CSV。

## MongoDB 数据库

本项目使用 MongoDB 作为唯一服务端存储。文章正文、账号配置与互动历史都需要可靠持久化，Redis 不适合作为这些数据的主存储，因此开源版不要求安装 Redis。

集合结构、字段含义、索引、初始化、查询和备份方法见 `docs/DATABASE.md`。

## 高级命令

### 只读识别

只截图并识别，不操作鼠标，也不写入数据库：

```powershell
.\.venv\Scripts\wechat-rpa.exe
```

### 采集一个已关注公众号

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --run-one-account --live --account-index 0 --max-articles 2
```

### 按名称搜索并采集单个公众号

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --run-search-accounts --live `
  --account-name "量子位" --max-articles 5
```

如果微信搜索联想框连续遮挡结果页，值守测试时可增加人工兜底（等待最多 120 秒）：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --run-search-accounts --live --manual-search-fallback `
  --account-name "厦门日报" --max-articles 1
```

### 常驻增量监听

每轮只检查当天最新的几张文章卡片，遇到已经记录过的文章 URL 后停止本轮继续翻历史：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-account "厦门日报" --live --poll-interval 1800 `
  --recent-card-limit 5 --metrics share `
  --output-dir ".\output\watch-xiamen"
```

单账号与多账号共用同一个调度器。多个账号可放入文本文件，每行一个名称：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-accounts-file ".\config\watch-accounts.txt" --live `
  --accounts-per-vm 10 --poll-interval 1800 --recent-card-limit 5 `
  --metrics share --output-dir ".\output\watch-vm-01"
```

也可以直接从 MongoDB 的 `collection_target.name` 读取账号：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-accounts-from-mongo --live `
  --accounts-per-vm 120 --poll-interval 1800 --recent-card-limit 5 `
  --write-mongo --output-dir ".\output\watch-all"
```

Windows 也可以直接使用两阶段启动脚本：

```powershell
.\scripts\windows\start-watch-prewarm.ps1
.\scripts\windows\start-watch-all.ps1
```

预热完成后不要关闭微信标签，第二个脚本只复用已有资料页池并开始常驻轮询。

`--accounts-per-vm` 默认仍是 10。直接传 120 意味着单台 Windows 会尝试维护 120 个资料页标签，
该规模尚未经过稳定性实测；调度语义是每个公众号距上次完成检查至少 30 分钟后再查一次，
不是保证 120 个公众号在一轮 30 分钟内全部完成。

多账号监听支持把资料页池和采集进程完全分开。先只建立资料页池：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-accounts-file ".\config\watch-accounts.txt" --bootstrap-profile-pool --live `
  --local-only --accounts-per-vm 10 --output-dir ".\output\watch-vm-01"
```

完成后保留微信标签，再启动只复用现有资料页的采集进程：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-accounts-file ".\config\watch-accounts.txt" --watch-existing-profile-pool --live `
  --local-only --accounts-per-vm 10 --poll-interval 1800 --recent-card-limit 5 `
  --metrics share --output-dir ".\output\watch-vm-01"
```

更新采集代码时只停止第二个进程，不关闭微信；重新执行第二条命令后，程序通过 OCR 重新登记现有标签，不会直接复用旧 HWND，也不会因某个账号附着失败而在启动阶段重新搜索全部账号。根目录 `profile-pool-state.json` 保存资料页池，`scheduler-state.json` 只保存调度进度，每账号 `watch-state.json` 只保存 URL 与采集轮次。

每天定时在 07:30～24:00 之间监听：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --watch-account "厦门日报" --live --watch-start-time 07:30 --watch-end-time 24:00 `
  --poll-interval 1800 --recent-card-limit 5 --metrics share `
  --output-dir ".\output\watch-xiamen-day"
```

到 24:00 时程序会完成当前采集轮次并保存状态，停止采集但保持进程等待；次日 07:30 从
`watch-state.json` 的已知 URL 继续，不需要任务计划程序强制终止进程。

### 仅使用本地 OCR，不调用视觉模型

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --run-search-accounts --live --local-only `
  --accounts-file .\config\accounts.txt --max-articles 20
```

本地 OCR 无法定位搜索结果、文章卡片或互动数时，此模式会记录失败，不会调用 Qwen-VL。

### 视觉模型最小闭环自检

配置好 `.env` 中的 `QWEN_VL_BASE_URL`、`QWEN_VL_API_KEY` 和 `QWEN_VL_MODEL` 后，
在 Windows 虚拟机的项目目录执行：

```powershell
.\.venv\Scripts\wechat-rpa-vision-smoke.exe
```

该命令默认抓取当前 Windows 桌面，调用公众号管理器布局识别，并把截图与模型 JSON
保存到 `output\vision-smoke\`。它不会点击微信，也不会写入 MongoDB。

也可以对已有截图做离线输入测试：

```powershell
.\.venv\Scripts\wechat-rpa-vision-smoke.exe `
  --image .\output\some-screenshot.png --task manager-layout
```

看到 `视觉模型最小闭环已完成：截图 -> Qwen-VL -> JSON` 即表示模型配置、网络、图片上传
和项目解析链路均已打通。

### 从 MongoDB 读取所有账号并写入文章

```powershell
$env:MONGO_URI="mongodb://127.0.0.1:27017/"
.\.venv\Scripts\wechat-rpa.exe `
  --run-search-accounts --live --local-only `
  --accounts-from-mongo --write-mongo --max-articles 20
```

多账号手动测试不需要跑完全部公众号时，可按名称指定起始账号并限制本次数量：

```powershell
.\.venv\Scripts\wechat-rpa.exe `
  --run-search-accounts --live --local-only `
  --accounts-from-mongo --write-mongo --max-articles 20 `
  --start-account "厦门晚报" --max-accounts 3
```

`--start-account` 按公众号名称匹配并包含该账号，不传则从第一个开始；`--max-accounts` 表示本次最多采集的账号数，0 表示全部。控制台“手动执行”页面提供同样的两个选项。

### 采集当前已经打开的文章

```powershell
.\.venv\Scripts\wechat-rpa.exe --collect-open-article
```

### 配置 Qwen-VL 兜底

仅在本地 OCR 无法可靠识别时调用。请在启动控制台或采集命令前设置环境变量：

```powershell
$env:QWEN_VL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:QWEN_VL_API_KEY="你的密钥"
$env:QWEN_VL_MODEL="dashscope/qwen3-vl-plus"
```

不要把真实 API Key 写进代码、README、配置文件或提交到版本库。

如果视觉模型部署在可信内网，并且 OpenAI 兼容接口明确不要求鉴权，可以不使用 API Key：

```text
QWEN_VL_BASE_URL=http://内网模型地址:端口/v1
QWEN_VL_API_KEY=
QWEN_VL_MODEL=实际加载的视觉模型名
QWEN_VL_ALLOW_NO_AUTH=true
```

此时客户端不会发送 `Authorization` 请求头。该开关只适用于可信内网的无认证服务，
不要对公网地址启用。接口必须支持 `POST /v1/chat/completions`，并接受
OpenAI 格式的 `image_url` 图片输入。

## 配置项与数据目录

| 路径 / 变量 | 用途 |
|---|---|
| `config/control_panel.json` | 定时任务、扫描范围、指标、每账号上限 |
| `config/account_aliases.json` | MongoDB 账号名与微信搜索名的映射 |
| `config/accounts.txt` | 可选的本地账号名单，每行一个名称 |
| `output/control-panel.log` | 控制台运行日志 |
| `output/<run-id>/run.log` | 单次采集日志 |
| `output/<run-id>/articles.jsonl` | 本次采集的完整文章结果 |
| `output/<run-id>/articles.csv` | 便于检查的表格结果 |
| `output/<run-id>/article_evidence.png` | 单篇文章截图证据 |
| `output/<run-id>/verification.json` | URL、标题和互动栏校验信息 |
| `MONGO_URI` | MongoDB 连接串 |
| `CONTROL_PANEL_USERNAME` | 管理员用户名 |
| `CONTROL_PANEL_PASSWORD` | 管理员密码 |
| `QWEN_VL_*` | Qwen-VL 兜底模型配置 |

## 常见问题

### 采集时提示“未找到搜一搜顶部搜索框”

先确认微信已登录并且“搜一搜”窗口存在且没有被最小化。控制台的“重新检测 / 自动恢复”会尝试拉起窗口；如果仍失败，手工在新版微信左侧会话栏顶部搜索框输入“搜一搜”，在候选项中点击“搜一搜”后重新检测。程序不会因为一次资料页 OCR 失败就直接关闭搜一搜窗口；会先扫描同一浏览器窗口的其他标签，并保存 `profile_detection_attempt` 诊断事件。

### 定时任务到点却没有执行

检查以下前提：

- 控制台启动窗口仍在运行；
- Windows 用户仍登录，桌面未锁定；
- VM 未休眠；
- 微信与搜一搜窗口可用；
- 控制台中已保存并启用定时计划。

### 文章没有写入 MongoDB

检查：

- 命令是否包含 `--write-mongo`；
- `MONGO_URI` 是否能连通；
- 文章正文是否为空；空正文按规则不会写入；
- `output/<run-id>/run.log` 是否出现标题、URL 或窗口校验失败。

### 为什么同一篇文章会被打开或复制链接多次

这是安全校验的一部分。程序会在互动数识别前后分别复制 URL，只有两次 URL 相同才认为互动数与正文属于同一篇文章。卡片标题不再作为硬失败条件，避免 OCR 截断导致同一 URL 重复重试；失败文章仍会在有限次数内重试，已成功 URL 会在当前任务中去重。

### 为什么不要在采集期间改变远程桌面分辨率

模板匹配、OCR 截图区域和窗口布局都依赖真实桌面坐标。远程桌面重连可能导致 DPI、分辨率或窗口渲染变化，从而降低识别稳定性。

## 运行安全提示

- 不要把控制台端口暴露到公网。
- 局域网开放前必须修改默认管理员密码。
- 不要在采集过程中人工操作微信、鼠标或键盘。
- 不要同时在两台机器上运行同一个微信账号的批量采集。
- 账号别名、运行历史和输出日志属于运行数据；更新代码前建议先提交代码、再单独备份配置与输出目录。
