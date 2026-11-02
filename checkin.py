import os
import random
import re
import sys
import time

import requests
from serverchan_sdk import sc_send

CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"
HEADERS_BASE = {
    "origin": "https://glados.cloud",
    "referer": "https://glados.cloud/console/checkin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
PAYLOAD = {"token": "glados.cloud"}
TIMEOUT = (5, 15)
# Allow slow check-in responses while keeping the shorter timeout for
# optional account queries and Telegram requests.
CHECKIN_TIMEOUT = (10, 60)
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 15
SESSION_KEY = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+):sess(?P<sig>\.sig)?$")


def normalize_cookie(cookie):
    """Remove paste wrappers without changing session names or values (upstream #16)."""
    cookie = cookie.strip()
    while cookie:
        previous = cookie
        if len(cookie) >= 2 and cookie[0] == cookie[-1] and cookie[0] in "\"'":
            cookie = cookie[1:-1].strip()
        if cookie[:7].lower() == "cookie:":
            cookie = cookie[7:].strip()
        if cookie == previous:
            break
    return cookie


def parse_cookies(raw):
    # Keep the existing & separator; also accept upstream's ||| and newline forms.
    return [cookie for part in re.split(r"\|\|\||[&\r\n]", raw)
            if (cookie := normalize_cookie(part))]


def session_prefix(cookie):
    """Require a nonempty session/signature pair with the same, arbitrary prefix."""
    groups = {}
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        match = SESSION_KEY.fullmatch(key.strip())
        if match and separator and value.strip():
            field = "sig" if match.group("sig") else "sess"
            groups.setdefault(match.group("prefix"), set()).add(field)
    return next((prefix for prefix, fields in groups.items()
                 if fields == {"sess", "sig"}), None)


def classify_checkin(code):
    # Business codes are authoritative; a failure message containing "got" is not success.
    if type(code) is int:
        return {0: "ok", 1: "repeat"}.get(code, "fail")
    if isinstance(code, str):
        return {"0": "ok", "1": "repeat"}.get(code.strip(), "fail")
    return "fail"


def safe_json(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def request_json(session, method, url, headers):
    timeout = CHECKIN_TIMEOUT if url == CHECKIN_URL else TIMEOUT
    label = "CHECKIN" if method == "POST" else "ACCOUNT"
    for attempt in range(MAX_ATTEMPTS):
        try:
            print(f"{label}_ATTEMPT: {attempt + 1}/{MAX_ATTEMPTS} "
                  f"(connect={timeout[0]}s, read={timeout[1]}s)")
            kwargs = {"headers": headers, "timeout": timeout}
            if method == "POST":
                kwargs["json"] = PAYLOAD
            response = session.request(method, url, **kwargs)
            print(f"{label}_STATUS:", response.status_code)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data
        except requests.RequestException as error:
            response = error.response
            status = response.status_code if response is not None else None
            retryable = (not isinstance(error, requests.HTTPError)
                         or status == 429 or (status is not None and 500 <= status < 600))
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise
            delay = min(RETRY_DELAY_SECONDS * (2 ** attempt), 30)
            print(f"⚠️ {label} 请求暂时失败（{type(error).__name__}，HTTP {status or '-'}），"
                  f"{delay} 秒后重试 {attempt + 1}/{MAX_ATTEMPTS - 1}")
            time.sleep(delay)


def redact_message(message, cookie):
    message = message if isinstance(message, str) else ""
    for part in cookie.split(";"):
        value = part.strip().partition("=")[2]
        if len(value) >= 4:
            message = message.replace(value, "[已隐藏]")
    return message[:200]


def mask_email(email):
    if not isinstance(email, str) or "@" not in email:
        return "unknown"
    name, domain = email.rsplit("@", 1)
    return f"{name[:2] if len(name) > 3 else ''}***@{domain}"


def checkin_account(session, cookie, index):
    account = {"result": "fail", "email": "unknown", "points": "-",
               "days": "-", "status": "❌ 失败"}
    prefix = session_prefix(cookie)
    if prefix is None:
        account["status"] = "❌ Cookie 格式错误：sess 与 sess.sig 必须同前缀成对且非空，请重新获取完整 Cookie"
        return account

    print(f"账号 {index} 会话前缀：{prefix}:")
    session.cookies.clear()  # Prevent cookies set for one account affecting another.
    headers = {**HEADERS_BASE, "cookie": cookie}
    try:
        data = request_json(session, "POST", CHECKIN_URL, headers)
        message = redact_message(data.get("message"), cookie)
        code = data.get("code")
        print("CHECKIN_RESP:", {"code": code, "message": message})
        account["result"] = classify_checkin(code)
        if account["result"] == "ok":
            account["status"] = "✅ 成功"
        elif account["result"] == "repeat":
            account["status"] = "🔁 已签到"
        else:
            if code in (-2, "-2") or any(word in message.lower() for word in
                    ("没有权限", "未登录", "unauthorized", "forbidden")):
                account["status"] = "❌ 鉴权失败：请重新登录并更新 COOKIES"
            else:
                account["status"] = f"❌ 失败：{message or '未知业务响应'}"
            return account
        points = data.get("points", "-")
        if isinstance(points, (int, float, str)):
            account["points"] = str(points)[:32]
    except Exception as error:
        response = getattr(error, "response", None)
        status = response.status_code if response is not None else None
        if status in (401, 403):
            account["status"] = f"❌ 鉴权失败（HTTP {status}）：请重新登录并更新 COOKIES"
        elif isinstance(error, requests.Timeout):
            account["status"] = (f"❌ 网络超时（{type(error).__name__}）："
                                 f"{MAX_ATTEMPTS} 次请求后仍未确认签到结果，请稍后重跑")
        else:
            account["status"] = f"❌ 请求异常（{type(error).__name__}，HTTP {status or '-'}）"
        return account

    # Account details are optional: their failure must not change the check-in result.
    try:
        response = request_json(session, "GET", STATUS_URL, headers)
        details = response.get("data") or {}
        account["email"] = mask_email(details.get("email"))
        if details.get("leftDays") is not None:
            account["days"] = f"{int(float(details['leftDays']))} 天"
    except Exception as error:
        print(f"⚠️ 账号 {index} 状态查询失败（{type(error).__name__}），保留签到结果")
    return account


def push_telegram(bot_token, chat_id, title, content):
    if not bot_token or not chat_id:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n\n{content}"[:4000]},
            timeout=TIMEOUT,
        )
        if response.status_code == 200 and safe_json(response).get("ok"):
            print("✅ Telegram 推送成功")
        else:
            print(f"⚠️ Telegram 推送失败：HTTP {response.status_code}")
    except Exception as error:
        print(f"⚠️ Telegram 推送异常（{type(error).__name__}）")


