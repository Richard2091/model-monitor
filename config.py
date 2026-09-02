# -*- coding: utf-8 -*-
"""配置模块：从 .env 读取并解析所有运行配置。

支持 .env（默认 /app/.env，可用 ENV_FILE 覆盖），环境变量优先。
"""

import logging
import os

logger = logging.getLogger(__name__)


def _read_env(path=None):
    """读取 .env 文件为字典；文件不存在或读取失败则返回空字典并记录警告。

    @param path .env 文件路径，为 None 时使用 ENV_FILE 环境变量或默认值
    @return 包含 .env 键值对的字典
    """
    # 确定配置文件路径
    if path is None:
        path = os.environ.get("ENV_FILE", "/app/.env")
    # 初始化结果字典
    env = {}
    # 逐行解析 .env 文件，过滤注释与空行
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError as e:
        # 读取失败时记录警告，便于排查
        logger.warning("读取 .env 失败: %s", e)
    # 返回解析结果
    return env


def _get(name, default):
    """优先取环境变量（非空），其次 .env，最后默认值。

    @param name 配置项名称
    @param default 默认值
    @return 配置值字符串
    """
    # 环境变量存在且非空时优先使用
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    # 回退到 .env 中的值
    return _env.get(name, default)


def _get_int(name, default):
    """将配置值解析为整数，失败时抛出含配置项名的明确异常。

    @param name 配置项名称
    @param default 默认值（字符串形式）
    @return 整数配置值
    """
    # 获取原始字符串值
    raw = _get(name, default)
    # 尝试解析为整数，失败时给出明确错误
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise ValueError(f"配置项 {name} 的值 '{raw}' 不是有效整数")


# 按顺序读取 .env 配置
_env = _read_env()

# ===== 基础配置 =====
SUB2API_URL = _get("SUB2API_URL", "http://sub2api:8080")
API_KEY = _get("SUB2API_KEY", "")
if not API_KEY:
    # API 密钥为空时记录警告
    logger.warning("SUB2API_KEY is empty, probing will fail until it is set in .env or environment.")
DB_PATH = _get("DB_PATH", "/data/model-monitor.db")
PORT = _get_int("PORT", "8090")
HOST = _get("HOST", "0.0.0.0")
# 手动探测鉴权 Token，空=禁用 /api/probe
PROBE_TOKEN = _get("PROBE_TOKEN", "")
# 管理页登录与密钥加密配置，仅从环境变量或 .env 读取
ADMIN_USERNAME = _get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _get("ADMIN_PASSWORD", "")
ADMIN_SESSION_TTL_MINUTES = _get_int("ADMIN_SESSION_TTL_MINUTES", "480")
ADMIN_IDLE_TIMEOUT_MINUTES = _get_int("ADMIN_IDLE_TIMEOUT_MINUTES", "30")
ADMIN_COOKIE_SECURE = _get("ADMIN_COOKIE_SECURE", "false").lower() == "true"
MODEL_MONITOR_MASTER_KEY = _get("MODEL_MONITOR_MASTER_KEY", "")
MODEL_CONFIG_SOURCE = _get("MODEL_CONFIG_SOURCE", "database")

# ===== 探测配置 =====
# 探测间隔，单位分钟，默认 5
PROBE_INTERVAL_MIN = _get_int("PROBE_INTERVAL_MINUTES", "5")
# 校验探测间隔必须大于 0
if PROBE_INTERVAL_MIN <= 0:
    raise ValueError(f"PROBE_INTERVAL_MINUTES 必须大于 0，当前值为 {PROBE_INTERVAL_MIN}")
