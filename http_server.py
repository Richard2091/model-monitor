# -*- coding: utf-8 -*-
"""HTTP 服务：监控页面、管理页面与 JSON API。"""

import json
import logging
import hmac
import os
import secrets
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import config
import database
import monitor
import config_manager
from security import hash_token, verify_admin_password, SecretError

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_INDEX = os.path.join(BASE_DIR, "templates", "index.html")
_TEMPLATE_ADMIN = os.path.join(BASE_DIR, "templates", "admin.html")
_TEMPLATE_LOGIN = os.path.join(BASE_DIR, "templates", "admin_login.html")
_TEMPLATE_MAINTENANCE = os.path.join(BASE_DIR, "templates", "maintenance.html")
_login_failures = {}


def _read_template(path, theme="light"):
    """读取 HTML 模板并替换主题占位符。"""
    # 读取模板并应用主题类
    try:
        with open(path, "r", encoding="utf-8") as file:
            page = file.read()
    except OSError:
        logger.exception("读取模板失败: %s", path)
        return None
    return page.replace("__THEME_CLASS__", "dark" if theme == "dark" else "")


def render_index(theme):
    """渲染监控页面 HTML。"""
    # 读取首页并注入本地日期
    page = _read_template(_TEMPLATE_INDEX, theme)
    if page is None:
        return None
    now = config.now_local()
    return page.replace("__TODAY__", f"new Date({now.year},{now.month - 1},{now.day})")


def _cookie_value(headers, name):
    """从 Cookie 请求头读取指定值。"""
    # 拆分 Cookie 键值并返回目标令牌
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def _json_body(handler):
    """读取并解析限制大小的 JSON 请求体。"""
    # 限制请求体大小，避免管理接口被超大请求消耗资源
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > 1024 * 1024:
        raise ValueError("请求内容无效")
    # 解析 UTF-8 JSON
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _session(handler):
    """读取并校验当前管理员会话。"""
    # 从 HttpOnly Cookie 取令牌并查询服务端会话
    token = _cookie_value(handler.headers, "mm_session")
    if not token:
        return None, ""
    return database.get_session(hash_token(token)), token


def _csrf_ok(handler, session):
    """校验管理写请求的 CSRF 令牌。"""
    # 使用会话中保存的 CSRF 哈希验证请求头
    csrf = handler.headers.get("X-CSRF-Token", "")
    return bool(csrf) and hmac.compare_digest(hash_token(csrf), session["csrf_token_hash"])


def _admin_required(handler):
    """统一检查管理 API 登录状态。"""
    # 未登录请求返回 401，调用方据此跳转登录页
    session, token = _session(handler)
    if not session:
        handler._json({"error": "未登录或会话已过期"}, 401)
        return None
    return session


