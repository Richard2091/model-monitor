# -*- coding: utf-8 -*-
"""邮件告警模块测试：使用临时 SQLite 并 mock 掉 smtplib。"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# 使用临时空配置导入项目模块，避免读取真实环境变量或 /app/.env。
_CONFIG_ENV_NAMES = (
    "ENV_FILE", "DB_PATH", "ALERT_EMAIL_TO", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
    "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_NAME", "SMTP_USE_SSL", "SMTP_TIMEOUT",
    "ALERT_DAILY_LIMIT", "ALERT_PAGE_URL",
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
    import alerts
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


def _res(model, ok, code=200, msg=""):
    """构造一条探测结果。"""
    return {"model": model, "ok": ok, "latency_ms": 123, "status_code": code, "message": msg}


class AlertsTest(unittest.TestCase):
    """状态变化告警的判定、发送、重试与限流测试。"""

    def setUp(self):
        """准备临时数据库与告警配置，并 mock smtplib。"""
        # 临时目录与数据库
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        overrides = {
            "DB_PATH": os.path.join(self._tmp.name, "t.db"),
            "ALERT_EMAIL_TO": "a@example.com, b@example.com",
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": 465,
            "SMTP_USERNAME": "user@example.com", "SMTP_PASSWORD": "x",
            "SMTP_FROM": "user@example.com", "SMTP_FROM_NAME": "模型监控",
            "SMTP_USE_SSL": True, "SMTP_TIMEOUT": 5,
            "ALERT_DAILY_LIMIT": 30, "ALERT_PAGE_URL": "",
        }
        for name, value in overrides.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        # mock SSL 与普通 SMTP，记录发信次数
        self.server = MagicMock()
        p1 = patch("smtplib.SMTP_SSL", return_value=self.server)
        p2 = patch("smtplib.SMTP", return_value=self.server)
        self.ssl_mock = p1.start()
        self.smtp_mock = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.targets = (SimpleNamespace(model_key="m1", display_name="模型一"),
                        SimpleNamespace(model_key="m2", display_name=None))

    def _sent(self):
        """返回已发送邮件数。"""
        return self.server.send_message.call_count

    def test_normal_to_red_sends(self):
        """正常→红发送故障邮件，正文含显示名、状态码与页面链接。"""
        with patch.object(config, "ALERT_PAGE_URL", "http://mon.example/"):
            alerts.process_round([_res("m1", True)], self.targets)
            self.assertEqual(self._sent(), 0)
            alerts.process_round([_res("m1", False, 500, "boom" * 100)], self.targets)
        self.assertEqual(self._sent(), 1)
        msg = self.server.send_message.call_args[0][0]
        self.assertEqual(str(msg["Subject"]), "【模型监控】模型一（m1） 异常：HTTP 500")
        body = msg.get_content()
        self.assertIn("模型一（m1）", body)
        self.assertIn("500", body)
        self.assertIn("http://mon.example/", body)
        self.assertNotIn("boom" * 60, body)

    def test_red_to_red_no_send(self):
        """红→红不重复发信。"""
        alerts.process_round([_res("m1", False)], self.targets)
        alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self._sent(), 1)

    def test_red_to_normal_sends_recovery(self):
        """红→正常发恢复邮件，标题含模型名，且无显示名时只写模型ID。"""
        alerts.process_round([_res("m2", False, 502)], self.targets)
        self.assertEqual(self._sent(), 1)
        alerts.process_round([_res("m2", True)], self.targets)
        self.assertEqual(self._sent(), 2)
        msg = self.server.send_message.call_args[0][0]
        self.assertEqual(str(msg["Subject"]), "【模型监控】m2 已恢复")
        self.assertIn("恢复", msg.get_content())

    def test_per_model_emails(self):
        """同一轮两个模型变化发两封，各自标题含各自模型名。"""
        alerts.process_round([_res("m1", False, 500), _res("m2", False, 503)], self.targets)
        self.assertEqual(self._sent(), 2)
        subjects = [str(c[0][0]["Subject"]) for c in self.server.send_message.call_args_list]
        self.assertEqual(subjects, ["【模型监控】模型一（m1） 异常：HTTP 500", "【模型监控】m2 异常：HTTP 503"])

    def test_status_zero_subject(self):
        """状态码为 0 或空时标题写“请求失败”。"""
        alerts.process_round([_res("m1", False, 0, "timeout"), _res("m2", False, None)], self.targets)
        subjects = [str(c[0][0]["Subject"]) for c in self.server.send_message.call_args_list]
        self.assertEqual(subjects, ["【模型监控】模型一（m1） 异常：请求失败", "【模型监控】m2 异常：请求失败"])

    def test_recovery_has_duration(self):
        """恢复邮件含“故障持续”，算不出时间时不写该行。"""
        past = (config.now_local() - timedelta(minutes=10)).isoformat()
        database.save_alert_states([("m1", False, past), ("m2", False, "坏时间")])
        alerts.process_round([_res("m1", True), _res("m2", True)], self.targets)
        bodies = [c[0][0].get_content() for c in self.server.send_message.call_args_list]
        self.assertEqual(len(bodies), 2)
        self.assertIn("故障持续：10 分钟", bodies[0])
        self.assertNotIn("故障持续", bodies[1])

    def test_partial_failure_retries_only_failed(self):
        """同一轮一封成功一封失败，只有失败的模型下一轮重发。"""
        def fake_send(msg, **kwargs):
            if "m2" in str(msg["Subject"]):
                raise OSError("拒收")
        self.server.send_message.side_effect = fake_send
        alerts.process_round([_res("m1", False), _res("m2", False)], self.targets)
        self.assertEqual(database.get_alert_states(), {"m1": False})
        self.server.send_message.side_effect = None
        self.server.send_message.reset_mock()
        alerts.process_round([_res("m1", False), _res("m2", False)], self.targets)
        self.assertEqual(self._sent(), 1)
        self.assertIn("m2", str(self.server.send_message.call_args[0][0]["Subject"]))
        self.assertEqual(database.get_alert_states(), {"m1": False, "m2": False})

    def test_first_seen(self):
        """首次红发信，首次正常只记录状态不发信。"""
        alerts.process_round([_res("m1", True)], self.targets)
        self.assertEqual(self._sent(), 0)
        self.assertEqual(database.get_alert_states(), {"m1": True})
        alerts.process_round([_res("m2", False)], self.targets)
        self.assertEqual(self._sent(), 1)

    def test_new_process_no_duplicate(self):
        """重新读库（模拟新进程）后红→红不重复发信。"""
        alerts.process_round([_res("m1", False)], self.targets)
        importlib.reload(alerts)
        alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self._sent(), 1)
        self.assertEqual(database.get_alert_states(), {"m1": False})

    def test_send_failure_retries(self):
        """发信失败不写状态并记失败，下一轮自动重试。"""
        self.server.send_message.side_effect = OSError("连接失败")
        alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(database.get_alert_states(), {})
        self.assertEqual(database.count_alert_emails(config.now_local().strftime("%Y-%m-%d")), 1)
        self.server.send_message.side_effect = None
        alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self.server.send_message.call_count, 2)
        self.assertEqual(database.get_alert_states(), {"m1": False})

    def test_daily_limit(self):
        """达到每日上限不再发信，但状态照样更新。"""
        with patch.object(config, "ALERT_DAILY_LIMIT", 1):
            alerts.process_round([_res("m1", False)], self.targets)
            alerts.process_round([_res("m1", True)], self.targets)
        self.assertEqual(self._sent(), 1)
        self.assertEqual(database.get_alert_states(), {"m1": True})

    def test_disabled_updates_state_only(self):
        """未配置收件人时不发信，但更新状态。"""
        with patch.object(config, "ALERT_EMAIL_TO", ""):
            alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self._sent(), 0)
        self.assertEqual(database.get_alert_states(), {"m1": False})
        # 之后开启告警，旧故障不应被当作新故障
        alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self._sent(), 0)

    def test_starttls_branch(self):
        """SMTP_USE_SSL=false 时使用 SMTP + starttls。"""
        with patch.object(config, "SMTP_USE_SSL", False):
            alerts.process_round([_res("m1", False)], self.targets)
        self.assertEqual(self.ssl_mock.call_count, 0)
        self.server.starttls.assert_called_once()

    def test_internal_exception_swallowed(self):
        """process_round 内部异常不向外抛出。"""
        with patch.object(database, "get_alert_states", side_effect=RuntimeError("坏了")):
            with self.assertLogs("alerts", level="ERROR"):
                alerts.process_round([_res("m1", False)], self.targets)


if __name__ == "__main__":
    unittest.main()
