#!/usr/bin/env python3
"""把 extension/ 模板生成你自己的扩展：替换 X 账号与站点地址占位符后复制到 --out 目录。

用法（服务器 / 自有域名模式）：
  python3 configure_extension.py --handle your_handle --domain example.com --out /tmp/my-extension

用法（无服务器 / 无域名模式：本地网关或 Tailscale Funnel 地址）：
  python3 configure_extension.py --handle your_handle --base-url http://127.0.0.1:8799 --out /tmp/my-extension
  python3 configure_extension.py --handle your_handle --base-url https://your-machine.your-tailnet.ts.net --out /tmp/my-extension

--base-url 是站点的根地址（不带路径、不带结尾斜杠）：上报端点拼为
<base-url>/collect-api/ingest，发布板为 <base-url>/x/。给了 --base-url 就不用再给 --domain。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

TEXT_SUFFIXES = {".js", ".json", ".html", ".md", ".txt"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True, help="你的 X handle，不带 @，如 your_handle")
    ap.add_argument("--domain", help="你的站点域名，如 example.com（与 --base-url 二选一）")
    ap.add_argument("--base-url", help="站点根地址，如 http://127.0.0.1:8799 或 "
                                       "https://your-machine.your-tailnet.ts.net（与 --domain 二选一）")
    ap.add_argument("--out", required=True, help="生成目录（须不存在或为空）")
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent), help="模板目录，默认本脚本所在目录")
    args = ap.parse_args()

    handle = args.handle.lstrip("@").strip()
    if args.base_url:
        base_url = args.base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print("--base-url 须形如 http://127.0.0.1:8799 或 https://example.com", file=sys.stderr)
            return 2
        if parsed.path not in ("", "/"):
            print("--base-url 只填根地址，不要带 /x/ 之类的路径", file=sys.stderr)
            return 2
        # manifest 的 host 权限不允许带端口：单独拼 scheme://主机/*
        host_pattern = f"{parsed.scheme}://{parsed.hostname}/*"
        display_host = parsed.netloc
    elif args.domain:
        domain = args.domain.replace("https://", "").replace("http://", "").strip("/")
        if not domain:
            print("--domain 不能为空", file=sys.stderr)
            return 2
        base_url = f"https://{domain}"
        host_pattern = f"https://{domain}/*"
        display_host = domain
    else:
        print("必须给 --domain 或 --base-url 之一", file=sys.stderr)
        return 2
    if not handle:
        print("handle 不能为空", file=sys.stderr)
        return 2

    src = Path(args.src)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"输出目录已存在且非空：{out}", file=sys.stderr)
        return 2
    shutil.copytree(src, out, dirs_exist_ok=True)

    replaced = 0
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in TEXT_SUFFIXES and p.name != Path(__file__).name:
            t = p.read_text(encoding="utf-8")
            # 先换带 scheme 的完整前缀（端点、上报地址、板页链接、manifest 权限），
            # 再换剩下的裸域名（popup 里的展示文字）。
            n = t.replace("https://YOUR_DOMAIN", base_url).replace("YOUR_DOMAIN", display_host)
            n = n.replace("YOUR_X_HANDLE", handle)
            if n != t:
                p.write_text(n, encoding="utf-8")
                replaced += 1

    # manifest.json 的 host_permissions 里绝不能带端口，校正为 scheme://主机/*
    manifest_path = out / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hosts = manifest.get("host_permissions") or []
        fixed = [host_pattern if h.startswith(base_url) else h for h in hosts]
        if fixed != hosts:
            manifest["host_permissions"] = fixed
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

    leftover = []
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in TEXT_SUFFIXES and p.name != Path(__file__).name:
            t = p.read_text(encoding="utf-8")
            if "YOUR_X_HANDLE" in t or "YOUR_DOMAIN" in t:
                leftover.append(str(p))
    if leftover:
        print(f"仍有未替换占位符：{leftover}", file=sys.stderr)
        return 1
    print(f"已生成扩展：{out}（替换了 {replaced} 个文件）")
    print(f"上报端点：{base_url}/collect-api/ingest；发布板：{base_url}/x/")
    print("下一步：chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选这个目录；")
    print("再在扩展 popup 里填入回采令牌（服务器版在 /opt/xcollect/collect-token.txt，"
          "本地版在你 config.env 的 XCOLLECT_TOKEN_FILE 指向的文件）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
