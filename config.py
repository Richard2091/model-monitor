# -*- coding: utf-8 -*-
"""配置模块：从 .env 读取并解析所有运行配置。

支持 .env（默认 /app/.env，可用 ENV_FILE 覆盖），环境变量优先。
"""

import os


def _read_env(path=None):
    """读取 .env 文件为字典；文件不存在则返回空字典。"""
    if path is None:
        path = os.environ.get("ENV_FILE", "/app/.env")
    env = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _get(name, default):
    """优先取环境变量，其次 .env，最后默认值。"""
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    return _env.get(name, default)


_env = _read_env()

# ===== 基础配置 =====
SUB2API_URL = _get("SUB2API_URL", "http://sub2api:8080")
API_KEY = _get("SUB2API_KEY", "")
if not API_KEY:
    print("[config] WARNING: SUB2API_KEY is empty, probing will fail until it is set in .env or environment.")
DB_PATH = _get("DB_PATH", "/data/model-monitor.db")
PORT = int(_get("PORT", "8090"))

# ===== 探测配置 =====
# 探测间隔，单位分钟，默认 5
PROBE_INTERVAL_MIN = int(_get("PROBE_INTERVAL_MINUTES", "5"))
PROBE_INTERVAL = PROBE_INTERVAL_MIN * 60
# 慢响应阈值，单位毫秒，默认 10000
SLOW_THRESHOLD_MS = int(_get("SLOW_THRESHOLD_MS", "10000"))
# 监控时段：多段，逗号分隔，格式 HH:MM-HH:MM，默认 08:00-22:00，含起点与终点
MONITOR_WINDOWS_STR = _get("MONITOR_WINDOWS", "08:00-22:00")
# 待监控模型列表，逗号分隔
MODELS = [m.strip() for m in _get("MODELS", "deepseek-v4-flash,deepseek-v4-flash-vision-exp,glm-5.3-flash,gpt-5.4,gpt-5.5,gpt-5.6-sol,gpt-5.6-terra,qwen3.7-flash,qwen3.7-plus").split(",") if m.strip()]


def _parse_time(hhmm):
    """解析 HH:MM 为当日分钟数。"""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _parse_windows(s):
    """解析 MONITOR_WINDOWS 为 [(start_min, end_min), ...]（含端点）。"""
    wins = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        wins.append((_parse_time(a.strip()), _parse_time(b.strip())))
    if not wins:
        wins = [(8 * 60, 22 * 60)]
    return wins


MONITOR_WINDOWS = _parse_windows(MONITOR_WINDOWS_STR)
# 整个时间轴起点与终点（所有窗口合并）
TIME_START = min(w[0] for w in MONITOR_WINDOWS)
TIME_END = max(w[1] for w in MONITOR_WINDOWS)
# 含端点的槽位总数
TOTAL_SLOTS = (TIME_END - TIME_START) // PROBE_INTERVAL_MIN + 1


def slot_time_minutes(i):
    """第 i 个槽位对应的当日分钟数。"""
    return TIME_START + i * PROBE_INTERVAL_MIN


def in_window(minutes):
    """判断某分钟是否落在任一监控窗口内（含端点）。"""
    for s, e in MONITOR_WINDOWS:
        if s <= minutes <= e:
            return True
    return False


def in_monitor_window(dt):
    """判断当前时间是否落在任一监控窗口内（含端点）。"""
    return in_window(dt.hour * 60 + dt.minute)


def now_local():
    """获取当前本地时间（依赖容器内 TZ=Asia/Shanghai）。"""
    from datetime import datetime
    return datetime.now()
