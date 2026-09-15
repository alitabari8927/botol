"""
followings-viewer — a tiny standalone web tool.

Enter a public Instagram username, get back the list of accounts that
username follows (its "following" list), via RocketAPI
(https://rocketapi.io). Just a small, self-contained wrapper with a
one-box web UI on top.

Run:
    export ROCKETAPI_TOKEN=your-rocketapi-token
    python app.py
Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import os

import httpx
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Hard ceiling regardless of what the client asks for, so a typo/misuse
# can't blow through your RocketAPI quota in one request.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 20.0

ROCKETAPI_BASE_URL = "https://v1.rocketapi.io"
# RocketAPI's own page-size cap for the "get_following" endpoint.
ROCKETAPI_FOLLOWING_PAGE_SIZE = 200


def _get_token() -> str | None:
    return os.environ.get("ROCKETAPI_TOKEN")


def _clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").strip()


class RocketAPIError(Exception):
    """Raised for any RocketAPI-level failure, already carrying a
    Persian, user-facing message plus the http status to answer with."""

    def __init__(self, message: str, http_status: int = 502):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _rocket_post(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    """POST to a RocketAPI endpoint and return the inner `response.body`.

    RocketAPI wraps every result as:
        {"status": "done", "response": {"status_code": 200,
                                         "content_type": "application/json",
                                         "body": {...actual payload...}}}
    A non-2xx status on the *outer* HTTP call means the request to
    RocketAPI itself failed (bad token, no credits, rate limit, RocketAPI
    outage). A "done" envelope with an inner status_code that isn't 200
    means Instagram itself rejected/blocked the underlying request
    (private account, not found, login required, etc).
    """
    token = _get_token()
    try:
        resp = await client.post(
            f"{ROCKETAPI_BASE_URL}/{path}",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {token}",
            },
        )
    except httpx.RequestError as exc:
        raise RocketAPIError(f"خطا در ارتباط با RocketAPI: {exc}", 502) from exc

    if resp.status_code != 200:
        raise RocketAPIError(_friendly_http_error(resp.status_code), 502)

    try:
        envelope = resp.json()
    except ValueError as exc:
        raise RocketAPIError("پاسخ نامعتبر از RocketAPI دریافت شد.", 502) from exc

    if not isinstance(envelope, dict) or envelope.get("status") != "done":
        raise RocketAPIError(
            f"RocketAPI درخواست را کامل نکرد: {envelope}", 502
        )

    inner = envelope.get("response") or {}
    inner_status = inner.get("status_code")
    body = inner.get("body")

    if inner_status != 200 or not isinstance(body, dict):
        raise RocketAPIError(_friendly_inner_error(inner_status, body), 502)

    return body


def _friendly_http_error(status: int) -> str:
    if status == 401:
        return "توکن RocketAPI نامعتبر است (401). توکن را در متغیر ROCKETAPI_TOKEN چک کن."
    if status == 402:
        return "سهمیهٔ حساب RocketAPI تمام شده (402). باید شارژ/آپگرید کنی."
    if status == 403:
        return "دسترسی مسدود شده (403) — توکن یا حساب RocketAPI مشکل دارد."
    if status == 429:
        return "درخواست‌های زیاد، ریت‌لیمیت خوردی (429) — کمی صبر کن و دوباره امتحان کن."
    if 500 <= status < 600:
        return f"خطای سرور RocketAPI ({status}) — موقتی است، دوباره امتحان کن."
    return f"خطای RocketAPI با کد {status}"


def _friendly_inner_error(inner_status: int | None, body) -> str:
    # Instagram-side rejection surfaced through RocketAPI. `body` is often
    # a dict with a "message"/"error_type" even when status_code != 200.
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error_type") or "")

    if inner_status == 404:
        return "همچین یوزرنیمی روی اینستاگرام پیدا نشد (404)."
    if inner_status in (400, 401) and "login_required" in message.lower():
        return "برای این حساب (احتمالاً private) نیاز به لاگین است و RocketAPI بدون لاگین نمی‌تواند لیست را بگیرد."
    if inner_status == 429:
        return "اینستاگرام موقتاً ریت‌لیمیت کرده — کمی صبر کن و دوباره امتحان کن."
    if inner_status:
        return f"اینستاگرام درخواست را رد کرد (کد {inner_status})" + (f": {message}" if message else "")
    return f"پاسخ غیرمنتظره از RocketAPI: {body}"


async def _fetch_following(username: str, limit: int) -> dict:
    """Resolve `username` to a numeric id, then page through
    instagram/user/get_following until `limit` accounts are collected."""
    token = _get_token()
    if not token:
        raise RuntimeError(
            "ROCKETAPI_TOKEN is not set on the server. Set it as an "
            "environment variable before starting app.py."
        )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        profile_body = await _rocket_post(
            client, "instagram/user/get_web_profile_info", {"username": username}
        )
        user = _unwrap_profile_user(profile_body)
        if not isinstance(user, dict) or not user.get("id"):
            raise LookupError(f"user '{username}' not found")

        user_id = int(user["id"])
        target_is_private = bool(user.get("is_private", False))

        results: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while len(results) < limit:
            page_payload: dict = {
                "id": user_id,
                "count": min(ROCKETAPI_FOLLOWING_PAGE_SIZE, limit - len(results)),
            }
            if cursor:
                page_payload["max_id"] = cursor

            body = await _rocket_post(client, "instagram/user/get_following", page_payload)
            items, next_cursor = _extract_following_page(body)

            for item in items:
                if not isinstance(item, dict) or not item.get("pk"):
                    continue
                results.append(
                    {
                        "pk": str(item["pk"]),
                        "username": item.get("username", ""),
                        "full_name": item.get("full_name") or "",
                        "is_private": bool(item.get("is_private", False)),
                        "is_verified": bool(item.get("is_verified", False)),
                        "profile_pic_url": item.get("profile_pic_url") or "",
                    }
                )
                if len(results) >= limit:
                    break

            if not next_cursor or next_cursor in seen_cursors or not items:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return {
            "username": username,
            "pk": str(user_id),
            "target_is_private": target_is_private,
            "count": len(results),
            "following": results,
        }


def _unwrap_profile_user(body: dict) -> dict | None:
    """`get_web_profile_info` mirrors Instagram's own web endpoint shape:
    {"data": {"user": {...}}, "status": "ok"} — but fall back to a
    top-level "user" key or the raw body, in case RocketAPI changes it."""
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        return data["user"]
    if isinstance(body.get("user"), dict):
        return body["user"]
    if body.get("id"):
        return body
    return None


def _extract_following_page(body: dict) -> tuple[list, str | None]:
    """`get_following` mirrors Instagram's private "following" endpoint
    shape: {"users": [...], "next_max_id": ..., "status": "ok"}."""
    items = []
    for key in ("users", "items"):
        candidate = body.get(key)
        if isinstance(candidate, list):
            items = candidate
            break

    cursor = None
    for key in ("next_max_id", "next_page_id", "end_cursor", "next_min_id"):
        value = body.get(key)
        if value not in (None, False, ""):
            cursor = str(value)
            break

    return items, cursor


@app.get("/")
def index():
    return render_template("index.html", default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT)


@app.get("/api/following")
def api_following():
    raw_username = request.args.get("username", "")
    username = _clean_username(raw_username)
    if not username:
        return jsonify({"error": "یک یوزرنیم وارد کن."}), 400

    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        return jsonify({"error": "عدد limit نامعتبر است."}), 400
    limit = max(1, min(limit, MAX_LIMIT))

    if not _get_token():
        return jsonify({"error": "ROCKETAPI_TOKEN روی سرور تنظیم نشده."}), 500

    try:
        data = asyncio.run(_fetch_following(username, limit))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except RocketAPIError as exc:
        return jsonify({"error": exc.message}), exc.http_status
    except Exception as exc:  # network errors, timeouts, etc.
        return jsonify({"error": f"خطا در گرفتن اطلاعات: {exc}"}), 502

    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
