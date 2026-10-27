#!/usr/bin/env python3
"""X 数据回采接收端。

只绑 127.0.0.1:8795，经 Caddy https://example.com/collect-api/* 反代。
接口：POST /ingest（带 X-Collect-Token 或 Authorization: Bearer）
      GET  /health
令牌：/opt/xcollect/collect-token.txt（600，值不进日志/报告）
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

HOST = os.environ.get("XCOLLECT_HOST", "127.0.0.1")
PORT = int(os.environ.get("XCOLLECT_PORT", "8795"))
TOKEN_FILE = Path(os.environ.get("XCOLLECT_TOKEN_FILE", "/opt/xcollect/collect-token.txt"))
MAX_BODY = 512 * 1024
TARGET = core.TARGET_ACCOUNT_AT
log = logging.getLogger("xcollect.server")


def load_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token() -> str:
    """令牌文件不存在时生成一个（权限 600），与手册「首次启动自动生成」一致。"""
    token = load_token()
    if token:
        return token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def normalize_account(value: Any) -> str | None:
    return core.normalize_account(value)


def validate_account(data: dict) -> tuple[bool, str]:
    acct = normalize_account(data.get("account"))
    if acct is None:
        return False, "missing_account"
    if acct != TARGET:
        return False, "not_target_account"
    return True, acct or ""


def parse_captured_at(data: dict) -> Any:
    if data.get("captured_at"):
        dt = core.parse_dt(data.get("captured_at"))
        if dt is None:
            raise ValueError("invalid_captured_at")
        return dt
    return core.now_beijing()


def clean_metrics(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("metrics_must_be_object")
    allowed = ("views", "likes", "bookmarks", "reposts", "replies", "quotes",
               "profile_visits", "link_clicks")
    out = {}
    for key in allowed:
        if key in raw:
            val = core.parse_number(raw[key])
            if val is not None and val >= 0:
                out[key] = val
    return out


def process(data: dict) -> tuple[int, dict]:
    ok, acct = validate_account(data)
    if not ok:
        log.warning("reject account=%s", acct)
        return 400, {"ok": False, "error": acct}
    kind = str(data.get("type") or "").strip().lower()
    try:
        captured = parse_captured_at(data)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}

    if kind == "posts":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return 400, {"ok": False, "error": "items_required"}
        if len(items) > 50:
            return 400, {"ok": False, "error": "too_many_items"}
        safe_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            safe_items.append({
                "id": str(item.get("id") or item.get("post_id") or "")[:32],
                "url": str(item.get("url") or item.get("link") or "")[:500],
                "text": str(item.get("text") or item.get("full_text") or "")[:8000],
                "created_at": item.get("created_at") or item.get("createdAt"),
                "author": str(item.get("author") or item.get("screen_name") or "")[:32],
            })
        result = core.handle_posts_report(safe_items)
        log.info("report posts account=%s received=%d matched=%d no_match=%d ambiguous=%d skipped=%d",
                 acct, result["received"], result["matched"], result["no_match"],
                 result["ambiguous"], result["skipped"])
        return 200, {"ok": True, "type": "posts", **result}

    if kind == "metrics":
        post_id = str(data.get("post_id") or data.get("id") or "").strip()
        if not (post_id.isdigit() and 10 <= len(post_id) <= 25):
            return 400, {"ok": False, "error": "invalid_post_id"}
        metrics = clean_metrics(data.get("metrics"))
        if not metrics:
            return 400, {"ok": False, "error": "no_metrics"}
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        result = core.handle_metric_report(
            post_id=post_id, metrics=metrics, captured_at=captured,
            source="ingest", raw=raw,
        )
        log.info("report metrics post=%s metrics=%s result=%s",
                 post_id, json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                 result.get("fill", {}).get("reason", result.get("reason")))
        return 200, {"ok": True, "type": "metrics", **result}

    if kind == "account":
        followers = core.parse_number(data.get("followers"))
        verified = core.parse_number(data.get("verified_followers"))
        if followers is not None and (followers < 0 or followers > 10**12):
            return 400, {"ok": False, "error": "invalid_followers"}
        if verified is not None and (verified < 0 or verified > 10**12):
            return 400, {"ok": False, "error": "invalid_verified_followers"}
        result = core.handle_account_report(
            account=acct, followers=followers, verified_followers=verified,
            captured_at=captured, source="ingest",
        )
        log.info("report account=%s followers=%s verified=%s ignored_followers=%s result=%s",
                 acct, followers, verified, result.get("ignored_followers"),
                 "created" if result.get("created") else result.get("reason", "duplicate"))
        return 200, {"ok": True, "type": "account", **result}

    return 400, {"ok": False, "error": "unknown_type"}


class Handler(BaseHTTPRequestHandler):
    server_version = "XCollect/1.0"
    protocol_version = "HTTP/1.1"

    def allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or origin in {
            "https://x.com", "https://twitter.com", "https://www.x.com",
        }:
            return origin
        return None

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def token_ok(self) -> bool:
        expected = load_token()
        supplied = self.headers.get("X-Collect-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        return bool(expected) and bool(supplied) and secrets.compare_digest(supplied, expected)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/health", "/collect-api/health"}:
            self.send_json(200, {"ok": True, "service": "xcollect"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_OPTIONS(self) -> None:
        # 扩展 service worker 的跨域预检：只放行扩展自身与 X 页面来源。
        self.send_response(204)
        self.send_header("Allow", "POST, GET, OPTIONS")
        origin = self.allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Collect-Token, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        # 不复用连接：拒绝请求时可能未读 body，避免 keep-alive 串包。
        self.close_connection = True
        path = urlparse(self.path).path
        if path not in {"/ingest", "/collect-api/ingest"}:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.token_ok():
            log.warning("ingest rejected: invalid token from %s", self.address_string())
            self.send_json(403, {"ok": False, "error": "invalid_token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.send_json(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length > MAX_BODY:
            self.send_json(413, {"ok": False, "error": "body_too_large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(data, dict):
            self.send_json(400, {"ok": False, "error": "body_must_be_object"})
            return
        try:
            status, payload = process(data)
        except core.LarkError as exc:
            log.error("feishu error: %s", str(exc)[:800])
            self.send_json(502, {"ok": False, "error": "feishu_error"})
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest internal error: %s", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        self.send_json(status, payload)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> int:
    core.configure_logging("ingest")
    token = ensure_token()
    try:
        mode = TOKEN_FILE.stat().st_mode & 0o777
        if mode != 0o600:
            log.warning("token file mode is %o, expected 600", mode)
    except OSError:
        pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("xcollect ingest listening on %s:%s (token file %s, target %s)",
             HOST, PORT, TOKEN_FILE, TARGET)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
