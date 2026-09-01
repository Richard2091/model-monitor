# -*- coding: utf-8 -*-
"""探测模块：对 sub2api 的模型发起可用性探测。"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List

import config
import database

logger = logging.getLogger(__name__)

# 模块级探测互斥锁，串行化后台循环与 HTTP 手动触发
_probe_lock = threading.Lock()


def probe_model(model):
    """对单个模型发起最小聊天请求探测。

    @param model 模型 id
    @return 字典：ok(是否成功)、latency_ms、status_code、message
    """
    # 构建探测请求 URL 与请求体
    url = config.SUB2API_URL + "/v1/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    data = json.dumps(payload).encode("utf-8")
    # 构建带鉴权的 HTTP 请求
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + config.API_KEY,
    })
    # 记录开始时间用于计算延迟
    t0 = time.time()
    # 发送请求并解析响应
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        # 判断 HTTP 状态码是否成功
        ok = 200 <= status < 300
        if ok:
            # 成功响应时校验返回体中是否包含 choices
            try:
                parsed = json.loads(body)
                if "choices" not in parsed:
                    ok = False
                    body = "missing choices"
            except (ValueError, KeyError):
                ok = False
                body = "invalid json"
        # 计算延迟并返回结果
        latency = int((time.time() - t0) * 1000)
        return {"ok": ok, "latency_ms": latency, "status_code": status, "message": body[:300]}
    except urllib.error.HTTPError as e:
        # HTTP 错误响应：读取错误体并返回失败结果
        latency = int((time.time() - t0) * 1000)
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "latency_ms": latency, "status_code": e.code, "message": body[:300]}
    except Exception as e:
        # 其他网络异常：返回失败结果
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "status_code": 0, "message": str(e)[:300]}


def run_checks():
    """并发对所有模型做一次探测（用于启动自检与手动触发）。

    全程持有互斥锁，避免后台循环与 HTTP 手动触发同时执行。

    @return 探测结果列表
    """
    # 空模型列表直接返回空结果
    if not config.MODELS:
        return []
    # 获取互斥锁，串行化探测
    with _probe_lock:
        # 初始化结果列表
        results = []
        # 并发提交探测任务
        with ThreadPoolExecutor(max_workers=len(config.MODELS)) as ex:
            futures = {ex.submit(probe_model, m): m for m in config.MODELS}
            # 收集探测结果并写入数据库
            for fut, m in futures.items():
                try:
                    r = fut.result()
                except Exception as e:
                    # 探测任务异常时构造失败结果
                    logger.exception("探测模型 %s 异常: %s", m, e)
                    r = {"ok": False, "latency_ms": None, "status_code": 0, "message": str(e)[:300]}
                # 保存记录到数据库
                database.save_record(m, r)
                # 追加到结果列表
                results.append({"model": m, **r})
        # 按配置顺序排序结果
        order = {m: i for i, m in enumerate(config.MODELS)}
        results.sort(key=lambda x: order.get(x["model"], 0))
        # 返回排序后的结果
        return results


def _next_probe_time(now: datetime) -> float:
    """计算当前时间之后最近的合法监控槽位时间戳。

    @param now 当前本地时间
    @return 下一次监控槽位的 Unix 时间戳
    """
    # 从当天开始向后查找，最多覆盖下一天的监控槽位
    for day_offset in range(2):
        target_date = (now + timedelta(days=day_offset)).date()
        # 遍历当天所有配置槽位
        for index in range(config.TOTAL_SLOTS):
            slot_minutes = config.slot_time_minutes(index)
            if not config.in_window(slot_minutes):
                continue
            candidate = datetime.combine(
                target_date,
                datetime.min.time(),
            ) + timedelta(minutes=slot_minutes)
            # 只返回当前时间之后的槽位，避免进程启动时立即探测
            if candidate > now:
                return candidate.timestamp()
    # 正常配置不会走到这里，兜底返回下一间隔后的时间
    return now.timestamp() + config.PROBE_INTERVAL


def monitor_loop() -> None:
    """后台循环：按监控槽位对齐调度并探测全部模型。

    仅在监控时段内执行探测；每轮重新计算下一合法槽位，避免探测耗时
    超过间隔时连续补偿执行。

    @return None
    """
    # 计算进程启动后最近的合法监控槽位
    next_run = _next_probe_time(config.now_local())
    while True:
        # 睡眠至下一合法槽位，避免固定相对间隔造成漂移
        time.sleep(max(0, next_run - time.time()))
        try:
            # 到达槽位后执行一次探测
            run_checks()
        except Exception:
            # 捕获异常并继续计算下一槽位，防止后台线程静默退出
            logger.exception("探测循环异常")
        # 按当前时间重新查找下一槽位，跳过已经过期的槽位
        next_run = _next_probe_time(config.now_local())
