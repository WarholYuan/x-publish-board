#!/usr/bin/env python3
"""本地网关：无服务器 / 无域名模式的统一入口（纯 Python 标准库，零依赖）。

把服务器部署里 Caddy 干的三件事搬到你自己的电脑上，只绑本机回环地址：

  GET  /x/、/x/*            → 静态发布板（目录取 XBOARD_OUT_DIR，与渲染脚本一致）
  *    /x-api/*              → 反代状态服务  127.0.0.1:8791（路径原样转发）
  *    /collect-api/*        → 反代回采接收端 127.0.0.1:8795（路径原样转发）
  GET  /xcollect/extension.zip → 扩展安装包（文件存在才有，路径取 EXTENSION_ZIP_OUT）

用法（先起 state_server.py 与 ingest_server.py，再起本网关）：
  python3 local/gateway.py            # 打开 http://127.0.0.1:8799/x/

想让手机也能打开、或想要一个稳定的 https 地址，在本网关之上再套一层
Tailscale Funnel 即可（tailscale funnel --bg 8799），见 docs/no-server.md。

环境变量（都有默认值，一般不用改）：
  XLOCAL_HOST / XLOCAL_PORT          网关监听地址与端口（默认 127.0.0.1:8799）
  XBOARD_OUT_DIR                     发布板静态目录（默认 /var/www/x-publish-board/x，
                                     本地模式请在 config.env 里改成本地目录）
  XBOARD_STATE_HOST / XBOARD_STATE_PORT   状态服务地址（默认 127.0.0.1:8791）
  XCOLLECT_HOST / XCOLLECT_PORT      回采接收端地址（默认 127.0.0.1:8795）
  EXTENSION_ZIP_OUT                  扩展 zip 路径（默认 /var/www/x-publish-board/xcollect/extension.zip）

安全说明：网关默认只绑 127.0.0.1，本机以外访问不到。一旦用 Funnel 之类
把它暴露到公网，暴露面与服务器版相同——板页里嵌着状态令牌、两个 API 都靠
令牌鉴权，所以那条 https 地址不要公开分享，当自家地址用。
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HOST = os.environ.get("XLOCAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("XLOCAL_PORT", "8799"))
OUT_DIR = Path(os.environ.get("XBOARD_OUT_DIR", "/var/www/x-publish-board/x"))
EXT_ZIP = Path(os.environ.get(
    "EXTENSION_ZIP_OUT", "/var/www/x-publish-board/xcollect/extension.zip"))
STATE_ADDR = (os.environ.get("XBOARD_STATE_HOST", "127.0.0.1"),
              int(os.environ.get("XBOARD_STATE_PORT", "8791")))
COLLECT_ADDR = (os.environ.get("XCOLLECT_HOST", "127.0.0.1"),
                int(os.environ.get("XCOLLECT_PORT", "8795")))

HOP_HEADERS = {"host", "content-length", "connection", "keep-alive",
               "proxy-connection", "transfer-encoding", "te", "trailer",
               "upgrade"}


class Handler(BaseHTTPRequestHandler):
    server_version = "XLocalGateway/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 通用 ----------
    def send_bytes(self, status: int, body: bytes, ctype: str,
                   extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")

    # ---------- 静态板页 ----------
    def serve_static(self, rel: str) -> None:
        rel = unquote(rel)
        if rel in ("", "/"):
            target = OUT_DIR / "index.html"
        else:
            target = (OUT_DIR / rel.lstrip("/")).resolve()
            try:
                target.relative_to(OUT_DIR.resolve())
            except ValueError:
                self.send_json(403, {"ok": False, "error": "forbidden"})
                return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_json(404, {"ok": False, "error": "not_found",
                                 "hint": "板页还没渲染出来，先跑一次 render_board.py"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_bytes(200, target.read_bytes(), ctype)

    # ---------- 反代 ----------
    def proxy(self, addr: tuple[str, int]) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        conn = http.client.HTTPConnection(addr[0], addr[1], timeout=120)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except OSError:
            self.send_json(502, {"ok": False, "error": "backend_down",
                                 "hint": f"后端服务 {addr[0]}:{addr[1]} 没在跑"})
            return
        finally:
            conn.close()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------- 路由 ----------
    def route(self) -> None:
        path = urlparse(self.path).path
        if path in ("/x-api", "/x-api/") or path.startswith("/x-api/"):
            self.proxy(STATE_ADDR)
        elif path in ("/collect-api", "/collect-api/") or path.startswith("/collect-api/"):
            self.proxy(COLLECT_ADDR)
        elif path == "/x":
            self.send_bytes(301, b"", "text/plain", {"Location": "/x/"})
        elif path == "/x/" or path.startswith("/x/"):
            if self.command not in ("GET", "HEAD"):
                self.send_json(405, {"ok": False, "error": "method_not_allowed"})
                return
            self.serve_static(path[2:])  # 去掉 "/x"，余下以 / 开头
        elif path == "/xcollect/extension.zip":
            if EXT_ZIP.is_file():
                self.send_bytes(200, EXT_ZIP.read_bytes(), "application/zip")
            else:
                self.send_json(404, {"ok": False, "error": "extension_zip_not_found"})
        elif path == "/":
            self.send_bytes(301, b"", "text/plain", {"Location": "/x/"})
        elif path == "/health":
            self.send_json(200, {"ok": True, "service": "xlocal-gateway",
                                 "board_dir": str(OUT_DIR)})
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})

    do_GET = route
    do_HEAD = route
    do_POST = route
    do_OPTIONS = route

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    if not OUT_DIR.exists():
        print(f"提示：板页目录还不存在（{OUT_DIR}），先渲染一次再刷新页面。",
              file=sys.stderr)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"本地网关已启动：http://{HOST}:{PORT}/x/  "
          f"（状态服务 → {STATE_ADDR[0]}:{STATE_ADDR[1]}，"
          f"回采 → {COLLECT_ADDR[0]}:{COLLECT_ADDR[1]}）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
