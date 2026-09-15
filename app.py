"""
followings-viewer — a tiny standalone web tool.

Enter a public Instagram username, get back the list of accounts that
username follows (its "following" list), via the HikerAPI service
(https://hikerapi.com). Same API insto's `hiker` backend uses, just a
much smaller, self-contained wrapper with a one-box web UI on top.

Run:
    export HIKERAPI_TOKEN=hk_live_...
    python app.py
Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import os

import hikerapi
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Hard ceiling regardless of what the client asks for, so a typo/misuse
# can't blow through your HikerAPI quota in one request.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 20.0


def _get_token() -> str | None:
    return os.environ.get("HIKERAPI_TOKEN")


def _clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").strip()


async def _fetch_following(username: str, limit: int) -> dict:
    """Resolve `username` to a pk, then page through user_following_chunk_v1.

    Mirrors insto's HikerBackend.resolve_target + iter_user_following, but
    inlined here so this project has no dependency on insto itself.
    """
    token = _get_token()
    if not token:
        raise RuntimeError(
            "HIKERAPI_TOKEN is not set on the server. Set it as an "
            "environment variable before starting app.py."
        )

    client = hikerapi.AsyncClient(token=token, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        profile_payload = await client.user_by_username_v2(username=username)
        user = _unwrap_user(profile_payload)
        if not isinstance(user, dict) or not user.get("pk"):
            raise LookupError(f"user '{username}' not found")
        pk = str(user["pk"])
        target_is_private = bool(user.get("is_private", False))

        results: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while len(results) < limit:
            payload = await client.user_following_chunk_v1(user_id=pk, max_id=cursor)

            items, next_cursor = _extract_chunk(payload)
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

            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return {
            "username": username,
            "pk": pk,
            "target_is_private": target_is_private,
            "count": len(results),
            "following": results,
        }
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()


def _unwrap_user(payload) -> dict | None:
    """HikerAPI's user endpoints sometimes wrap the user dict as
    {"user": {...}} and sometimes return the user dict directly —
    handle both, same as insto's HikerBackend._unwrap_user does.
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get("user")
    if isinstance(inner, dict):
        return inner
    return payload


def _extract_chunk(payload) -> tuple[list, str | None]:
    """Same three response shapes HikerAPI's chunk endpoints can return."""
    if isinstance(payload, list) and len(payload) == 2:
        raw_items, raw_cursor = payload
        items = list(raw_items) if isinstance(raw_items, list) else []
        cursor = None if raw_cursor in (None, False, "") else str(raw_cursor)
        return items, cursor

    if isinstance(payload, dict):
        inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        items = []
        for key in ("users", "items"):
            candidate = inner.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        cursor = None
        for key in ("next_max_id", "next_page_id", "end_cursor", "next_min_id"):
            value = inner.get(key, payload.get(key))
            if value not in (None, False, ""):
                cursor = str(value)
                break
        return items, cursor

    return [], None


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
        return jsonify({"error": "HIKERAPI_TOKEN روی سرور تنظیم نشده."}), 500

    try:
        data = asyncio.run(_fetch_following(username, limit))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # HikerAPI/network errors, private/login-walled, etc.
        return jsonify({"error": f"خطا در گرفتن اطلاعات: {exc}"}), 502

    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
