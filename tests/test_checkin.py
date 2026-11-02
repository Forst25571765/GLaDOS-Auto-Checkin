import io
import json
import os
from pathlib import Path
import runpy
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, call, patch

import requests

import checkin


COOKIE = "gld:sess=session-value-one; gld:sess.sig=signature-value-one"
COOKIE_TWO = "koa:sess=session-value-two; koa:sess.sig=signature-value-two"
SENDKEY = "SCT-test-key-not-real"


def response(data=None, status=200, body=None):
    result = requests.Response()
    result.status_code = status
    result.url = checkin.CHECKIN_URL
    result.encoding = "utf-8"
    result._content = (json.dumps(data) if body is None else body).encode("utf-8")
    return result


def success():
    return response({"code": 0, "points": 12, "message": "Checkin! Got 12 Points"})


def details():
    return response({"data": {"email": "example@example.com", "leftDays": "70.9"}})


def auth_failure():
    return response({"code": -2, "message": "没有权限"})


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.env = {"COOKIES": COOKIE, "SCT_SENDKEY": SENDKEY}
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.cookies = requests.cookies.RequestsCookieJar()
        session_patch = patch.object(checkin.requests, "Session", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        # Every HTTP response is supplied by a test; no credentials or network are used.
        self.session.request.side_effect = AssertionError("Unexpected HTTP request")
        post_patch = patch.object(checkin.requests, "post", side_effect=AssertionError("Unexpected Telegram request"))
        self.telegram = post_patch.start()
        self.addCleanup(post_patch.stop)
        send_patch = patch.object(checkin, "sc_send", return_value={"code": 0, "data": {"readkey": "private-read-key"}})
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)
        sleep_patch = patch.object(checkin.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def supply(self, *items):
        self.session.request.side_effect = list(items)

    def test_cookie_prefixes_and_paste_wrappers(self):
        for prefix in ("gld", "koa", "future.prefix"):
            with self.subTest(prefix=prefix):
                cookie = f"{prefix}:sess=abc123; {prefix}:sess.sig=xyz789"
                for pasted in (cookie, f'"Cookie: {cookie}"', f'Cookie: "{cookie}"'):
                    cleaned = checkin.parse_cookies(pasted)
                    self.assertEqual(cleaned, [cookie])
                    self.assertEqual(checkin.session_prefix(cleaned[0]), prefix)

    def test_multiple_cookie_separators(self):
        for separator in (" & ", "\n", "\r\n", " ||| "):
            with self.subTest(separator=separator):
                self.assertEqual(checkin.parse_cookies(COOKIE + separator + COOKIE_TWO), [COOKIE, COOKIE_TWO])

    def test_paste_wrappers_are_removed_per_account(self):
        pasted = f'"Cookie: {COOKIE}" ||| Cookie: "{COOKIE_TWO}"'
        self.assertEqual(checkin.parse_cookies(pasted), [COOKIE, COOKIE_TWO])

    def test_incomplete_mismatched_and_empty_sessions_fail_without_http(self):
        for cookie in ("gld:sess=secret", "gld:sess=a; koa:sess.sig=b",
                       "gld:sess=; gld:sess.sig=b", "irrelevant=value"):
            with self.subTest(cookie=cookie), patch.dict(os.environ, {"COOKIES": cookie}):
                self.assertEqual(checkin.main(), 1)
        self.session.request.assert_not_called()
        self.assertNotIn("irrelevant=value", self.output.getvalue())

    def test_missing_cookie_fails_and_keeps_sct_notification(self):
        with patch.dict(os.environ, {"COOKIES": '""'}):
            self.assertEqual(checkin.main(), 1)
        self.session.request.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], SENDKEY)
        self.assertIn("未检测到 COOKIES", self.send.call_args.args[2])

    def test_success_notifies_existing_sendkey_and_returns_zero(self):
        self.supply(success(), details())
        self.assertEqual(checkin.main(), 0)
        self.send.assert_called_once_with(SENDKEY, "GLaDOS 签到完成 ✅1 ❌0 🔁0",
            "1. ex***@example.com | ✅ 成功 | P:12 | 剩余:70 天", {"tags": "GLaDOS|签到"})
        self.assertNotIn("private-read-key", self.output.getvalue())
        self.assertNotIn("example@example.com", self.output.getvalue())

    def test_observation_logged_is_already_checked_in(self):
        self.supply(response({"code": 1, "points": 0,
            "message": "Today's observation logged. Return tomorrow for more points."}), details())
        self.assertEqual(checkin.main(), 0)
        self.assertIn("✅0 ❌0 🔁1", self.send.call_args.args[1])

    def test_business_failure_returns_one_even_when_notification_succeeds(self):
        self.supply(auth_failure())
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 1)
        self.send.assert_called_once()
        self.assertIn("更新 COOKIES", self.send.call_args.args[2])

    def test_automated_checkin_rejection_is_a_failure(self):
        self.supply(response({"code": 4, "message": "Automated check-in detected. Please sign in again to continue."}))
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 1)

    def test_failure_code_cannot_be_overridden_by_got_in_message(self):
        for code in (-2, 4, None, "invalid", True):
            with self.subTest(code=code):
                self.supply(response({"code": code, "message": "got 12 points"}))
                self.assertEqual(checkin.main(), 1)

    def test_numeric_string_response_codes(self):
        for code, result in (("0", "ok"), ("1", "repeat"), ("-2", "fail")):
            with self.subTest(code=code):
                self.assertEqual(checkin.classify_checkin(code), result)

    def test_http_authentication_rejection_is_not_retried(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.supply(response({}, status=status))
                self.assertEqual(checkin.main(), 1)
                self.assertEqual(self.session.request.call_count, 1)
                self.assertIn("更新 COOKIES", self.send.call_args.args[2])

    def test_temporary_http_failure_retries_then_succeeds(self):
        self.supply(response({}, status=503), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 3)
        self.sleep.assert_called_once_with(15)

    def test_rate_limit_retries_then_succeeds(self):
        self.supply(response({}, status=429), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 3)

    def test_exhausted_network_errors_fail_without_leaking_cookie(self):
        self.supply(*[requests.Timeout(COOKIE) for _ in range(checkin.MAX_ATTEMPTS)])
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, checkin.MAX_ATTEMPTS)
        self.assertNotIn("session-value-one", self.output.getvalue())
        self.assertNotIn("signature-value-one", self.output.getvalue())

    def test_checkin_can_wait_for_a_response_slower_than_fifteen_seconds(self):
        def handle(method, url, **kwargs):
            if method == "POST":
                if kwargs["timeout"][1] < 25:
                    raise requests.ReadTimeout("Response would arrive after 25 seconds")
                return success()
            # Optional details keep their shorter timeout.
            self.assertEqual(kwargs["timeout"], (5, 15))
            return details()

        self.session.request.side_effect = handle
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 2)
        self.sleep.assert_not_called()

    def test_read_timeout_then_already_checked_in_is_successful(self):
        # The first POST may have reached the service even if its response was lost.
        self.supply(requests.ReadTimeout(COOKIE), response({"code": 1,
            "message": "Today's observation logged. Return tomorrow for more points."}), details())
        self.assertEqual(checkin.main(), 0)
        self.assertIn("✅0 ❌0 🔁1", self.send.call_args.args[1])
        self.sleep.assert_called_once_with(15)
        self.assertNotIn("session-value-one", self.output.getvalue())

    def test_three_read_timeouts_fail_with_bounded_backoff_and_network_notice(self):
        self.supply(*[requests.ReadTimeout(COOKIE) for _ in range(3)])
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(15), call(30)])
        for request in self.session.request.call_args_list:
            self.assertEqual(request.kwargs["timeout"], (10, 60))
        self.send.assert_called_once()
        self.assertIn("网络超时", self.send.call_args.args[2])
        self.assertNotIn("更新 COOKIES", self.send.call_args.args[2])
        self.assertNotIn("session-value-one", self.output.getvalue())
        self.assertNotIn("signature-value-one", self.output.getvalue())

    def test_optional_details_timeout_does_not_turn_checkin_red(self):
        self.supply(success(), *[requests.ReadTimeout(COOKIE) for _ in range(3)])
        self.assertEqual(checkin.main(), 0)
        self.assertIn("✅1 ❌0 🔁0", self.send.call_args.args[1])
        for request in self.session.request.call_args_list[1:]:
            self.assertEqual(request.kwargs["timeout"], (5, 15))

    def test_non_json_and_wrong_json_type_fail(self):
        for item in (response(body="<html>gateway error</html>"), response([])):
            with self.subTest(body=item.text):
                self.session.request.side_effect = None
                self.session.request.return_value = item
                self.assertEqual(checkin.main(), 1)

    def test_status_query_failures_do_not_overwrite_success(self):
        for item in (response({}, status=403), response({"data": {"leftDays": "invalid"}})):
            with self.subTest(response=item.text):
                self.supply(success(), item)
                self.assertEqual(checkin.main(), 0)
                self.assertIn("✅1 ❌0 🔁0", self.send.call_args.args[1])

    def test_partial_failure_is_red_and_remaining_accounts_are_processed(self):
        with patch.dict(os.environ, {"COOKIES": COOKIE + "\n" + COOKIE_TWO + "\n" + COOKIE}):
            self.supply(success(), details(), auth_failure(),
                response({"code": 1, "message": "Today's observation logged."}), details())
            self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 5)
        self.assertIn("✅1 ❌1 🔁1", self.send.call_args.args[1])

    def test_each_account_has_its_own_cookie_header_and_clears_session(self):
        seen_headers = []

        def handle(method, url, **kwargs):
            seen_headers.append(kwargs["headers"]["cookie"])
            if method == "POST":
                self.assertEqual(len(self.session.cookies), 0)
                self.assertEqual(kwargs["json"], {"token": "glados.cloud"})
                self.session.cookies.set("server-set", "previous-account")
                return success()
            return details()

        self.session.request.side_effect = handle
        with patch.dict(os.environ, {"COOKIES": COOKIE + " & " + COOKIE_TWO}):
            self.assertEqual(checkin.main(), 0)
        self.assertEqual(seen_headers, [COOKIE, COOKIE, COOKIE_TWO, COOKIE_TWO])

    def test_notification_error_does_not_change_business_result_or_log_sendkey(self):
        self.send.side_effect = RuntimeError(SENDKEY)
        self.supply(success(), details())
        self.assertEqual(checkin.main(), 0)
        self.supply(auth_failure())
        self.assertEqual(checkin.main(), 1)
        self.assertNotIn(SENDKEY, self.output.getvalue())

    def test_serverchan_api_error_is_not_reported_as_notification_success(self):
        self.send.return_value = {"code": 40001}
        self.supply(success(), details())
        self.assertEqual(checkin.main(), 0)
        self.assertIn("Server酱推送失败", self.output.getvalue())
        self.assertNotIn("微信推送完成", self.output.getvalue())

    def test_telegram_and_sct_can_both_receive_the_summary(self):
        self.telegram.side_effect = None
        self.telegram.return_value = response({"ok": True})
        self.supply(success(), details())
        with patch.dict(os.environ, {"TG_BOT_TOKEN": "test-bot", "TG_CHAT_ID": "123"}):
            self.assertEqual(checkin.main(), 0)
        self.telegram.assert_called_once()
        self.send.assert_called_once()

    def test_response_message_does_not_log_session_credentials(self):
        self.supply(response({"code": 4, "message": "rejected session-value-one signature-value-one"}))
        self.assertEqual(checkin.main(), 1)
        self.assertNotIn("session-value-one", self.output.getvalue())
        self.assertNotIn("signature-value-one", self.output.getvalue())

    def test_script_entrypoint_exits_with_business_failure_code(self):
        self.supply(auth_failure())
        with patch("serverchan_sdk.sc_send", self.send):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(checkin.__file__)), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)
        self.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
