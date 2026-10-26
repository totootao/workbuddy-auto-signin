import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import signin


STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

# 企业（旗舰版）登录态下领取被拒时服务端的真实形态：与"今天已签到"共用 10001
ENTERPRISE_REJECTION = {"code": 10001, "msg": "企业账号不支持该操作"}


def plain_session(endpoint="https://test.invalid", **account):
    base = {"uid": "synthetic-user"}
    base.update(account)
    return {"auth": {"accessToken": "synthetic-token", "endpoint": endpoint},
            "account": base}


class SigninResultTests(unittest.TestCase):
    """issue #17 回归：领取结果的"已签"判定。

    企业态被拒返回 {"code":10001,"msg":"企业账号不支持该操作"}，与"今天已签到"
    同一个 10001。旧代码凭 code 判已签，失败被汇报成 ALREADY、退出码 0——无人
    值守下连签断了都看不出异常。现在只认 msg 里的"已签"语义。
    """

    def setUp(self):
        signin._start_budget()
        self.status = {"active": True, "today_checked_in": False,
                       "streak_days": 3, "total_credits": 900}
        self.claim = 200, {"credit": 100}
        patch.object(signin, "post", side_effect=self.post).start()
        self.addCleanup(patch.stopall)

    def post(self, url, headers, payload=None, **kwargs):
        if url.endswith(STATUS_PATH):
            return 200, self.status
        if url.endswith(CLAIM_PATH):
            return self.claim
        raise AssertionError("unexpected url: %s" % url)

    def run_auto(self):
        return signin.run_auto({}, "https://test.invalid")

    def test_credit_response_is_claimed(self):
        code, out = self.run_auto()
        self.assertEqual((code, out["result"]), (0, "CLAIMED"))
        self.assertEqual(out["credit"], 100)
        self.assertIn("成功领取 100 积分", out["report"])

    def test_explicit_already_checked_in_message_is_quiet_success(self):
        self.claim = 400, {"code": 10001, "msg": "今天已签到，请明天再来"}
        code, out = self.run_auto()
        self.assertEqual((code, out["result"]), (0, "ALREADY"))

    def test_already_checked_in_message_with_http_200_is_success(self):
        # 幂等接口也可能用 200 + 已签文案，不能按 HTTP 状态判失败
        self.claim = 200, {"code": 10001, "msg": "今天已签到，请明天再来"}
        code, out = self.run_auto()
        self.assertEqual((code, out["result"]), (0, "ALREADY"))

    def test_null_claim_body_is_already(self):
        self.claim = 200, None
        code, out = self.run_auto()
        self.assertEqual((code, out["result"]), (0, "ALREADY"))

    def test_enterprise_rejection_with_http_200_is_reported_as_error(self):
        self.claim = 200, dict(ENTERPRISE_REJECTION)
        code, out = self.run_auto()
        self.assertEqual(code, 1)
        self.assertEqual(out["result"], "ERROR")
        self.assertIn("企业账号不支持该操作", out["report"])

    def test_enterprise_rejection_with_http_400_is_reported_as_error(self):
        self.claim = 400, dict(ENTERPRISE_REJECTION)
        code, out = self.run_auto()
        self.assertEqual(code, 1)
        self.assertEqual(out["result"], "ERROR")
        self.assertIn("企业账号不支持该操作", out["report"])

    def test_other_10001_is_reported_as_error(self):
        self.claim = 200, {"code": 10001, "msg": "invalid request"}
        code, out = self.run_auto()
        self.assertEqual((code, out["result"]), (1, "ERROR"))

    def test_run_daily_marks_enterprise_rejection_for_attention(self):
        self.claim = 200, dict(ENTERPRISE_REJECTION)
        with patch.object(signin, "run_growth",
                          return_value=(0, {"result": "IDLE", "report": "成长中心无可领取项",
                                            "idle": True})):
            code, out, quiet = signin.run_daily({}, "https://test.invalid")
        self.assertEqual(code, 1)
        self.assertEqual(out["result"], "ERROR")
        self.assertTrue(out["needs_attention"])
        self.assertFalse(quiet)

    def test_silent_poll_logs_enterprise_rejection(self):
        # 轮询模式下错误必须落盘：quiet 只属于 ALREADY/INACTIVE 空跑
        self.claim = 200, dict(ENTERPRISE_REJECTION)
        with tempfile.TemporaryDirectory() as temp:
            auth = Path(temp) / "auth.json"
            log = Path(temp) / "signin.log"
            auth.write_text(json.dumps(plain_session()), encoding="utf-8")
            with patch.dict(os.environ, {"WORKBUDDY_AUTH_FILE": str(auth),
                                         "WORKBUDDY_SIGNIN_LOG": str(log)}), \
                    patch.object(signin, "run_growth",
                                 return_value=(0, {"result": "IDLE",
                                                   "report": "成长中心无可领取项",
                                                   "idle": True})):
                self.assertEqual(signin._run("silent-poll"), 1)
            entries = [json.loads(line.split("] ", 1)[1])
                       for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["result"], "ERROR")
        self.assertTrue(entries[0]["needs_attention"])
        self.assertEqual(entries[0]["trigger"], "poll")
        self.assertIn("企业账号不支持该操作", entries[0]["report"])