def push_serverchan(sendkey, title, content):
    if not sendkey:
        return
    try:
        response = sc_send(sendkey, title, content, {"tags": "GLaDOS|签到"})
        if isinstance(response, dict) and response.get("code") == 0:
            print("✅ Server酱微信推送完成")
        else:
            print("⚠️ Server酱推送失败，请检查 SCT_SENDKEY 和推送服务状态")
        # Never print the raw SDK response: it contains a notification read key.
    except Exception as error:
        print(f"⚠️ Server酱推送异常（{type(error).__name__}）")


def push_all(bot_token, chat_id, sendkey, title, content):
    push_telegram(bot_token, chat_id, title, content)
    push_serverchan(sendkey, title, content)
    if not sendkey and not (bot_token and chat_id):
        print("⚠️ 未配置推送渠道，可在 Secrets 中配置 SCT_SENDKEY 或 Telegram")


def main():
    bot_token = os.getenv("TG_BOT_TOKEN", "")
    chat_id = os.getenv("TG_CHAT_ID", "")
    sendkey = os.getenv("SCT_SENDKEY", "")
    cookies = parse_cookies(os.getenv("COOKIES", ""))
    if not cookies:
        message = "❌ 未检测到 COOKIES，请在仓库 Actions Secrets 中配置"
        print(message)
        push_all(bot_token, chat_id, sendkey, "GLaDOS 签到失败", message)
        return 1

    counts = {"ok": 0, "fail": 0, "repeat": 0}
    lines = []
    with requests.Session() as session:
        for index, cookie in enumerate(cookies, 1):
            account = checkin_account(session, cookie, index)
            counts[account["result"]] += 1
            lines.append(f"{index}. {account['email']} | {account['status']} | "
                         f"P:{account['points']} | 剩余:{account['days']}")
            if index < len(cookies):
                time.sleep(random.uniform(1, 2))

    title = f"GLaDOS 签到完成 ✅{counts['ok']} ❌{counts['fail']} 🔁{counts['repeat']}"
    content = "\n".join(lines)
    print(title)
    print(content)
    # Send the existing notifications before returning a failed CI result.
    push_all(bot_token, chat_id, sendkey, title, content)
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
