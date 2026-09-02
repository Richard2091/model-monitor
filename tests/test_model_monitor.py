# -*- coding: utf-8 -*-
"""模型监控项目最小回归测试。"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from email.message import Message
from unittest.mock import patch


# 使用临时空配置导入项目模块，避免读取真实环境变量或 /app/.env。
_CONFIG_ENV_NAMES = (
    "ENV_FILE",
    "SUB2API_URL",
    "SUB2API_KEY",
    "DB_PATH",
    "PORT",
    "HOST",
    "PROBE_TOKEN",
    "PROBE_INTERVAL_MINUTES",
    "SLOW_THRESHOLD_MS",
    "MONITOR_WINDOWS",
    "MODELS",
)
_ORIGINAL_CONFIG_ENV = {name: os.environ.get(name) for name in _CONFIG_ENV_NAMES}
_TEMP_ENV_FD, _TEMP_ENV_PATH = tempfile.mkstemp(prefix="model-monitor-test-", suffix=".env")
os.close(_TEMP_ENV_FD)
try:
    for _name in _CONFIG_ENV_NAMES:
        os.environ.pop(_name, None)
    os.environ["ENV_FILE"] = _TEMP_ENV_PATH
    if "config" in sys.modules:
        config = importlib.reload(sys.modules["config"])
    else:
        import config
    import database
    import http_server
    import monitor
finally:
    for _name, _value in _ORIGINAL_CONFIG_ENV.items():
        if _value is None:
            os.environ.pop(_name, None)
        else:
            os.environ[_name] = _value
    try:
        os.unlink(_TEMP_ENV_PATH)
    except OSError:
        pass


class ModelMonitorRegressionTest(unittest.TestCase):
    """模型监控核心配置、存储、渲染与鉴权回归测试。"""

    def _create_handler(self, token=""):
        """创建不依赖真实网络监听的 HTTP 处理器对象。"""
        # 构造处理器实例并设置手动探测请求路径
        handler = object.__new__(http_server.Handler)
        handler.path = "/api/probe"
        # 构造可供 do_POST 读取的请求头对象
        handler.headers = Message()
        if token:
            handler.headers["X-Monitor-Token"] = token
        # 收集 JSON 响应，避免触发真实套接字写入
        handler.responses = []
        handler._json = lambda obj, code=200: handler.responses.append((obj, code))
        return handler

    def tearDown(self):
        """清理测试期间可能被修改的模块级配置。"""
        # 恢复数据库测试可能留下的临时路径，避免影响后续测试
        if hasattr(self, "_old_db_path"):
            database.config.DB_PATH = self._old_db_path
        # 恢复数据库相关槽位配置，确保测试之间相互隔离
        if hasattr(self, "_old_database_config"):
            for name, value in self._old_database_config.items():
                setattr(database.config, name, value)

    def _snapshot_database_config(self):
        """保存数据库槽位测试所需的全局配置。"""
        # 记录测试即将覆盖的配置项
        names = (
            "DB_PATH",
            "MODELS",
            "TIME_START",
            "TIME_END",
            "PROBE_INTERVAL_MIN",
            "TOTAL_SLOTS",
            "MONITOR_WINDOWS",
            "MONITOR_WINDOWS_STR",
            "SLOW_THRESHOLD_MS",
        )
        self._old_database_config = {name: getattr(database.config, name) for name in names}
        self._old_db_path = database.config.DB_PATH

    def test_default_models_exclude_disabled_vendors(self):
        """校验内置模型清单已移除停用的 DeepSeek 与 GLM 模型。"""
        # 检查默认清单保留可用的 GPT 与 Qwen 模型
        self.assertEqual(list(config.DEFAULT_MODELS), [
            "gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra",
            "qwen3.7-flash", "qwen3.7-plus",
        ])
        # 检查停用厂商模型不会进入默认监控列表
        self.assertNotIn("deepseek-v4-flash", config.MODELS)
        self.assertNotIn("deepseek-v4-flash-vision-exp", config.MODELS)
        self.assertNotIn("glm-5.3-flash", config.MODELS)

    def test_parse_models_trims_empty_items_and_deduplicates(self):
        """校验模型配置可清理空项、空白并保持首次出现顺序。"""
        # 模拟来自 .env 或环境变量的多厂商模型配置
        raw_models = " qwen3.7-plus, ,gpt-5.5,qwen3.7-plus, gpt-5.4 "
        # 解析配置并检查规范化结果
        self.assertEqual(config._parse_models(raw_models), [
            "qwen3.7-plus", "gpt-5.5", "gpt-5.4",
        ])

    def test_parse_time_accepts_valid_values_and_rejects_invalid_ranges(self):
        """校验时间解析支持合法边界，并拒绝非法小时和分钟。"""
        # 解析当天最小时间与最大时间
        self.assertEqual(config._parse_time("00:00"), 0)
        self.assertEqual(config._parse_time("23:59"), 1439)
        # 校验非法小时与非法分钟会明确抛出异常
        with self.assertRaises(ValueError):
            config._parse_time("24:00")
        with self.assertRaises(ValueError):
            config._parse_time("12:60")

    def test_parse_windows_accepts_normal_windows_and_rejects_cross_midnight(self):
        """校验普通监控窗口解析成功，并拒绝跨午夜窗口。"""
        # 解析两个普通监控时段
        windows = config._parse_windows("08:00-12:00, 13:30-18:00")
        # 校验解析结果保持分钟数和输入顺序
        self.assertEqual(windows, [(480, 720), (810, 1080)])
        # 校验跨午夜配置被明确拒绝
        with self.assertRaises(ValueError):
            config._parse_windows("22:00-02:00")

    def test_run_checks_returns_empty_without_starting_executor_when_models_empty(self):
        """校验模型列表为空时直接返回空结果且不创建线程池。"""
        # 临时清空模型列表，模拟无待探测模型配置
        with patch.object(monitor.config, "MODELS", []):
            # 替换线程池构造器，用于验证短路逻辑
            with patch.object(monitor, "ThreadPoolExecutor") as executor:
                result = monitor.run_checks()
        # 校验空模型时返回空列表
        self.assertEqual(result, [])
        # 校验线程池未被创建
        executor.assert_not_called()

    def test_build_query_merges_records_into_expected_slots(self):
        """使用临时 SQLite 数据库校验探测记录按时间槽位归并。"""
        # 保存并覆盖数据库查询所需的模块级配置
        self._snapshot_database_config()
        # 使用关闭后再手工清理的临时文件，避免 Windows 清理目录时锁定 SQLite 文件
        temp_file = tempfile.NamedTemporaryFile(prefix="model-monitor-db-", suffix=".db", delete=False)
        db_path = temp_file.name
        temp_file.close()
        try:
            database.config.DB_PATH = db_path
            database.config.MODELS = ["model-a"]
            database.config.TIME_START = 8 * 60
            database.config.TIME_END = 8 * 60 + 10
            database.config.PROBE_INTERVAL_MIN = 5
            database.config.TOTAL_SLOTS = 3
            database.config.MONITOR_WINDOWS = [(8 * 60, 8 * 60 + 10)]
            database.config.MONITOR_WINDOWS_STR = "08:00-08:10"
            database.config.SLOW_THRESHOLD_MS = 1000
            # 初始化临时数据库表结构
            database.init_db()
            # 写入两个不同槽位及同槽位后写入记录
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                conn.executemany(
                    "INSERT INTO probe_records(model, probed_at, ok, latency_ms, status_code, message) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("model-a", "2026-09-01T08:02:00", 1, 100, 200, "first slot"),
                        ("model-a", "2026-09-01T08:04:00", 0, 200, 500, "second slot old"),
                        ("model-a", "2026-09-01T08:07:00", 1, 300, 200, "second slot latest"),
                    ],
                )
                conn.commit()
            finally:
                # 显式关闭写入连接，避免 Windows 删除临时数据库时仍被文件句柄占用
                conn.close()
            # 查询指定日期并读取模型槽位数据
            result = database.build_query("2026-09-01")
        finally:
            # 执行 WAL 检查点并关闭连接，确保数据库及旁车文件可在 Windows 删除
            checkpoint_conn = sqlite3.connect(db_path, timeout=10)
            try:
                checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                checkpoint_conn.close()
            # 手工清理数据库、WAL 和共享内存文件，不依赖 TemporaryDirectory 即时删除
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except FileNotFoundError:
                    pass
        # 校验返回的基础查询信息
        self.assertEqual(result["date"], "2026-09-01")
        self.assertEqual(result["interval"], 5)
        self.assertEqual(len(result["models"]), 1)
        slots = result["models"][0]["slots"]
        # 校验首槽位记录
        self.assertEqual(slots[0]["time"], "08:00")
        self.assertTrue(slots[0]["ok"])
        self.assertEqual(slots[0]["latency_ms"], 100)
        # 校验同一槽位后写入记录覆盖先前记录
        self.assertEqual(slots[1]["time"], "08:05")
        self.assertTrue(slots[1]["ok"])
        self.assertEqual(slots[1]["latency_ms"], 300)
        self.assertEqual(slots[1]["message"], "second slot latest")
        # 校验未写入槽位保持空值
        self.assertIsNone(slots[2]["ok"])

    def test_render_index_removes_models_placeholder_and_keeps_theme_date_replacements(self):
        """校验首页渲染移除旧模型占位符并保留主题、日期替换。"""
        # 固定当前时间，避免测试依赖真实系统日期
        fixed_now = datetime(2026, 9, 1, 10, 30)
        with patch.object(http_server.config, "now_local", return_value=fixed_now):
            # 渲染深色主题首页
            page = http_server.render_index("dark")
        # 校验渲染结果存在且旧占位符已被移除
        self.assertIsNotNone(page)
        self.assertNotIn("__MODELS__", page)
        self.assertNotIn("__THEME_CLASS__", page)
        self.assertNotIn("__TODAY__", page)
        # 校验主题和固定日期仍被正确注入
        self.assertIn('class="dark"', page)
        self.assertIn("new Date(2026,8,1)", page)

    def test_manual_probe_error_response_does_not_expose_exception_details(self):
        """校验手动探测失败时只返回通用文案，不泄露异常原文。"""
        # 构造已通过鉴权的手动探测请求
        handler = self._create_handler("expected-token")
        with patch.object(http_server.config, "PROBE_TOKEN", "expected-token"):
            # 模拟包含敏感内部信息的探测异常
            with patch.object(http_server.monitor, "run_checks", side_effect=RuntimeError("secret-internal-host:8080")):
                handler.do_POST()
        # 校验响应为服务端错误且不包含异常原文
        self.assertEqual(handler.responses[0][1], 500)
        self.assertEqual(handler.responses[0][0]["error"], "手动探测失败，请查看服务端日志")
        self.assertNotIn("secret-internal-host", str(handler.responses[0][0]))

    def test_next_probe_time_aligns_to_the_next_monitoring_slot(self):
        """校验后台调度会跳过已过期槽位并对齐到下一个合法槽位。"""
        # 保存并覆盖调度计算所需的槽位配置
        names = ("TIME_START", "TIME_END", "PROBE_INTERVAL_MIN", "PROBE_INTERVAL", "TOTAL_SLOTS", "MONITOR_WINDOWS")
        old_values = {name: getattr(monitor.config, name) for name in names}
        try:
            monitor.config.TIME_START = 8 * 60
            monitor.config.TIME_END = 8 * 60 + 10
            monitor.config.PROBE_INTERVAL_MIN = 5
            monitor.config.PROBE_INTERVAL = 300
            monitor.config.TOTAL_SLOTS = 3
            monitor.config.MONITOR_WINDOWS = [(8 * 60, 8 * 60 + 10)]
            # 当前时间落在 08:05 槽位之后，下一次应为 08:10
            now = datetime(2026, 9, 1, 8, 6)
            next_time = monitor._next_probe_time(now)
            # 校验计算结果精确对齐到下一合法槽位
            self.assertEqual(datetime.fromtimestamp(next_time).replace(second=0, microsecond=0), datetime(2026, 9, 1, 8, 10))
        finally:
            # 恢复全局调度配置，避免影响其他测试
            for name, value in old_values.items():
                setattr(monitor.config, name, value)

    def test_probe_authentication_rejects_disabled_or_invalid_token_and_accepts_valid_token(self):
        """校验手动探测在未配置、令牌错误和令牌正确时的鉴权行为。"""
        # 未配置令牌时拒绝手动探测
        disabled_handler = self._create_handler()
        with patch.object(http_server.config, "PROBE_TOKEN", ""):
            disabled_handler.do_POST()
        self.assertEqual(disabled_handler.responses[0][1], 403)
        self.assertIn("probe disabled", disabled_handler.responses[0][0]["error"])
        # 配置令牌后拒绝错误令牌
        invalid_handler = self._create_handler("wrong-token")
        with patch.object(http_server.config, "PROBE_TOKEN", "expected-token"):
            invalid_handler.do_POST()
        self.assertEqual(invalid_handler.responses[0][1], 403)
        self.assertEqual(invalid_handler.responses[0][0]["error"], "invalid token")
        # 配置正确令牌后允许调用探测逻辑
        valid_handler = self._create_handler("expected-token")
        with patch.object(http_server.config, "PROBE_TOKEN", "expected-token"):
            with patch.object(http_server.monitor, "run_checks", return_value=[{"model": "model-a"}]) as run_checks:
                valid_handler.do_POST()
        # 校验探测函数被调用并返回成功响应
        run_checks.assert_called_once_with()
        self.assertEqual(valid_handler.responses, [({"ok": True, "results": [{"model": "model-a"}]}, 200)])


if __name__ == "__main__":
    unittest.main()
