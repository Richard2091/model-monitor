# 模型可用性监控（model-monitor）


对 sub2api 的多个模型提供可用性监控：在可配置的监控时段内，每隔指定分钟对每个模型发一次最小聊天请求，记录可用性与延迟；页面以横条时间线展示每个模型当天的探测结果，并通过 sub2api 自定义菜单挂到侧栏。


## 目录结构


```

model-monitor/

├── app.py            # 入口：启动后台探测线程 + HTTP 服务

├── config.py         # 配置：从 .env 读取并解析（多监控时段、分钟间隔、慢阈值、模型列表等）

├── database.py       # 数据：SQLite 初始化、写入探测记录、按槽位归并查询

├── config_manager.py # 运行配置快照与动态刷新

├── security.py       # API Key 加密与会话安全工具

├── monitor.py        # 探测：对模型发起可用性探测、后台定时循环

├── http_server.py    # 接口：提供监控页面与 JSON API（/、/api/status、/api/probe）

├── templates/

│   ├── index.html       # 前端页面（内嵌 CSS/JS），含 __THEME_CLASS__/__TODAY__ 占位符

│   └── maintenance.html # 独立维护页（后端 /maintenance 路由返回）

├── .env.example      # 配置模板（提交到仓库）
├── .env              # 本地运行配置（不入库，复制 .env.example 后填写）

├── Dockerfile        # 容器镜像

└── data/             # SQLite 数据库（挂载卷）

```


## 配置（.env）


> `.env` 只用于本机或容器运行配置，包含敏感信息时不应提交到仓库。仓库仅保留 `.env.example` 模板，复制为 `.env` 后填入真实值即可；`.env` 已被 `.gitignore` 排除。


| 变量 | 说明 | 默认值 |

| --- | --- | --- |

| `ENV_FILE` | `.env` 文件路径覆盖；环境变量优先级高于该文件中的同名配置 | `/app/.env` |
| `SUB2API_URL` | sub2api 网关地址 | `http://sub2api:8080` |

| `SUB2API_KEY` | API Key（仅服务端使用） | - |

| `DB_PATH` | SQLite 数据库路径 | `/data/model-monitor.db` |

| `HOST` | 服务监听地址 | `0.0.0.0` |
| `PORT` | 服务监听端口 | `8090` |
| `PROBE_TOKEN` | 手动探测 `/api/probe` 的鉴权 Token；留空则禁用手动探测接口 | 空 |

| `PROBE_INTERVAL_MINUTES` | 探测间隔（分钟） | `5` |

| `SLOW_THRESHOLD_MS` | 慢响应阈值（毫秒） | `10000` |

| `MONITOR_WINDOWS` | 监控时段，多段用逗号分隔，格式 `HH:MM-HH:MM`，含起点与终点 | `08:00-22:00` |

| `MODELS` | 待监控模型，逗号分隔；支持多个厂商和模型，环境变量优先于 `.env` | 6 个模型（GPT、Qwen） |


修改旧版 `.env` 配置后执行 `docker restart model-monitor` 生效。启用数据库配置管理后，管理页保存的厂商、模型和监控参数会在当前服务内动态生效，无需重启。

### 配置管理页

访问 `/model-monitor-admin` 进入“模型监控管理”。管理员账号、密码和 API Key 加密主密钥只从 `.env` 或进程环境读取：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请设置管理员密码
ADMIN_SESSION_TTL_MINUTES=480
ADMIN_IDLE_TIMEOUT_MINUTES=30
ADMIN_COOKIE_SECURE=false
MODEL_MONITOR_MASTER_KEY=请填写 Base64URL 编码的 32 字节密钥
MODEL_CONFIG_SOURCE=database
```

首次启动会将旧 `.env` 中的网关、模型、监控时间段和探测参数导入 SQLite，创建“默认网关”；已有数据库配置不会被 `.env` 覆盖。API Key 只保存 AES-256-GCM 密文，管理页面只显示“已配置”。

将 `MODEL_CONFIG_SOURCE` 改为 `legacy-env` 后重启服务，可临时回退到旧版环境变量读取逻辑。停用只停止探测并隐藏页面、保留历史；删除会先创建 SQLite 在线备份，再删除配置及关联历史记录。


### 模型配置说明

模型清单统一通过 `MODELS` 配置，支持在 `.env` 或容器环境变量中增删改，模型之间使用英文逗号分隔；当前服务启动时读取配置，修改后需要重启服务生效。例如：

```dotenv
MODELS=gpt-5.5,qwen3.7-plus,claude-3-7-sonnet
```

- 配置优先级：进程环境变量 `MODELS` > `.env` 中的 `MODELS` > 内置默认模型清单。
- 配置会自动清理空白并按首次出现顺序去重。
- 页面会根据模型名称开头的英文前缀自动识别厂商系列并分组，例如 `qwen3.7-plus` 归入 `Qwen`；新增厂商无需修改后端代码即可被探测和展示。
- 删除模型只会停止后续探测并隐藏页面展示，不会删除 SQLite 中已有的历史记录。
- 当前内置默认清单已移除 `DeepSeek` 和 `GLM-5.3-flash`；如需恢复，可在 `.env` 或环境变量中显式加入对应模型名称。



## 部署


```bash

