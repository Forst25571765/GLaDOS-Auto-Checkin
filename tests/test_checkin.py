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


def challenge(nonce="offline-fixture-nonce", ttl=300_000):
    return response({"code": 0, "nonce": nonce, "ttl": ttl})


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
        self.supply(challenge(), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.send.assert_called_once_with(SENDKEY, "GLaDOS 签到完成 ✅1 ❌0 🔁0",
            "1. ex***@example.com | ✅ 成功 | P:12 | 剩余:70 天", {"tags": "GLaDOS|签到"})
        self.assertNotIn("private-read-key", self.output.getvalue())
        self.assertNotIn("example@example.com", self.output.getvalue())

    def test_observation_logged_is_already_checked_in(self):
        self.supply(challenge(), response({"code": 1, "points": 0,
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
        self.supply(challenge(), response({"code": 4, "message": "Automated check-in detected. Please sign in again to continue."}))
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertIn("请在官网完成签到", self.send.call_args.args[2])

    def test_failure_code_cannot_be_overridden_by_got_in_message(self):
        for code in (-2, 4, None, "invalid", True):
            with self.subTest(code=code):
                self.supply(challenge(), response({"code": code, "message": "got 12 points"}))
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
        self.supply(challenge("first-attempt-nonce"), response({}, status=503),
                    challenge("second-attempt-nonce"), success(), details())
        self.assertEqual(checkin.main(), 0)
        attempts = self.session.request.call_args_list
        self.assertEqual([req.args[0] for req in attempts], ["GET", "POST", "GET", "POST", "GET"])
        self.assertEqual(attempts[1].kwargs["json"]["nonce"], "first-attempt-nonce")
        self.assertEqual(attempts[3].kwargs["json"]["nonce"], "second-attempt-nonce")
        self.assertNotEqual(attempts[1].kwargs["json"]["proof"], attempts[3].kwargs["json"]["proof"])
        self.assertEqual(self.sleep.call_args_list, [call(2.7), call(15), call(2.7)])

    def test_rate_limit_retries_then_succeeds(self):
        self.supply(response({}, status=429), challenge(), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 4)

    def test_exhausted_network_errors_fail_without_leaking_cookie(self):
        self.supply(*[requests.Timeout(COOKIE) for _ in range(checkin.MAX_ATTEMPTS)])
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, checkin.MAX_ATTEMPTS)
        self.assertNotIn("session-value-one", self.output.getvalue())
        self.assertNotIn("signature-value-one", self.output.getvalue())

    def test_checkin_can_wait_for_a_response_slower_than_fifteen_seconds(self):
        def handle(method, url, **kwargs):
            if url == checkin.CHALLENGE_URL:
                return challenge()
            if method == "POST":
                if kwargs["timeout"][1] < 25:
                    raise requests.ReadTimeout("Response would arrive after 25 seconds")
                return success()
            # Optional details keep their shorter timeout.
            self.assertEqual(kwargs["timeout"], (5, 15))
            return details()

        self.session.request.side_effect = handle
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 3)
        self.sleep.assert_called_once_with(2.7)

    def test_read_timeout_then_already_checked_in_is_successful(self):
        # The first POST may have reached the service even if its response was lost.
        self.supply(challenge("timeout-nonce"), requests.ReadTimeout(COOKIE),
                    challenge("retry-nonce"), response({"code": 1,
            "message": "Today's observation logged. Return tomorrow for more points."}), details())
        self.assertEqual(checkin.main(), 0)
        self.assertIn("✅0 ❌0 🔁1", self.send.call_args.args[1])
        self.assertEqual(self.sleep.call_args_list, [call(2.7), call(15), call(2.7)])
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
        self.supply(challenge(), success(), *[requests.ReadTimeout(COOKIE) for _ in range(3)])
        self.assertEqual(checkin.main(), 0)
        self.assertIn("✅1 ❌0 🔁0", self.send.call_args.args[1])
        for request in self.session.request.call_args_list[2:]:
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
                self.supply(challenge(), success(), item)
                self.assertEqual(checkin.main(), 0)
                self.assertIn("✅1 ❌0 🔁0", self.send.call_args.args[1])

    def test_partial_failure_is_red_and_remaining_accounts_are_processed(self):
        with patch.dict(os.environ, {"COOKIES": COOKIE + "\n" + COOKIE_TWO + "\n" + COOKIE}):
            self.supply(challenge(), success(), details(), auth_failure(), challenge(),
                response({"code": 1, "message": "Today's observation logged."}), details())
            self.assertEqual(checkin.main(), 1)
        self.assertEqual(self.session.request.call_count, 7)
        self.assertIn("✅1 ❌1 🔁1", self.send.call_args.args[1])

    def test_each_account_has_its_own_cookie_header_and_clears_session(self):
        seen_headers = []

        def handle(method, url, **kwargs):
            seen_headers.append(kwargs["headers"]["cookie"])
            if url == checkin.CHALLENGE_URL:
                self.assertEqual(len(self.session.cookies), 0)
                return challenge()
            if method == "POST":
                self.session.cookies.set("server-set", "previous-account")
                return success()
            return details()

        self.session.request.side_effect = handle
        with patch.dict(os.environ, {"COOKIES": COOKIE + " & " + COOKIE_TWO}):
            self.assertEqual(checkin.main(), 0)
        self.assertEqual(seen_headers, [COOKIE] * 3 + [COOKIE_TWO] * 3)

    def test_notification_error_does_not_change_business_result_or_log_sendkey(self):
        self.send.side_effect = RuntimeError(SENDKEY)
        self.supply(challenge(), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.supply(auth_failure())
        self.assertEqual(checkin.main(), 1)
        self.assertNotIn(SENDKEY, self.output.getvalue())

    def test_serverchan_api_error_is_not_reported_as_notification_success(self):
        self.send.return_value = {"code": 40001}
        self.supply(challenge(), success(), details())
        self.assertEqual(checkin.main(), 0)
        self.assertIn("Server酱推送失败", self.output.getvalue())
        self.assertNotIn("微信推送完成", self.output.getvalue())

    def test_telegram_and_sct_can_both_receive_the_summary(self):
        self.telegram.side_effect = None
        self.telegram.return_value = response({"ok": True})
        self.supply(challenge(), success(), details())
        with patch.dict(os.environ, {"TG_BOT_TOKEN": "test-bot", "TG_CHAT_ID": "123"}):
            self.assertEqual(checkin.main(), 0)
        self.telegram.assert_called_once()
        self.send.assert_called_once()

    def test_response_message_does_not_log_session_credentials(self):
        self.supply(challenge(), response({"code": 4, "message": "rejected session-value-one signature-value-one"}))
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

    def test_first_checkin_uses_the_issued_nonce_after_the_wait(self):
        def handle(method, url, **kwargs):
            if url == checkin.CHALLENGE_URL:
                self.assertEqual(method, "GET")
                self.assertNotIn("json", kwargs)
                self.sleep.assert_not_called()
                return challenge()
            if url == checkin.CHECKIN_URL:
                self.assertEqual(method, "POST")
                self.sleep.assert_called_once_with(2.7)
                self.assertEqual(kwargs["json"], {
                    "token": "glados.cloud", "nonce": "offline-fixture-nonce",
                    "proof": "f9aa298c6ed1d365d2dd3a47b4901c77492553ecd48ad87dba58ceef12d5e837",
                })
                return success()
            return details()

        self.session.request.side_effect = handle
        self.assertEqual(checkin.main(), 0)
        self.assertEqual(self.session.request.call_count, 3)

    def test_missing_or_invalid_challenge_never_posts_a_legacy_checkin(self):
        for item in (response({"code": 0}), challenge(ttl=1000), challenge(ttl=True),
                     challenge(ttl=float("inf")), response({}, status=404)):
            with self.subTest(response=item.text, http_status=item.status_code):
                self.session.request.reset_mock()
                self.supply(item)
                self.assertEqual(checkin.main(), 1)
                self.session.request.assert_called_once()
                self.assertEqual(self.session.request.call_args.args, ("GET", checkin.CHALLENGE_URL))
        self.sleep.assert_not_called()

    def test_expiry_during_wait_prevents_submission(self):
        self.supply(challenge(ttl=5000))
        with patch.object(checkin.time, "monotonic", side_effect=[10, 10, 16]):
            self.assertEqual(checkin.main(), 1)
        self.session.request.assert_called_once()
        self.assertIn("签到验证已过期", self.send.call_args.args[2])

    def test_challenge_redirect_is_not_followed(self):
        redirected = response({}, status=302)
        redirected.headers["Location"] = "https://example.invalid/checkin"
        self.supply(redirected)
        self.assertEqual(checkin.main(), 1)
        self.session.request.assert_called_once()
        self.assertFalse(self.session.request.call_args.kwargs["allow_redirects"])

    def test_server_echo_of_challenge_secrets_is_redacted(self):
        secrets = []

        def handle(method, url, **kwargs):
            if url == checkin.CHALLENGE_URL:
                return challenge()
            values = kwargs["json"]
            secrets.extend([values["nonce"], values["proof"]])
            return response({"code": 7, "message": "invalid " + " ".join(secrets)})

        self.session.request.side_effect = handle
        self.assertEqual(checkin.main(), 1)
        self.assertEqual(len(secrets), 2)
        for secret in secrets:
            self.assertNotIn(secret, self.output.getvalue())
            self.assertNotIn(secret, self.send.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