class DebugCommandTests(unittest.TestCase):
    """status/claim 调试命令：HTTP 200 + 业务错误不能再计成成功。"""

    def setUp(self):
        signin._start_budget()
        self.status = {"active": True, "today_checked_in": True}
        self.claim = 200, {"credit": 100}
        patch.object(signin, "post", side_effect=self.post).start()
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth = Path(self.temp.name) / "auth.json"
        self.auth.write_text(json.dumps(plain_session()), encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            "WORKBUDDY_AUTH_FILE": str(self.auth),
            "WORKBUDDY_SIGNIN_LOG": str(Path(self.temp.name) / "signin.log"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def post(self, url, headers, payload=None, **kwargs):
        if url.endswith(STATUS_PATH):
            return 200, self.status
        if url.endswith(CLAIM_PATH):
            return self.claim
        raise AssertionError("unexpected url: %s" % url)

    def run_action(self, action):
        with patch.object(signin, "emit") as emit:
            code = signin._run(action)
        return code, emit.call_args.args[0]

    def test_status_with_business_error_needs_attention(self):
        self.status = dict(ENTERPRISE_REJECTION)
        code, out = self.run_action("status")
        self.assertEqual(code, 1)
        self.assertTrue(out["needs_attention"])
        self.assertEqual(out["step"], "status")

    def test_status_with_valid_body_is_quiet(self):
        code, out = self.run_action("status")
        self.assertEqual(code, 0)
        self.assertFalse(out["needs_attention"])

    def test_claim_with_business_error_needs_attention(self):
        self.claim = 200, dict(ENTERPRISE_REJECTION)
        code, out = self.run_action("claim")
        self.assertEqual(code, 1)
        self.assertTrue(out["needs_attention"])
        self.assertEqual(out["step"], "claim")

    def test_claim_with_credit_is_quiet(self):
        code, out = self.run_action("claim")
        self.assertEqual(code, 0)
        self.assertFalse(out["needs_attention"])

    def test_claim_already_signed_via_400_is_quiet(self):
        self.claim = 400, {"code": 10001, "msg": "今天已签到，请明天再来"}
        code, out = self.run_action("claim")
        self.assertEqual(code, 0)
        self.assertFalse(out["needs_attention"])

    def test_claim_other_business_error_via_400_needs_attention(self):
        self.claim = 400, {"code": 10001, "msg": "invalid request"}
        code, out = self.run_action("claim")
        self.assertEqual(code, 1)
        self.assertTrue(out["needs_attention"])


if __name__ == "__main__":
    unittest.main()
