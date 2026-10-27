#!/usr/bin/env python3
"""X 发布板状态服务：接收板页「发完了」/「撤销已发布」，回写飞书发布库。

接口：POST /x-api/state，JSON 体 {board_no, action}，action ∈ {published, undo}。
请求头 X-Board-Token 须与 /opt/xboard/state-token.txt 一致，否则 403。
只绑 127.0.0.1，经 Caddy /x-api/ 反代对外。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

HOST = os.environ.get("XBOARD_STATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("XBOARD_STATE_PORT", "8791"))
TOKEN_FILE = Path(os.environ.get("XBOARD_TOKEN_FILE", "/opt/xboard/state-token.txt"))
LARK = os.environ.get("LARK_CLI", "/opt/node24/bin/lark-cli")
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")
TABLE_ID = os.environ.get("FEISHU_PUBLISH_TABLE_ID", "")
SHANGHAI = ZoneInfo("Asia/Shanghai")
BOARD_NO_RE = re.compile(r"^x-[a-z0-9]{4,32}$")


def load_token() -> str:
    return TOKEN_FILE.read_text().strip()


def run_lark(*args: str, timeout: int = 60) -> dict:
    cmd = [LARK, "base", *args, "--as", "user", "--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {proc.stdout}{proc.stderr}")
    return json.loads(proc.stdout)


def find_record(board_no: str) -> str | None:
    out = run_lark("+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
                   "--filter-json", json.dumps(
                       {"logic": "and", "conditions": [["板上编号", "==", board_no]]},
                       ensure_ascii=False),
                   "--field-id", "板上编号", "--limit", "10")
    data = out.get("data") or {}
    rids = data.get("record_id_list") or []
    return rids[0] if rids else None


def update_record(record_id: str, fields: dict) -> None:
    body = json.dumps({"update_records": {record_id: fields}}, ensure_ascii=False)
    run_lark("+record-batch-update", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
             "--json", body)


def read_field(record_id: str, field: str):
    out = run_lark("+record-get", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
                   "--record-id", record_id, "--field-id", field)
    data = out["data"]
    row = data["data"][0]
    return row[0] if row else None


class Handler(BaseHTTPRequestHandler):
    server_version = "XBoardState/1"

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def token_ok(self) -> bool:
        expected = load_token()
        supplied = self.headers.get("X-Board-Token", "")
        return bool(expected) and bool(supplied) and secrets.compare_digest(supplied, expected)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/x-api/state", "/state"}:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.token_ok():
            self.send_json(403, {"ok": False, "error": "invalid_token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ValueError("body_too_large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("body_must_be_object")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid_json"})
            return
        board_no = str(data.get("board_no", ""))
        action = str(data.get("action", ""))
        if not BOARD_NO_RE.fullmatch(board_no):
            self.send_json(400, {"ok": False, "error": "invalid_board_no"})
            return
        if action not in {"published", "undo"}:
            self.send_json(400, {"ok": False, "error": "invalid_action"})
            return
        try:
            record_id = find_record(board_no)
        except Exception as exc:  # noqa: BLE001
            self.send_json(502, {"ok": False, "error": f"feishu_lookup_failed: {exc}"})
            return
        if not record_id:
            self.send_json(404, {"ok": False, "error": "board_no_not_found"})
            return
        try:
            if action == "published":
                published_at = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M")
                update_record(record_id, {"状态": ["已发布"], "发布时间": published_at})
                self.send_json(200, {"ok": True, "board_no": board_no,
                                     "status": "已发布", "published_at": published_at})
            else:
                update_record(record_id, {"状态": ["已上板"], "发布时间": None})
                self.send_json(200, {"ok": True, "board_no": board_no, "status": "已上板"})
        except Exception as exc:  # noqa: BLE001
            self.send_json(502, {"ok": False, "error": f"feishu_update_failed: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