def _set_cookie(handler, token, max_age):
    """设置管理会话 Cookie。"""
    # 设置 HttpOnly、SameSite 和可选 Secure 属性
    secure = "; Secure" if config.ADMIN_COOKIE_SECURE else ""
    handler.send_header("Set-Cookie", f"mm_session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}")


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    def _json(self, obj, code=200, extra_headers=None):
        """输出 JSON 响应。"""
        # 序列化响应并发送安全缓存策略
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items(): self.send_header(key, value)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass

    def _html(self, content, code=200):
        """输出 HTML 响应。"""
        # 编码并发送页面内容
        body = content.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass

    def do_GET(self):
        """处理监控页面、管理页面和读取接口。"""
        # 解析路径和查询参数
        parsed = urlparse(self.path); qs = parse_qs(parsed.query); theme = (qs.get("theme") or ["light"])[0]
        if parsed.path in ("/", "/model-monitor", "/model-monitor/"):
            page = render_index("dark" if theme == "dark" else "light")
            return self._json({"error": "页面模板加载失败"}, 500) if page is None or not page.strip() else self._html(page)
        if parsed.path in ("/model-monitor-admin/login",):
            page = _read_template(_TEMPLATE_LOGIN, theme)
            return self._json({"error": "页面模板加载失败"}, 500) if page is None or not page.strip() else self._html(page)
        if parsed.path in ("/model-monitor-admin", "/model-monitor-admin/"):
            session, _ = _session(self)
            if not session:
                # 未登录时跳转到固定登录地址，避免地址栏停留在管理页
                self.send_response(302)
                self.send_header("Location", "/model-monitor-admin/login")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            page = _read_template(_TEMPLATE_ADMIN, theme)
            return self._json({"error": "管理页面模板为空或加载失败"}, 500) if page is None or not page.strip() else self._html(page)
        if parsed.path in ("/maintenance", "/model-monitor/maintenance"):
            page = _read_template(_TEMPLATE_MAINTENANCE, theme)
            return self._json({"error": "页面模板加载失败"}, 500) if page is None or not page.strip() else self._html(page)
        if parsed.path == "/api/status":
            d = (qs.get("date") or [config.now_local().strftime("%Y-%m-%d")])[0]
            try: date.fromisoformat(d)
            except ValueError: return self._json({"error": "invalid date"}, 400)
            return self._json(database.build_query(d))
        if parsed.path == "/api/admin/session":
            session, token = _session(self)
            if not session:
                return self._json({"authenticated": False})
            # 每次页面恢复时轮换 CSRF 令牌，不在数据库保存明文
            csrf = secrets.token_urlsafe(32)
            database.rotate_csrf(hash_token(token), hash_token(csrf))
            return self._json({"authenticated": True, "username": session["username"], "csrf_token": csrf})
        if parsed.path == "/api/admin/config":
            if not _admin_required(self): return
            return self._json(database.list_admin_config())
        if parsed.path.startswith("/api/admin/models/") and parsed.path.endswith("/delete-preview"):
            if not _admin_required(self): return
            key=unquote(parsed.path[len("/api/admin/models/"):-len("/delete-preview")])
            try: return self._json(database.get_delete_preview("model", key))
            except ValueError as exc: return self._json({"error":str(exc)},400)
        if parsed.path.startswith("/api/admin/vendors/") and parsed.path.endswith("/delete-preview"):
            if not _admin_required(self): return
            key=unquote(parsed.path[len("/api/admin/vendors/"):-len("/delete-preview")])
            try: return self._json(database.get_delete_preview("vendor", key))
            except ValueError as exc: return self._json({"error":str(exc)},400)
        return self._html("页面不存在", 404)

    def do_POST(self):
        """处理登录、登出、手动探测和厂商测试接口。"""
        # 解析请求路径
        parsed = urlparse(self.path)
        if parsed.path == "/api/probe":
            # 手动探测不要求 JSON 请求体，先完成令牌校验
            if not config.PROBE_TOKEN: return self._json({"error": "probe disabled: PROBE_TOKEN not set"}, 403)
            if self.headers.get("X-Monitor-Token", "") != config.PROBE_TOKEN: return self._json({"error": "invalid token"}, 403)
            try: return self._json({"ok": True, "results": monitor.run_checks()})
            except Exception: logger.exception("手动探测异常"); return self._json({"ok": False, "error": "手动探测失败，请查看服务端日志"}, 500)
        try: payload = _json_body(self)
        except Exception as exc: return self._json({"error": str(exc)}, 400)
        if parsed.path == "/api/admin/login":
            # 校验登录频率并使用统一错误文案
            username = str(payload.get("username", "")).strip(); password = payload.get("password", "")
            if not username or not str(password):
                return self._json({"ok": False, "error": "用户名或密码错误"}, 403)
            key = username or "unknown"; now = datetime.now().timestamp(); failures = _login_failures.get(key, [])
            failures = [item for item in failures if now - item < 900]
            if len(failures) >= 5: return self._json({"ok": False, "error": "用户名或密码错误"}, 403)
            if not verify_admin_password(config.ADMIN_PASSWORD if username == config.ADMIN_USERNAME else "__invalid__", password):
                failures.append(now); _login_failures[key] = failures
                return self._json({"ok": False, "error": "用户名或密码错误"}, 403)
            _login_failures.pop(key, None); token = secrets.token_urlsafe(48); csrf = secrets.token_urlsafe(32); created = config.now_local(); expires = created + timedelta(minutes=config.ADMIN_SESSION_TTL_MINUTES)
            database.create_session(hash_token(token), hash_token(csrf), username, created.isoformat(), created.isoformat(), expires.isoformat())
            body={"ok":True,"csrf_token":csrf}; self._json(body, 200, {"Set-Cookie": f"mm_session={token}; Max-Age={config.ADMIN_SESSION_TTL_MINUTES*60}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if config.ADMIN_COOKIE_SECURE else "")}); return
        if parsed.path == "/api/admin/logout":
            # 登出同样校验当前会话和 CSRF，避免跨站请求强制注销
            session, token = _session(self)
            if session and not _csrf_ok(self, session):
                return self._json({"error": "CSRF 校验失败"}, 403)
            if token: database.delete_session(hash_token(token))
            secure = "; Secure" if config.ADMIN_COOKIE_SECURE else ""
            return self._json({"ok": True}, extra_headers={"Set-Cookie": f"mm_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}"})
        if parsed.path.startswith("/api/admin/"):
            return self._admin_write(parsed.path, payload)
        return self._json({"error": "接口不存在"}, 404)

    def _admin_write(self, path, payload):
        """处理管理配置写操作并刷新运行快照。"""
        # 验证会话和 CSRF
        session = _admin_required(self)
        if not session: return
        if not _csrf_ok(self, session): return self._json({"error": "CSRF 校验失败"}, 403)
        try:
            if path == "/api/admin/settings": database.update_settings(payload, session["username"])
            elif path == "/api/admin/vendors": database.save_vendor(payload, session["username"])
            elif path.startswith("/api/admin/vendors/") and path.endswith("/test"):
                vendor_key = unquote(path.split("/")[-2])
                result = database.test_vendor(vendor_key, payload)
                return self._json(result)
            elif path.startswith("/api/admin/vendors/"): database.save_vendor(payload, session["username"], unquote(path.split("/")[-1]))
            elif path == "/api/admin/models": database.save_model(payload, session["username"])
            elif path.startswith("/api/admin/models/"):
                key=unquote(path.split("/")[-1]);
                if self.command == "DELETE": database.delete_model(key, session["username"])
                else: database.save_model(payload, session["username"], key)
            else: return self._json({"error": "接口不存在"}, 404)
            config_manager.reload(); return self._json({"ok": True, "config": database.list_admin_config()})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except SecretError:
            logger.exception("敏感配置处理失败")
            return self._json({"error": "敏感配置处理失败，请检查服务端配置"}, 400)
        except Exception:
            logger.exception("管理配置写入失败")
            return self._json({"error": "服务端处理失败，请查看服务端日志"}, 500)

    def do_PUT(self):
        """处理管理页的厂商、模型和监控设置更新请求。"""
        # 复用统一管理写入逻辑
        parsed = urlparse(self.path)
        try:
            payload = _json_body(self)
        except Exception as exc:
            return self._json({"error": str(exc)}, 400)
        return self._admin_write(parsed.path, payload)

    def do_DELETE(self):
        """处理删除厂商和模型配置请求。"""
        # 复用管理写请求的认证与删除逻辑
        parsed=urlparse(self.path); session=_admin_required(self)
        if not session: return
        if not _csrf_ok(self, session): return self._json({"error":"CSRF 校验失败"},403)
        try:
            if parsed.path.startswith("/api/admin/vendors/"): database.delete_vendor(unquote(parsed.path.split("/")[-1]), session["username"])
            elif parsed.path.startswith("/api/admin/models/"): database.delete_model(unquote(parsed.path.split("/")[-1]), session["username"])
            else: return self._json({"error":"接口不存在"},404)
            config_manager.reload(); self._json({"ok":True})
        except ValueError as exc: self._json({"error":str(exc)},400)
        except SecretError:
            logger.exception("敏感配置处理失败")
            self._json({"error":"敏感配置处理失败，请检查服务端配置"},400)
        except Exception:
            logger.exception("管理配置删除失败")
            self._json({"error":"服务端处理失败，请查看服务端日志"},500)

    def log_message(self, fmt, *args):
        """屏蔽默认访问日志，避免敏感查询参数进入日志。"""
        pass
