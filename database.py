# -*- coding: utf-8 -*-
"""数据库模块：SQLite 初始化与探测记录查询。"""

import logging
import sqlite3
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def init_db():
    """初始化 SQLite 数据库，创建探测记录表并启用 WAL 模式。

    @return None
    """
    # 建立数据库连接（超时 10 秒，启用 WAL 提高并发性能）
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        # 启用 WAL 模式，减少写锁冲突
        conn.execute("PRAGMA journal_mode=WAL")
        # 创建探测记录表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS probe_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                probed_at TEXT NOT NULL,
                ok INTEGER NOT NULL,
                latency_ms INTEGER,
                status_code INTEGER,
                message TEXT
            )
        """)
        # 创建模型与时间复合索引，加速查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_model_time ON probe_records(model, probed_at)")
        # 提交事务
        conn.commit()
    finally:
        # 确保连接关闭
        conn.close()


def save_record(model, result):
    """将一次探测结果写入数据库。

    @param model 模型 id
    @param result 探测结果字典
    @return None
    """
    # 建立数据库连接
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        # 插入探测记录
        conn.execute(
            "INSERT INTO probe_records(model, probed_at, ok, latency_ms, status_code, message) VALUES (?,?,?,?,?,?)",
            (model, config.now_local().isoformat(), 1 if result["ok"] else 0,
             result["latency_ms"], result["status_code"], result["message"]),
        )
        # 提交事务
        conn.commit()
    finally:
        # 确保连接关闭
        conn.close()


def build_query(date_str):
    """按 interval 分钟槽位归并某日的探测记录，支持多监控时段。

    @param date_str 日期，格式 YYYY-MM-DD
    @return 包含各模型槽位数据的字典
    """
    # 建立数据库连接
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        # 创建游标
        cur = conn.cursor()
        # 初始化输出列表
        out = []
        # 遍历所有模型查询记录
        for model in config.MODELS:
            # 按模型与日期查询探测记录
            rows = cur.execute(
                "SELECT probed_at, ok, latency_ms, status_code, message FROM probe_records "
                "WHERE model=? AND substr(probed_at,1,10)=? ORDER BY probed_at",
                (model, date_str),
            ).fetchall()
            # 初始化槽位映射
            by_slot = {}
            # 逐条解析记录并归并到槽位
            for probed_at, ok, latency, status, msg in rows:
                # 解析 ISO 时间，失败时记录警告并跳过
                try:
                    dt = datetime.fromisoformat(probed_at)
                except ValueError:
                    logger.warning("跳过无法解析的时间记录: model=%s probed_at=%s", model, probed_at)
                    continue
                # 计算当日分钟数
                minutes = dt.hour * 60 + dt.minute
                # 计算槽位索引
                idx = round((minutes - config.TIME_START) / config.PROBE_INTERVAL_MIN)
                # 使用 config 的槽位函数统一计算槽位分钟
                if 0 <= idx < config.TOTAL_SLOTS:
                    mslot = config.slot_time_minutes(idx)
                    if config.in_window(mslot):
                        by_slot[idx] = {"ok": bool(ok), "latency_ms": latency, "status_code": status, "message": msg}
            # 构建该模型的槽位数据列表
            model_data = []
            for i in range(config.TOTAL_SLOTS):
                # 计算槽位时间字符串
                mslot = config.slot_time_minutes(i)
                time_str = "%02d:%02d" % (mslot // 60, mslot % 60)
                # 有数据则填充实际值，无数据则填充空值
                if i in by_slot:
                    model_data.append({"time": time_str, **by_slot[i]})
                else:
                    model_data.append({"time": time_str, "ok": None, "latency_ms": None, "status_code": None, "message": ""})
            # 追加到输出列表
            out.append({"model": model, "slots": model_data})
        # 组装返回结果
        return {
            "date": date_str,
            "interval": config.PROBE_INTERVAL_MIN,
            "slow_ms": config.SLOW_THRESHOLD_MS,
            "windows": config.MONITOR_WINDOWS_STR,
            "models": out,
        }
    finally:
        # 确保连接关闭
        conn.close()
