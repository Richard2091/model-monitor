# -*- coding: utf-8 -*-
"""数据库模块：SQLite 初始化与探测记录查询。"""

import sqlite3
from datetime import datetime

import config


def init_db():
    """初始化 SQLite 数据库，创建探测记录表。"""
    conn = sqlite3.connect(config.DB_PATH)
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_model_time ON probe_records(model, probed_at)")
    conn.commit()
    conn.close()


def save_record(model, result):
    """将一次探测结果写入数据库。"""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO probe_records(model, probed_at, ok, latency_ms, status_code, message) VALUES (?,?,?,?,?,?)",
        (model, config.now_local().isoformat(), 1 if result["ok"] else 0,
         result["latency_ms"], result["status_code"], result["message"]),
    )
    conn.commit()
    conn.close()


def build_query(date_str):
    """按 interval 分钟槽位归并某日的探测记录，支持多监控时段。

    @param date_str 日期，格式 YYYY-MM-DD
    @return 包含各模型槽位数据的字典
    """
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    out = []
    for model in config.MODELS:
        rows = cur.execute(
            "SELECT probed_at, ok, latency_ms, status_code, message FROM probe_records "
            "WHERE model=? AND substr(probed_at,1,10)=? ORDER BY probed_at",
            (model, date_str),
        ).fetchall()
        by_slot = {}
        for probed_at, ok, latency, status, msg in rows:
            try:
                dt = datetime.fromisoformat(probed_at)
            except Exception:
                continue
            minutes = dt.hour * 60 + dt.minute
            idx = round((minutes - config.TIME_START) / config.PROBE_INTERVAL_MIN)
            mslot = config.TIME_START + idx * config.PROBE_INTERVAL_MIN
            if 0 <= idx < config.TOTAL_SLOTS and config.in_window(mslot):
                by_slot[idx] = {"ok": bool(ok), "latency_ms": latency, "status_code": status, "message": msg}
        model_data = []
        for i in range(config.TOTAL_SLOTS):
            mslot = config.TIME_START + i * config.PROBE_INTERVAL_MIN
            time_str = "%02d:%02d" % (mslot // 60, mslot % 60)
            if i in by_slot:
                model_data.append({"time": time_str, **by_slot[i]})
            else:
                model_data.append({"time": time_str, "ok": None, "latency_ms": None, "status_code": None, "message": ""})
        out.append({"model": model, "slots": model_data})
    conn.close()
    return {
        "date": date_str,
        "interval": config.PROBE_INTERVAL_MIN,
        "slow_ms": config.SLOW_THRESHOLD_MS,
        "windows": config.MONITOR_WINDOWS_STR,
        "models": out,
    }