PROBE_INTERVAL = PROBE_INTERVAL_MIN * 60
# 慢响应阈值，单位毫秒，默认 10000
SLOW_THRESHOLD_MS = _get_int("SLOW_THRESHOLD_MS", "10000")
# 监控时段：多段，逗号分隔，格式 HH:MM-HH:MM，默认 08:00-22:00，含起点与终点
MONITOR_WINDOWS_STR = _get("MONITOR_WINDOWS", "08:00-22:00")
# 默认监控模型列表；实际运行时优先使用 .env 或环境变量 MODELS 覆盖。
DEFAULT_MODELS = ("gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "qwen3.7-flash", "qwen3.7-plus")


def _parse_models(raw_models):
    """解析逗号分隔的模型配置，去除空白并按首次出现顺序去重。

    @param raw_models 原始模型配置字符串
    @return 去重后的模型名称列表
    """
    # 按逗号拆分模型名称并清理首尾空白
    models = [model.strip() for model in str(raw_models or "").split(",") if model.strip()]
    # 保持用户配置顺序，同时去除重复模型
    return list(dict.fromkeys(models))


# 从 .env 或环境变量读取模型列表，支持随时增删改多个厂商模型
MODELS = _parse_models(_get("MODELS", ",".join(DEFAULT_MODELS)))
# 模型列表为空时记录警告
if not MODELS:
    logger.warning("MODELS is empty, no models will be probed.")


def _parse_time(hhmm):
    """解析 HH:MM 为当日分钟数，校验小时与分钟范围。

    @param hhmm 时间字符串，格式 HH:MM
    @return 当日分钟数（0-1439）
    """
    # 按冒号分割小时与分钟
    h, m = hhmm.split(":")
    # 解析并校验小时范围
    hour = int(h)
    minute = int(m)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError(f"时间 '{hhmm}' 无效：小时须在 0-23，分钟须在 0-59")
    # 返回当日分钟数
    return hour * 60 + minute


def _parse_windows(s):
    """解析 MONITOR_WINDOWS 为 [(start_min, end_min), ...]（含端点）。

    校验 start < end（不支持跨午夜），跨午夜时明确报错。

    @param s 逗号分隔的时间段字符串
    @return 时间段列表
    """
    # 初始化结果列表
    wins = []
    # 逐段解析时间范围
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        start = _parse_time(a.strip())
        end = _parse_time(b.strip())
        # 校验 start < end，不支持跨午夜
        if start >= end:
            raise ValueError(f"监控时段 '{part}' 无效：start 必须小于 end（不支持跨午夜），请拆分为多段")
        wins.append((start, end))
    # 无有效时段时回退到默认值
    if not wins:
        wins = [(8 * 60, 22 * 60)]
    # 返回解析结果
    return wins


MONITOR_WINDOWS = _parse_windows(MONITOR_WINDOWS_STR)
# 整个时间轴起点与终点（所有窗口合并）
TIME_START = min(w[0] for w in MONITOR_WINDOWS)
TIME_END = max(w[1] for w in MONITOR_WINDOWS)
# 含端点的槽位总数
TOTAL_SLOTS = (TIME_END - TIME_START) // PROBE_INTERVAL_MIN + 1


def slot_time_minutes(i):
    """第 i 个槽位对应的当日分钟数。

    @param i 槽位索引（从 0 开始）
    @return 当日分钟数
    """
    # 按槽位间隔计算对应分钟数
    return TIME_START + i * PROBE_INTERVAL_MIN


def in_window(minutes):
    """判断某分钟是否落在任一监控窗口内（含端点）。

    @param minutes 当日分钟数
    @return 是否在监控窗口内
    """
    # 逐一检查每个监控时段
    for s, e in MONITOR_WINDOWS:
        if s <= minutes <= e:
            return True
    # 不在任何窗口内
    return False


def in_monitor_window(dt):
    """判断当前时间是否落在任一监控窗口内（含端点）。

    @param dt datetime 对象
    @return 是否在监控窗口内
    """
    # 将 datetime 转换为分钟数后判断
    return in_window(dt.hour * 60 + dt.minute)


def now_local():
    """获取当前本地时间（依赖容器内 TZ=Asia/Shanghai）。

    @return datetime 对象
    """
    # 导入 datetime 并返回当前本地时间
    from datetime import datetime
    return datetime.now()
