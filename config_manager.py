# -*- coding: utf-8 -*-
"""运行配置快照：统一提供数据库配置和动态刷新。"""

from dataclasses import dataclass
import threading
import json
import logging

import config
import database
from security import decrypt_secret, SecretError


@dataclass(frozen=True)
class ModelTarget:
    """单个可探测模型及其所属厂商连接信息。"""
    model_key: str
    display_name: str | None
    vendor_key: str
    vendor_display_name: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class ConfigSnapshot:
    """一轮探测使用的不可变配置快照。"""
    generation: int
    models: tuple[ModelTarget, ...]
    windows: tuple[tuple[int, int], ...]
    probe_interval_min: int
    slow_threshold_ms: int


_lock = threading.RLock()
_reload_event = threading.Event()
_snapshot = None
logger = logging.getLogger(__name__)


def _parse_windows(raw):
    """将数据库 JSON 时间段解析为分钟元组。"""
    # 解析时间段 JSON 并复用现有时间校验
    windows = []
    for item in json.loads(raw or "[]"):
        start = config._parse_time(item["start"])
        end = config._parse_time(item["end"])
        if start >= end:
            raise ValueError("监控时间段无效")
        windows.append((start, end))
    if not windows:
        raise ValueError("至少需要一个监控时间段")
    return tuple(windows)


def load_snapshot():
    """从 SQLite 加载启用配置并构造新的不可变快照。

    @return ConfigSnapshot 当前有效运行配置
    """
    # 根据应急回退开关选择旧环境配置或数据库配置
    if config.MODEL_CONFIG_SOURCE == "legacy-env":
        windows = tuple(config.MONITOR_WINDOWS)
        targets = tuple(ModelTarget(model, None, "default-gateway", "默认网关", config.SUB2API_URL.rstrip("/"), config.API_KEY) for model in config.MODELS)
        with _lock:
            generation = (_snapshot.generation + 1) if _snapshot else 1
            snapshot = ConfigSnapshot(generation, targets, windows, config.PROBE_INTERVAL_MIN, config.SLOW_THRESHOLD_MS)
            globals()["_snapshot"] = snapshot
            return snapshot
    # 读取数据库中的厂商、模型和全局设置
    vendors, model_rows, settings = database.load_runtime_rows()
    if not settings:
        raise RuntimeError("监控设置不存在")
    # 解析全局设置并建立启用厂商索引
    windows = _parse_windows(settings[0])
    vendor_enabled = {row[1]: row for row in vendors if row[7]}
    targets = []
    for row in model_rows:
        model_key, display_name, model_enabled, vendor_key, vendor_name, base_url, cipher, nonce, version, vendor_enabled_flag = row
        if not model_enabled or not vendor_enabled_flag:
            continue
        if vendor_key not in vendor_enabled:
            continue
        api_key = ""
        if not cipher:
            # 保留配置目标供管理页和历史查询展示，但运行探测会过滤无密钥目标
            logger.warning("厂商 %s 未配置 API Key，运行探测将跳过", vendor_key)
        if cipher:
            try:
                # 解密当前厂商密钥；单个厂商失败时仅跳过该厂商，避免拖垮整个服务
                if not config.MODEL_MONITOR_MASTER_KEY:
                    raise SecretError("MODEL_MONITOR_MASTER_KEY 未配置")
                api_key = decrypt_secret(cipher, nonce, config.MODEL_MONITOR_MASTER_KEY, f"model-monitor/vendor/{vendor_key}/api-key/v{version}")
            except SecretError as exc:
                logger.error("厂商 %s 的 API Key 不可用，运行探测将跳过: %s", vendor_key, exc)
                api_key = ""
        targets.append(ModelTarget(model_key, display_name, vendor_key, vendor_name, base_url, api_key))
    with _lock:
        generation = (_snapshot.generation + 1) if _snapshot else 1
        snapshot = ConfigSnapshot(generation, tuple(targets), windows, int(settings[1]), int(settings[2]))
        globals()["_snapshot"] = snapshot
        _reload_event.set()
        _reload_event.clear()
        return snapshot


def initialize():
    """初始化并加载首次运行配置快照。"""
    # 完成数据库迁移后加载有效运行配置
    return load_snapshot()


def get_snapshot():
    """线程安全地获取当前配置快照。"""
    # 首次访问时同步初始化快照
    with _lock:
        if _snapshot is None:
            return load_snapshot()
        return _snapshot


def reload():
    """保存管理配置后重新加载快照并通知调度线程。"""
    # 重新读取数据库并唤醒后台调度
    snapshot = load_snapshot()
    _reload_event.set()
    return snapshot


def wait_reload(timeout):
    """等待配置变更事件或超时，供调度线程重新计算时间。"""
    # 等待配置变更通知，并消费事件
    changed = _reload_event.wait(timeout)
    if changed:
        _reload_event.clear()
    return changed