docker build -t model-monitor:latest .

docker run -d --name model-monitor \

  --network sub2api_sub2api-network \

  -p 18090:8090 \

  -v /data/sub2api/model-monitor/data:/data \

  -v /data/sub2api/model-monitor/.env:/app/.env:ro \

  --restart unless-stopped \

  model-monitor:latest

```


- 容器需与 sub2api 处于同一网络，才能通过容器名解析 `sub2api:8080`。

- 前端模板在 `templates/index.html`，修改后需 `docker build` 重建镜像（或挂载模板目录便于热更新）。
- 容器以固定的非 root 用户 `UID/GID=10001` 运行；使用宿主机绑定挂载 `/data` 前，请先执行 `mkdir -p /data/sub2api/model-monitor/data && chown -R 10001:10001 /data/sub2api/model-monitor/data`，否则 SQLite 可能无权写入。


## 接口


- `GET /`：监控页面（支持 `?theme=dark` 切换暗色）。

- `GET /api/status?date=YYYY-MM-DD`：返回某日当前启用模型的槽位数据。

- `GET /model-monitor-admin`：配置管理页；未登录时跳转到登录页。

- 管理接口提供登录、会话、厂商、模型、监控设置、测试连接和删除影响预览；写接口需要会话与 CSRF 令牌。

- `POST /api/probe`：手动触发一次全量探测；必须携带请求头 `X-Monitor-Token: <PROBE_TOKEN>`，未配置 `PROBE_TOKEN` 时接口禁用。

- `GET /maintenance`：独立维护页（也可用于直接访问）。


## 前端说明


页面为单文件 `templates/index.html`（含 CSS/JS）。渲染时由 `http_server.render_index` 替换两个占位符：

- `__THEME_CLASS__`：明暗主题 class。


- `__TODAY__`：今天日期（JS 可识别的 `new Date(...)`）。


监控点 hover 可查看单点明细，点击可复制该点信息（模型/时间/状态/延迟/错误）。


## 维护页面与失败自动恢复


为避免维护/重启期间出现生硬的「加载失败」提示，实现了两层维护展示：


1. **页面内嵌维护视图（主要机制，`index.html`）**

   - 触发条件：`load()` 请求 `api/status` 失败时，包括网络错误、HTTP 非 2xx、返回体异常（无 `models` 字段）。

   - 行为：隐藏列表区，显示同款风格的维护卡片（旋转图标 + 「系统维护中」+ 「立即重试」按钮）。

   - 自动重试：维护视图内 **每 10 秒倒计时自动重试**，也可点击「立即重试」手动触发；服务恢复后自动切回并正常加载数据，无需刷新页面。

   - 正常加载成功时维护视图自动隐藏。


2. **独立维护页（`templates/maintenance.html`）**

   - 通过 `GET /maintenance` 访问，适合容器完全下线后由外部工具/链接引导用户查看。

   - 页面自带 **30 秒倒计时自动刷新**，恢复后刷新即回到监控页面。


修改维护文案/样式：

- 页面内嵌维护视图：改 `templates/index.html` 中 `.maintenance/.maint-*` 样式与 HTML 片段。

- 独立维护页：改 `templates/maintenance.html`。
