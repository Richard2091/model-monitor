# -*- coding: utf-8 -*-
"""HTTP 服务：提供监控页面与 JSON API。"""

import json
import logging
import os
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import config
import database
import monitor

logger = logging.getLogger(__name__)

# 前端模板路径（index.html 内含 __THEME_CLASS__ / __TODAY__ 占位）
_TEMPLATE_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
_TEMPLATE_MAINTENANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "maintenance.html")


def render_index(theme):
    """渲染监控页面 HTML。

    @param theme light/dark
    @return 渲染后的 HTML 字符串，模板读取失败时返回 None
    """
    # 读取模板文件，失败时记录异常并返回 None
    try:
        with open(_TEMPLATE_INDEX, "r", encoding="utf-8") as f:
            page = f.read()
    except OSError:
        logger.exception("读取 index 模板失败")
        return None
    # 获取当前日期并构建 JS Date 表达式
    t = config.now_local()
    today_js = "new Date(%d,%d,%d)" % (t.year, t.month - 1, t.day)
    # 确定主题样式类名
    dark = "dark" if theme == "dark" else ""
    # 替换模板占位符并返回结果
    return page.replace("__THEME_CLASS__", dark).replace("__TODAY__", today_js)


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理：页面与 API。"""

    def _json(self, obj, code=200):
        """输出 JSON 响应。

        @param obj 待序列化为 JSON 的对象
        @param code HTTP 状态码
        @return None
        """
        # 序列化并编码响应体
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # 发送响应头
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # 写入响应体，客户端提前断开时静默忽略
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _html(self, content, code=200):
        """输出 HTML 响应。

        @param content HTML 字符串
        @param code HTTP 状态码
        @return None
        """
        # 编码响应体
        body = content.encode("utf-8")
        # 发送响应头
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # 写入响应体，客户端提前断开时静默忽略
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        """处理 GET 请求：页面与 /api/status。

        @return None
        """
        # 解析 URL 路径与查询参数
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # 获取主题参数
        theme = (qs.get("theme") or ["light"])[0]
        # 路由分发
        if parsed.path in ("/", "/model-monitor", "/model-monitor/"):
            # 渲染监控页面
            html = render_index("dark" if theme in ("dark",) else "light")
            if html is None:
                self._json({"error": "template load failed"}, 500)
            else:
                self._html(html)
        elif parsed.path in ("/maintenance", "/model-monitor/maintenance"):
            # 读取维护页模板，失败时返回 500
            try:
                with open(_TEMPLATE_MAINTENANCE, "r", encoding="utf-8") as f:
                    self._html(f.read())
            except OSError:
                logger.exception("读取维护页模板失败")
                self._json({"error": "template load failed"}, 500)
        elif parsed.path == "/api/status":
            # 获取查询日期参数
            d = (qs.get("date") or [config.now_local().strftime("%Y-%m-%d")])[0]
            # 校验日期格式
            try:
                date.fromisoformat(d)
            except ValueError:
                self._json({"error": "invalid date"}, 400)
                return
            # 返回状态数据
            self._json(database.build_query(d))
        else:
            # 未匹配的路由返回 404
            self._html("Not Found", 404)

    def do_POST(self):
        """处理 POST 请求：/api/probe 手动触发探测（需 Token 鉴权）。

        @return None
        """
        # 解析 URL 路径
        parsed = urlparse(self.path)
        if parsed.path == "/api/probe":
            # 检查 PROBE_TOKEN 是否已配置
            if not config.PROBE_TOKEN:
                self._json({"error": "probe disabled: PROBE_TOKEN not set"}, 403)
                return
            # 验证请求头中的 Token
            token = self.headers.get("X-Monitor-Token", "")
            if token != config.PROBE_TOKEN:
                self._json({"error": "invalid token"}, 403)
                return
            # Token 验证通过，执行探测
            try:
                results = monitor.run_checks()
                self._json({"ok": True, "results": results})
            except Exception as e:
                logger.exception("手动探测异常")
                self._json({"ok": False, "error": "手动探测失败，请查看服务端日志"}, 500)
        else:
            # 未匹配的路由返回 404
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        """屏蔽默认访问日志。

        @param fmt 格式字符串
        @param args 格式参数
        @return None
        """
        pass
