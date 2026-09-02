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
import config_manager

logger = logging.getLogger(__name__)

# 模块级探测互斥锁，串行化后台循环与 HTTP 手动触发
_probe_lock = threading.Lock()


def probe_lock():
    """返回探测轮次互斥锁，供删除配置时避免并发写回历史。

    @return 可用于 with 语句的探测互斥锁。
    """
    # 返回统一的轮次锁，确保探测与删除操作不会交错
    return _probe_lock


def probe_model(target):
    """对单个厂商模型发起最小聊天请求探测。

    @param target 模型与厂商连接配置
    @return 字典：ok(是否成功)、latency_ms、status_code、message
    """
    # 使用快照中的厂商地址和模型 ID 构建探测请求
    url = target.base_url.rstrip("/") + "/v1/chat/completions"
    payload = {"model": target.model_key, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    data = json.dumps(payload).encode("utf-8")
    # 构建带鉴权的 HTTP 请求
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + target.api_key,
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
    """使用同一份配置快照并发探测全部启用模型。

    @return 探测结果列表
    """
    # 兼容旧调用方对 config.MODELS 的空列表短路约定
    if config.MODEL_CONFIG_SOURCE == 'legacy-env' and not config.MODELS:
        return []
    # 在一轮探测开始时固定配置快照，避免中途混用新旧配置
    snapshot = config_manager.get_snapshot()
    if not snapshot.models:
        return []
    # 过滤缺少或无法解密 API Key 的目标，避免向上游发送空 Bearer Token
    probe_targets = tuple(target for target in snapshot.models if target.api_key)
    if not probe_targets:
        return []
    with _probe_lock:
        results = []
        with ThreadPoolExecutor(max_workers=len(probe_targets)) as ex:
            futures = {ex.submit(probe_model, target): target for target in probe_targets}
            for fut, target in futures.items():
                try:
                    result = fut.result()
                except Exception as exc:
                    logger.exception("探测模型 %s 异常: %s", target.model_key, exc)
                    result = {"ok": False, "latency_ms": None, "status_code": 0, "message": str(exc)[:300]}
                # 保存当前轮次的模型结果，不影响历史兼容
                database.save_record(target.model_key, result)
                results.append({"model": target.model_key, **result})
        order = {target.model_key: index for index, target in enumerate(probe_targets)}
        results.sort(key=lambda item: order.get(item["model"], 0))
        return results

def _next_probe_time(now: datetime, snapshot=None) -> float:
    """计算当前时间之后最近的合法监控槽位时间戳。

    @param now 当前本地时间
    @return 下一次监控槽位的 Unix 时间戳
    """
    # 未传快照时读取当前快照
    snapshot = snapshot or config_manager.get_snapshot()
    time_start = min(window[0] for window in snapshot.windows)
    time_end = max(window[1] for window in snapshot.windows)
    total_slots = (time_end - time_start) // snapshot.probe_interval_min + 1
    # 从当天开始向后查找，最多覆盖下一天的监控槽位
    for day_offset in range(2):
        target_date = (now + timedelta(days=day_offset)).date()
        # 遍历当天所有配置槽位
        for index in range(total_slots):
            slot_minutes = time_start + index * snapshot.probe_interval_min
            if not any(start <= slot_minutes <= end for start, end in snapshot.windows):
                continue
            candidate = datetime.combine(
                target_date,
                datetime.min.time(),
            ) + timedelta(minutes=slot_minutes)
            # 只返回当前时间之后的槽位，避免进程启动时立即探测
            if candidate > now:
                return candidate.timestamp()
    # 正常配置不会走到这里，兜底返回下一间隔后的时间
    return now.timestamp() + snapshot.probe_interval_min * 60


def monitor_loop() -> None:
    """后台循环：按监控槽位对齐调度并探测全部模型。

    仅在监控时段内执行探测；每轮重新计算下一合法槽位，避免探测耗时
    超过间隔时连续补偿执行。

    @return None
    """
    # 计算进程启动后最近的合法监控槽位
    while True:
        # 每轮读取一份快照，配置修改事件会提前唤醒等待
        snapshot = config_manager.get_snapshot()
        next_run = _next_probe_time(config.now_local(), snapshot)
        wait_seconds = max(0, next_run - time.time())
        if config_manager.wait_reload(wait_seconds):
            continue
        try:
            # 到达槽位后执行一次探测
            run_checks()
        except Exception:
            # 捕获异常并继续计算下一槽位，防止后台线程静默退出
            logger.exception("探测循环异常")
