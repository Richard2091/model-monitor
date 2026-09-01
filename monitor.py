# -*- coding: utf-8 -*-
"""探测模块：对 sub2api 的模型发起可用性探测。"""

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import config
import database


def probe_model(model):
    """对单个模型发起最小聊天请求探测。

    @param model 模型 id
    @return 字典：ok(是否成功)、latency_ms、status_code、message
    """
    url = config.SUB2API_URL + "/v1/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + config.API_KEY,
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        ok = 200 <= status < 300
        if ok:
            try:
                parsed = json.loads(body)
                if "choices" not in parsed:
                    ok = False
                    body = "missing choices"
            except Exception:
                ok = False
                body = "invalid json"
        latency = int((time.time() - t0) * 1000)
        return {"ok": ok, "latency_ms": latency, "status_code": status, "message": body[:300]}
    except urllib.error.HTTPError as e:
        latency = int((time.time() - t0) * 1000)
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "latency_ms": latency, "status_code": e.code, "message": body[:300]}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "status_code": 0, "message": str(e)[:300]}


def run_checks():
    """并发对所有模型做一次探测（用于启动自检与手动触发）。"""
    results = []
    with ThreadPoolExecutor(max_workers=len(config.MODELS)) as ex:
        futures = {ex.submit(probe_model, m): m for m in config.MODELS}
        for fut, m in futures.items():
            try:
                r = fut.result()
            except Exception as e:
                r = {"ok": False, "latency_ms": None, "status_code": 0, "message": str(e)[:300]}
            database.save_record(m, r)
            results.append({"model": m, **r})
    order = {m: i for i, m in enumerate(config.MODELS)}
    results.sort(key=lambda x: order.get(x["model"], 0))
    return results


def monitor_loop():
    """后台循环：每隔 interval 分钟，若处于监控时段则探测全部模型。"""
    database.init_db()
    try:
        run_checks()
    except Exception as e:
        print("startup probe error:", e)
    while True:
        time.sleep(config.PROBE_INTERVAL)
        n = config.now_local()
        if config.in_monitor_window(n):
            try:
                run_checks()
            except Exception as e:
                print("probe error:", e)
