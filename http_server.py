# -*- coding: utf-8 -*-
"""HTTP 服务：提供监控页面与 JSON API。"""

import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import config
import database
import monitor

# 前端模板路径（index.html 内含 __THEME_CLASS__ / __MODELS__ / __TODAY__ 占位）
_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
_MAINTENANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "maintenance.html")


def render_index(theme):
    """渲染监控页面 HTML。

    @param theme light/dark
    @return 渲染后的 HTML 字符串
    """
    with open(_TEMPLATE, "r", encoding="utf-8") as f:
        page = f.read()
    models_json = json.dumps(config.MODELS, ensure_ascii=False)
    t = config.now_local()
    today_js = "new Date(%d,%d,%d)" % (t.year, t.month - 1, t.day)
    dark = "dark" if theme == "dark" else ""
    return page.replace("__THEME_CLASS__", dark).replace("__MODELS__", models_json).replace("__TODAY__", today_js)


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理：页面与 API。"""

    def _json(self, obj, code=200):
        """输出 JSON 响应。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content, code=200):
        """输出 HTML 响应。"""
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """处理 GET 请求：页面与 /api/status。"""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        theme = (qs.get("theme") or ["light"])[0]
        if parsed.path in ("/", "/model-monitor", "/model-monitor/"):
            self._html(render_index("dark" if theme in ("dark",) else "light"))
        elif parsed.path in ("/maintenance", "/model-monitor/maintenance"):
            with open(_MAINTENANCE, "r", encoding="utf-8") as f:
                self._html(f.read())
        elif parsed.path == "/api/status":
            d = (qs.get("date") or [config.now_local().strftime("%Y-%m-%d")])[0]
            try:
                date.fromisoformat(d)
            except Exception:
                self._json({"error": "invalid date"}, 400)
                return
            self._json(database.build_query(d))
        else:
            self._html("Not Found", 404)

    def do_POST(self):
        """处理 POST 请求：/api/probe 手动触发探测。"""
        parsed = urlparse(self.path)
        if parsed.path == "/api/probe":
            try:
                results = monitor.run_checks()
                self._json({"ok": True, "results": results})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        """屏蔽默认访问日志。"""
        pass
