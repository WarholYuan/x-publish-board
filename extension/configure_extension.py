#!/usr/bin/env python3
"""把 extension/ 模板生成你自己的扩展：替换 X 账号与域名占位符后复制到 --out 目录。

用法：
  python3 configure_extension.py --handle your_handle --domain example.com --out /tmp/my-extension
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

TEXT_SUFFIXES = {".js", ".json", ".html", ".md", ".txt"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True, help="你的 X handle，不带 @，如 your_handle")
    ap.add_argument("--domain", required=True, help="你的站点域名，如 example.com")
    ap.add_argument("--out", required=True, help="生成目录（须不存在或为空）")
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent), help="模板目录，默认本脚本所在目录")
    args = ap.parse_args()

    handle = args.handle.lstrip("@").strip()
    domain = args.domain.replace("https://", "").replace("http://", "").strip("/")
    if not handle or not domain:
        print("handle / domain 不能为空", file=sys.stderr)
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
            n = t.replace("YOUR_X_HANDLE", handle).replace("YOUR_DOMAIN", domain)
            if n != t:
                p.write_text(n, encoding="utf-8")
                replaced += 1
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
    print("下一步：chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选这个目录；")
    print("再在扩展 popup 里填入服务器 /opt/xcollect/collect-token.txt 的令牌。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
