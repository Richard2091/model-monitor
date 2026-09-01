# 模型可用性监控（model-monitor）

对 sub2api 的多个模型提供可用性监控：在可配置的监控时段内，每隔指定分钟对每个模型发一次最小聊天请求，记录可用性与延迟；页面以横条时间线展示每个模型当天的探测结果，并通过 sub2api 自定义菜单挂到侧栏。

## 目录结构

```
model-monitor/
├── app.py            # 入口：启动后台探测线程 + HTTP 服务
├── config.py         # 配置：从 .env 读取并解析（多监控时段、分钟间隔、慢阈值、模型列表等）
├── database.py       # 数据：SQLite 初始化、写入探测记录、按槽位归并查询
├── monitor.py        # 探测：对模型发起可用性探测、后台定时循环
├── http_server.py    # 接口：提供监控页面与 JSON API（/、/api/status、/api/probe）
├── templates/
│   ├── index.html       # 前端页面（内嵌 CSS/JS），含 __THEME_CLASS__/__MODELS__/__TODAY__ 占位符
│   └── maintenance.html # 独立维护页（后端 /maintenance 路由返回）
├── .env              # 运行配置（见下）
├── Dockerfile        # 容器镜像
└── data/             # SQLite 数据库（挂载卷）
```

## 配置（.env）

> 敏感信息（如 `SUB2API_KEY`）只通过 `.env` 或环境变量注入，不写入代码与仓库。仓库提供 `.env.example` 模板，复制为 `.env` 后填入真实值即可；`.env` 已被 `.gitignore` 排除。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SUB2API_URL` | sub2api 网关地址 | `http://sub2api:8080` |
| `SUB2API_KEY` | API Key（仅服务端使用） | - |
| `DB_PATH` | SQLite 数据库路径 | `/data/model-monitor.db` |
| `PORT` | 服务监听端口 | `8090` |
| `PROBE_INTERVAL_MINUTES` | 探测间隔（分钟） | `5` |
| `SLOW_THRESHOLD_MS` | 慢响应阈值（毫秒） | `10000` |
| `MONITOR_WINDOWS` | 监控时段，多段用逗号分隔，格式 `HH:MM-HH:MM`，含起点与终点 | `08:00-22:00` |
| `MODELS` | 待监控模型，逗号分隔 | 9 个模型 |

修改配置后执行 `docker restart model-monitor` 生效。

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

## 接口

- `GET /`：监控页面（支持 `?theme=dark` 切换暗色）。
- `GET /api/status?date=YYYY-MM-DD`：返回某日各模型的槽位数据。
- `POST /api/probe`：手动触发一次全量探测。
- `GET /maintenance`：独立维护页（也可用于直接访问）。

## 前端说明

页面为单文件 `templates/index.html`（含 CSS/JS）。渲染时由 `http_server.render_index` 替换三个占位符：
- `__THEME_CLASS__`：明暗主题 class。
- `__MODELS__`：模型列表 JSON。
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
