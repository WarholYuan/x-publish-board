#!/usr/bin/env python3
"""渲染 X 发布板静态页 /var/www/x-publish-board/x/index.html。

数据源：飞书 Base「X 运营」发布库（经 lark-cli --as user 读写）。
- 状态=待发布/已上板 -> 待发布区；状态=已发布 -> 已发布区（按发布时间倒序，最多 30 条）；已复盘不进板。
- 无「板上编号」的记录生成 x- 前缀短编号（由 record_id 派生，稳定唯一）并回写，
  同时状态 待发布 -> 已上板；已上板的不重复改。
- 「图片」附件下载到 /var/www/x-publish-board/x/assets/<板上编号>/，文件名安全化，页面用相对路径引用。
- 页面嵌入状态服务令牌（/opt/xboard/state-token.txt，无则生成，权限 600）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")
TABLE_ID = os.environ.get("FEISHU_PUBLISH_TABLE_ID", "")
LARK = os.environ.get("LARK_CLI", "/opt/node24/bin/lark-cli")
OUT_DIR = Path(os.environ.get("XBOARD_OUT_DIR", "/var/www/x-publish-board/x"))
OUT_FILE = OUT_DIR / "index.html"
ASSETS_DIR = OUT_DIR / "assets"
TOKEN_FILE = Path(os.environ.get("XBOARD_TOKEN_FILE", "/opt/xboard/state-token.txt"))
SHANGHAI = ZoneInfo("Asia/Shanghai")

PENDING_STATUS = {"待发布", "已上板"}
# 发布预览里显示的 X 昵称（2026-10-26 在 X 个人页实测）；未配置的账号退回显示 handle
X_DISPLAY_NAMES = json.loads(os.environ.get("X_DISPLAY_NAMES_JSON", "{}"))  # e.g. {"@your_handle": "Your Name"}
PUBLISHED_MAX = 30
# 板子侧每条帖子最多 4 张图（X 单帖上限，用户定案）：只取飞书「图片」前 4 个附件，第 5 张起忽略；飞书源附件不动
MAX_IMAGES = 4

FIELD_NAMES = [
    "标题/首句", "链接点击", "1小时互动", "可复用点", "转发", "图片", "打法类型",
    "板上编号", "回复", "本帖涨粉", "内容类型", "发布时间", "互动率", "串/首条回复",
    "爆文判定", "帖子链接", "正文", "收入/线索", "发布时粉丝数", "收藏", "收藏率",
    "点赞", "账号", "复盘归因", "状态", "选题来源", "是否商单", "曝光", "主页访问",
    "计划发布时间",
]


def run_lark(*args: str, timeout: int = 120) -> dict:
    cmd = [LARK, "base", *args, "--as", "user", "--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def list_records() -> list[dict]:
    all_rows: list[tuple[str, list]] = []
    fields: list[str] | None = None
    offset = 0
    while True:
        out = run_lark("+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
                       "--limit", "200", "--offset", str(offset), "--format", "json")
        data = out["data"]
        fields = data["fields"]
        rows = data["data"]
        rids = data["record_id_list"]
        all_rows.extend(zip(rids, rows))
        if not data.get("has_more") or not rows:
            break
        offset += len(rows)
    records = []
    for rid, row in all_rows:
        rec = {"record_id": rid}
        for name, value in zip(fields, row):
            rec[name] = value
        records.append(rec)
    return records


def batch_update(updates: dict[str, dict]) -> None:
    body = json.dumps({"update_records": updates}, ensure_ascii=False)
    run_lark("+record-batch-update", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
             "--json", body)


def board_no_for(record_id: str) -> str:
    digest = hashlib.sha256(record_id.encode()).hexdigest()
    return "x-" + digest[:8]


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^\w.\-一-鿿]+", "_", name)
    return name[:120] or "image"


def ensure_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        # 富文本分段（[{"text": ...}, ...]）按原文直接拼接，不能插入换行，否则会改动正文排版
        if value and all(isinstance(v, dict) and "text" in v for v in value):
            return "".join(str(v["text"]) for v in value)
        return "\n".join(str(v) for v in value)
    return str(value)


def cell_option(value) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return ""


def ts_to_iso(value) -> str:
    """飞书 datetime 字段返回毫秒时间戳字符串/数字，转成 ISO；原样返回已是字符串的情况。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, SHANGHAI).isoformat()
    s = str(value)
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, SHANGHAI).isoformat()
    return s


def download_images(record_id: str, board_no: str, attachments: list) -> tuple[list[str], list[str]]:
    """下载前 MAX_IMAGES 个附件到 assets/<board_no>/，返回 (相对路径列表, 失败文件名列表)。"""
    target = ASSETS_DIR / board_no
    target.mkdir(parents=True, exist_ok=True)
    saved, failed = [], []
    attachments = [a for a in (attachments or []) if isinstance(a, dict)]
    if len(attachments) > MAX_IMAGES:
        print(f"图片超过 {MAX_IMAGES} 张只取前 {MAX_IMAGES} 张：{board_no}（{len(attachments)} → {MAX_IMAGES}）")
    attachments = attachments[:MAX_IMAGES]
    keep = {safe_filename(str(a.get("name", "image"))) for a in attachments}
    # 清掉本条 assets 目录里不在前 4 张之内的旧文件（只动板子侧副本，不碰飞书附件）
    for old in target.iterdir():
        if old.is_file() and old.name not in keep:
            old.unlink()
            print(f"清理多余图片副本 {board_no}/{old.name}")
    for att in attachments:
        name = safe_filename(str(att.get("name", "image")))
        dest = target / name
        rel = f"assets/{board_no}/{name}"
        if dest.exists() and dest.stat().st_size > 0:
            saved.append(rel)
            continue
        try:
            # lark-cli 只允许把附件落到 cwd/tmp，先落临时目录再挪进 assets
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_dest = Path(tmpdir) / name
                run_lark("+record-download-attachment", "--base-token", BASE_TOKEN,
                         "--table-id", TABLE_ID, "--record-id", record_id,
                         "--file-token", str(att.get("file_token", "")),
                         "--output", str(tmp_dest), "--overwrite")
                if tmp_dest.exists() and tmp_dest.stat().st_size > 0:
                    shutil.move(str(tmp_dest), str(dest))
                    os.chmod(dest, 0o644)
                    saved.append(rel)
                else:
                    failed.append(name)
        except Exception as exc:  # noqa: BLE001 — 单张失败不拖垮整次渲染
            print(f"WARN image download failed {record_id} {name}: {exc}", file=sys.stderr)
            failed.append(name)
    return saved, failed


def image_size(path: Path) -> list[int] | None:
    """读图片文件头取 [宽, 高]（PNG/GIF/WebP/JPEG），供预览按 X 拼图比例排版；读不出返回 None，前端加载后再校正。"""
    try:
        with open(path, "rb") as f:
            head = f.read(512 * 1024)
    except OSError:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return list(struct.unpack(">II", head[16:24]))
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return list(struct.unpack("<HH", head[6:10]))
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            kind = head[12:16]
            if kind == b"VP8X":
                return [int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return [w & 0x3FFF, h & 0x3FFF]
            if kind == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return [(bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1]
        if head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                    i += 1 if marker == 0xFF else 2
                    continue
                seg = struct.unpack(">H", head[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return [w, h]
                i += 2 + seg
    except struct.error:
        return None
    return None


def build_note(rec: dict, images: list[str]) -> dict:
    title = cell_text(rec.get("标题/首句"))
    body = cell_text(rec.get("正文"))
    return {
        "id": cell_text(rec.get("板上编号")) or board_no_for(rec["record_id"]),
        "recordId": rec["record_id"],
        "account": cell_option(rec.get("账号")),
        "type": cell_option(rec.get("内容类型")),
        "planTime": ts_to_iso(rec.get("计划发布时间")),
        "title": title,
        "body": body,
        "thread": cell_text(rec.get("串/首条回复")),
        "images": images,
        "imageSizes": [image_size(OUT_DIR / rel) for rel in images],
        "status": cell_option(rec.get("状态")),
        "publishedAt": ts_to_iso(rec.get("发布时间")),
        # 发帖页预填只给非长文（短帖/图帖/线程首条等）；长文无 intent 预填，前端走长文编辑器动线
        "postUrl": ("https://x.com/intent/tweet?text=" + urllib.parse.quote(body.strip(), safe=""))
                   if body.strip() and cell_option(rec.get("内容类型")) != "长文" else "",
    }


def render_html(notes: list[dict], accounts: list[dict], token: str, generated_at: str,
                failures: list[str]) -> str:
    notes_js = json.dumps(notes, ensure_ascii=False)
    accounts_js = json.dumps(accounts, ensure_ascii=False)
    failures_html = ""
    if failures:
        items = "".join(f"<li>{escape_html(f)}</li>" for f in failures)
        failures_html = f'<div class="render-warn"><strong>图片下载失败（不影响发布）：</strong><ul>{items}</ul></div>'
    # 内嵌进 <script> 的 JSON 转义 "</"，防正文里出现 </script> 截断页面；
    # 单遍替换占位符，防正文里恰好含 __TOKEN__ 之类字样被二次替换
    values = {
        "__NOTES__": notes_js.replace("</", "<\\/"),
        "__ACCOUNTS__": accounts_js.replace("</", "<\\/"),
        "__TOKEN__": token,
        "__GENERATED__": generated_at,
        "__FAILURES__": failures_html,
    }
    return re.sub("|".join(values), lambda m: values[m.group(0)], PAGE_TEMPLATE)


def escape_html(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    records = list_records()
    updates: dict[str, dict] = {}
    notes: list[dict] = []
    failures: list[str] = []
    used_nos: set[str] = set()

    for rec in records:
        status = cell_option(rec.get("状态"))
        if status == "已复盘":
            continue
        board_no = cell_text(rec.get("板上编号")).strip()
        if not board_no:
            board_no = board_no_for(rec["record_id"])
            if board_no in used_nos:
                board_no = "x-" + hashlib.sha256(rec["record_id"].encode()).hexdigest()[:12]
            fields = {"板上编号": board_no}
            if status == "待发布":
                fields["状态"] = ["已上板"]
                rec["状态"] = ["已上板"]
            updates[rec["record_id"]] = fields
        used_nos.add(board_no)
        if status not in PENDING_STATUS and cell_option(rec.get("状态")) not in PENDING_STATUS:
            pass
        images, failed = download_images(rec["record_id"], board_no, rec.get("图片") or [])
        failures.extend(f"{board_no}/{f}" for f in failed)
        notes.append(build_note(rec, images))

    if updates:
        batch_update(updates)
        print(f"回写飞书 {len(updates)} 条（板上编号/状态）")

    pending = [n for n in notes if n["status"] in PENDING_STATUS]
    published = sorted((n for n in notes if n["status"] == "已发布"),
                       key=lambda n: n["publishedAt"], reverse=True)[:PUBLISHED_MAX]
    board_notes = pending + published

    account_map: dict[str, dict] = {}
    palette = ["#1d9bf0", "#7856ff", "#00ba7c", "#f4212e", "#ff7a00", "#536471"]
    for idx, n in enumerate(board_notes):
        name = n["account"] or "未指定账号"
        if name not in account_map:
            account_map[name] = {"id": f"acc{len(account_map)}", "label": name,
                                 "displayName": X_DISPLAY_NAMES.get(name, ""),
                                 "color": palette[len(account_map) % len(palette)]}
        n["accountId"] = account_map[name]["id"]
    accounts = list(account_map.values())

    token = ensure_token()
    generated_at = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(board_notes, accounts, token, generated_at, failures)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_FILE.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(OUT_FILE, OUT_DIR / f"index.html.bak-{stamp}")
    tmp = Path(tempfile.mkstemp(dir=OUT_DIR, suffix=".tmp")[1])
    tmp.write_text(html, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT_FILE)
    print(f"板页已生成 {OUT_FILE}（待发布 {len(pending)}，已发布 {len(published)}，"
          f"图片下载失败 {len(failures)}）")
    return 0


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <meta name="theme-color" content="#ffffff">
  <meta name="format-detection" content="telephone=no">
  <title>X 发布台</title>
  <style>
    :root {
      --bg:#fff; --surface:#fff; --surface-soft:#f7f7f8;
      --ink:#171719; --muted:#74747a; --line:#e9e9ec;
      --accent:#1d9bf0; --accent-ink:#0f7acb; --accent-soft:#e8f5fe;
      --success:#00ba7c; --danger:#f4212e;
      --x-ink:#0f1419; --x-muted:#536471; --x-line:#eff3f4; --x-link:#1d9bf0;
      --shadow:0 12px 34px rgba(24,24,27,.08),0 2px 8px rgba(24,24,27,.05);
      --radius:18px;
      --safe-top:env(safe-area-inset-top,0px);
      --safe-bottom:env(safe-area-inset-bottom,0px);
    }
    * { box-sizing:border-box; }
    html { background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; -webkit-text-size-adjust:100%; text-size-adjust:100%; }
    html.lock, html.lock body { overflow:hidden; }
    body { margin:0; min-height:100vh; min-height:100dvh; background:var(--bg); }
    button,select,textarea { font:inherit; }
    button { color:inherit; -webkit-tap-highlight-color:transparent; }
    a { -webkit-tap-highlight-color:transparent; }
    [hidden] { display:none!important; }
    .shell { min-height:100vh; min-height:100dvh; max-width:1180px; margin:0 auto; }
    .app-header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; padding:36px 28px 26px; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:30px; line-height:1.18; letter-spacing:-.04em; }
    .app-subtitle { margin:7px 0 0; color:var(--muted); font-size:14px; line-height:1.5; }
    .save-state { display:flex; align-items:center; gap:7px; color:var(--muted); font-size:13px; white-space:nowrap; }
    .save-dot { width:8px; height:8px; border-radius:50%; background:#a4a4aa; box-shadow:0 0 0 3px #f0f0f1; }
    .inapp-tip { margin:12px 28px 0; padding:10px 14px; border-radius:12px; background:#fff7e6; color:#874d00; font-size:13px; line-height:1.55; }
    .workspace { display:grid; grid-template-columns:340px minmax(0,1fr); min-height:calc(100vh - 116px); }
    .accounts-panel { border-right:1px solid var(--line); background:#fff; }
    .panel-label { padding:22px 26px 12px; color:var(--muted); font-size:12px; font-weight:650; letter-spacing:.08em; }
    .account-list { display:flex; flex-direction:column; }
    .account-row { width:100%; display:grid; grid-template-columns:48px minmax(0,1fr) 20px; align-items:center; gap:14px; min-height:88px; padding:18px 24px; border:0; border-top:1px solid var(--line); background:#fff; text-align:left; cursor:pointer; transition:background .18s ease; }
    .account-row:last-child { border-bottom:1px solid var(--line); }
    .account-row:hover,.account-row:focus-visible { background:var(--surface-soft); outline:none; }
    .account-row.active { background:var(--accent-soft); box-shadow:inset 3px 0 0 var(--accent); }
    .avatar { position:relative; display:grid; place-items:center; width:48px; height:48px; border-radius:50%; color:#fff; background:var(--avatar,#1d9bf0); font-weight:750; font-size:17px; }
    .avatar::after { content:""; position:absolute; right:0; bottom:1px; width:10px; height:10px; border:2px solid #fff; border-radius:50%; background:var(--success); }
    .account-text { min-width:0; }
    .account-name { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:16px; font-weight:700; }
    .account-counts { display:flex; flex-wrap:wrap; gap:5px 10px; margin-top:7px; color:var(--muted); font-size:12px; }
    .account-counts span + span::before { content:""; display:inline-block; width:1px; height:10px; margin:0 10px 0 0; vertical-align:-1px; background:#d6d6da; }
    .chevron { width:18px; height:18px; color:#9a9aa0; }
    .detail-panel { min-width:0; background:#fafafa; }
    .detail-header { position:sticky; top:0; z-index:8; display:flex; align-items:center; gap:12px; min-height:68px; padding:12px 24px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.96); -webkit-backdrop-filter:saturate(180%) blur(18px); backdrop-filter:saturate(180%) blur(18px); }
    .back-button { display:none; place-items:center; flex:0 0 44px; width:44px; height:44px; margin-left:-6px; border:0; border-radius:50%; background:transparent; cursor:pointer; }
    .back-button:hover,.back-button:focus-visible { background:var(--surface-soft); outline:none; }
    .detail-title { min-width:0; font-size:18px; font-weight:750; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .detail-actions { margin-left:auto; display:flex; align-items:center; gap:8px; flex-shrink:0; }
    .detail-count { color:var(--muted); font-size:13px; }
    .refresh-button { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-height:36px; padding:0 12px; border:1px solid var(--line); border-radius:999px; background:#fff; color:var(--muted); font-size:13px; font-weight:600; cursor:pointer; transition:all .18s ease; flex-shrink:0; }
    .refresh-button:hover,.refresh-button:focus-visible { color:var(--ink); background:var(--surface-soft); border-color:#c6c6ca; outline:none; }
    .refresh-button:active { transform:scale(.96); }
    .status-tabs { display:grid; grid-template-columns:repeat(2,1fr); padding:0 24px; border-bottom:1px solid var(--line); background:#fff; }
    .status-tab { position:relative; height:52px; border:0; background:transparent; color:var(--muted); font-size:15px; cursor:pointer; }
    .status-tab.active { color:var(--accent); font-weight:700; }
    .status-tab.active::after { content:""; position:absolute; left:28%; right:28%; bottom:0; height:3px; border-radius:3px 3px 0 0; background:var(--accent); }
    .type-filter { padding:14px 24px 0; overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none; }
    .type-filter::-webkit-scrollbar { display:none; }
    .tabs { display:flex; gap:8px; width:max-content; }
    .tab { flex:0 0 auto; min-height:38px; border:1px solid var(--line); border-radius:999px; padding:0 15px; background:#fff; color:var(--muted); font-size:14px; cursor:pointer; }
    .tab.active { border-color:#222; background:#222; color:#fff; }
    .note-list { display:flex; flex-direction:column; gap:14px; padding:16px 24px 40px; }
    .note-card { min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:var(--radius); background:var(--surface); box-shadow:var(--shadow); }
    .note-main { padding:20px; }
    .note-meta { display:flex; align-items:center; gap:7px 9px; color:var(--muted); font-size:12px; flex-wrap:wrap; }
    .badge { background:var(--accent-soft); color:var(--accent-ink); border-radius:7px; padding:4px 7px; font-weight:700; }
    .badge.type { background:#eef4ff; color:#315f9a; }
    .badge.type[data-kind="article"] { background:#f1ecff; color:#5b3fc4; }
    .badge.type[data-kind="thread"] { background:#e9f8f1; color:#0b7a50; }
    .plan-chip { border-radius:7px; padding:4px 7px; background:#f0f0f1; color:#38383d; font-weight:700; }
    .preview-hit { display:block; cursor:pointer; }
    .note-title { margin:12px 0 0; font-size:19px; line-height:1.45; letter-spacing:-.02em; overflow-wrap:anywhere; word-break:break-word; }
    .internal-tag { display:inline-block; margin-left:6px; padding:2px 6px; border-radius:6px; background:#f0f0f1; color:var(--muted); font-size:11px; font-weight:600; letter-spacing:0; vertical-align:3px; }
    .body-text { margin-top:10px; color:#38383d; font-size:15px; line-height:1.8; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; }
    .thread-text { margin-top:12px; padding:10px 12px; border-left:3px solid var(--accent); background:var(--surface-soft); border-radius:0 10px 10px 0; color:#38383d; font-size:14px; line-height:1.7; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; }
    .thread-text b { display:block; margin-bottom:4px; color:var(--muted); font-size:12px; }
    .save-button { display:flex; align-items:center; justify-content:center; gap:5px; width:100%; min-height:40px; margin-top:6px; border:1px solid var(--line); border-radius:10px; background:#fff; color:var(--ink); font-size:13px; font-weight:650; cursor:pointer; }
    .save-all { margin-top:10px; min-height:44px; }
    .actions { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; margin-top:18px; }
    .actions .wide { grid-column:1/-1; }
    .action-button { display:flex; align-items:center; justify-content:center; gap:6px; min-width:0; min-height:48px; border:1px solid var(--line); border-radius:12px; padding:8px 10px; background:#fff; color:var(--ink); font-size:14px; font-weight:650; line-height:1.25; text-align:center; text-decoration:none; cursor:pointer; overflow-wrap:anywhere; }
    .action-button:hover,.action-button:focus-visible { border-color:#c6c6ca; background:var(--surface-soft); outline:none; }
    .action-button:active,.save-button:active { transform:scale(.98); }
    .action-button.primary { border-color:var(--x-ink); background:var(--x-ink); color:#fff; }
    .action-button.primary:hover,.action-button.primary:focus-visible { background:#272c30; }
    .action-button.xblue { border-color:var(--accent); background:var(--accent); color:#fff; }
    .action-button.xblue:hover,.action-button.xblue:focus-visible { background:#1a8cd8; }
    .action-button[disabled] { opacity:.45; cursor:not-allowed; }
    .xd-cell:only-child { flex:1 1 0; }
    /* 卡片（Round 4）：固定大小统一版式，所有卡片等高 */
    .note-card.fixed .note-meta { flex-wrap:nowrap; height:24px; overflow:hidden; white-space:nowrap; }
    .note-card.fixed .note-meta > * { flex:0 0 auto; }
    .note-card.fixed .note-meta .meta-acc { flex:0 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; }
    .note-card.fixed .note-title { height:52px; margin:12px 0 0; font-size:18px; line-height:26px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
    .note-card.fixed .note-text { height:125px; margin-top:8px; overflow:hidden; }
    .note-card.fixed .body-text { margin:0; font-size:15px; line-height:25px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:5; overflow:hidden; }
    .note-card.fixed .note-text.has-thread .body-text { -webkit-line-clamp:3; }
    .note-card.fixed .thread-text { margin-top:8px; padding:4px 10px; font-size:14px; line-height:25px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:1; overflow:hidden; }
    .note-card.fixed .thread-text b { display:inline; margin:0 6px 0 0; }
    .text-empty { color:var(--muted); }
    .media-zone { margin-top:14px; container-type:inline-size; }
    /* 图片格固定边长：min(96px, (容器宽-3 个间距)/4)，行高同值——空格子与图片格一样大，卡片等高 */
    .thumb-row { --cell:min(96px, calc((100cqi - 24px) / 4)); display:grid; grid-template-columns:repeat(4,var(--cell)); grid-auto-rows:var(--cell); gap:8px; }
    .thumb-cell { position:relative; display:grid; place-items:center; width:100%; height:100%; min-width:0; min-height:0; padding:0; border:0; border-radius:10px; overflow:hidden; background:#ececef; cursor:zoom-in; }
    .thumb-cell img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
    .thumb-cell.thumb-empty { border:1px dashed #d6d6da; background:#fafafa; color:#a4a4aa; font-size:12px; cursor:default; }
    .thumb-more { position:absolute; inset:0; display:grid; place-items:center; background:rgba(17,17,19,.5); color:#fff; font-size:18px; font-weight:700; }
    .note-card.fixed .save-all { height:44px; margin-top:10px; }
    .save-button[disabled] { color:#a4a4aa; cursor:default; }
    .fixed-actions { display:grid; grid-template-columns:1fr; gap:8px; margin-top:14px; }
    .act-row { display:grid; grid-template-columns:repeat(var(--cols),minmax(0,1fr)); gap:8px; }
    .fixed-actions .action-button { height:48px; min-height:48px; }
    .note-card.fixed .status-rail { height:66px; }
    .note-card.fixed .status-time { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    /* 逐张保存队列条（不支持多文件分享时） */
    .save-queue { position:fixed; left:50%; bottom:calc(16px + var(--safe-bottom)); z-index:95; display:flex; align-items:center; gap:8px; width:min(440px, calc(100% - 24px)); padding:10px 10px 10px 16px; border-radius:16px; background:#fff; box-shadow:0 12px 40px rgba(0,0,0,.18),0 0 0 1px rgba(0,0,0,.05); transform:translateX(-50%); font-size:13px; color:var(--muted); }
    .save-queue span { flex:1; min-width:0; }
    .save-queue button { flex:0 0 auto; min-height:44px; padding:0 14px; border-radius:12px; font-size:14px; font-weight:700; cursor:pointer; }
    .sq-next { border:0; background:var(--accent); color:#fff; }
    .sq-close { border:1px solid var(--line); background:#fff; color:var(--ink); }
    .status-rail { display:flex; align-items:center; gap:12px; min-height:66px; padding:12px 20px; border-top:1px solid var(--line); background:#fcfcfc; }
    .status-icon { display:grid; place-items:center; flex:0 0 30px; width:30px; height:30px; border-radius:50%; color:#fff; background:#a4a4aa; }
    .note-card[data-status="published"] .status-icon { background:var(--success); }
    .status-copy { min-width:0; }
    .status-name { display:block; font-size:13px; font-weight:700; }
    .status-time { display:block; margin-top:2px; color:var(--muted); font-size:11px; }
    .publish-button { margin-left:auto; flex-shrink:0; min-height:44px; border:0; border-radius:12px; padding:0 18px; background:var(--accent); color:#fff; font-size:15px; font-weight:700; cursor:pointer; }
    .undo-button { margin-left:auto; flex-shrink:0; min-height:40px; border:1px solid var(--line); border-radius:12px; padding:0 14px; background:#fff; color:var(--muted); font-size:13px; font-weight:600; cursor:pointer; }
    .empty { padding:72px 24px; color:var(--muted); text-align:center; }
    .empty strong { display:block; margin-bottom:8px; color:var(--ink); font-size:17px; }
    .render-warn { margin:14px 24px 0; padding:12px 16px; border:1px solid #ffd591; border-radius:12px; background:#fffbe6; color:#874d00; font-size:13px; overflow-wrap:anywhere; }
    .render-warn ul { margin:6px 0 0; padding-left:18px; }
    .toast { position:fixed; left:50%; bottom:calc(24px + var(--safe-bottom)); z-index:100; max-width:calc(100% - 32px); transform:translate(-50%,20px); border-radius:16px; padding:11px 17px; background:rgba(20,20,22,.94); color:#fff; font-size:14px; line-height:1.45; text-align:center; opacity:0; pointer-events:none; transition:opacity .2s ease,transform .2s ease; }
    .toast.show { opacity:1; transform:translate(-50%,0); }
    html.preview-open .toast { top:calc(12px + var(--safe-top)); bottom:auto; transform:translate(-50%,-20px); }
    html.preview-open .toast.show { transform:translate(-50%,0); }
    /* 发完了确认 */
    .modal-backdrop { position:fixed; inset:0; z-index:80; display:none; align-items:flex-end; justify-content:center; padding:20px 16px calc(20px + var(--safe-bottom)); background:rgba(17,17,19,.36); }
    .modal-backdrop.show { display:flex; }
    .modal { width:min(440px,100%); border-radius:20px; padding:22px; background:#fff; box-shadow:0 24px 70px rgba(0,0,0,.18); }
    .modal h2 { margin:0; font-size:19px; }
    .modal p { margin:9px 0 20px; color:var(--muted); font-size:14px; line-height:1.6; }
    .modal-actions { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .modal-actions button { min-height:48px; border-radius:12px; font-size:15px; font-weight:700; cursor:pointer; }
    .cancel-button { border:1px solid var(--line); background:#fff; }
    .confirm-button { border:0; background:var(--accent); color:#fff; }
    /* 发布预览弹层：短帖/线程=X 手机版帖子详情页；长文=X 手机版 Article 页（均以用户提供的真机截图为基准） */
    .sheet-backdrop { position:fixed; inset:0; z-index:50; display:flex; align-items:center; justify-content:center; padding:24px; background:rgba(15,20,25,.45); }
    .sheet { position:relative; display:flex; flex-direction:column; width:min(600px,100%); max-height:min(92vh,980px); border-radius:20px; background:#fff; box-shadow:0 24px 80px rgba(0,0,0,.22); overflow:hidden; }
    .icon-button { display:grid; place-items:center; flex:0 0 44px; width:44px; height:44px; border:0; border-radius:50%; background:transparent; color:var(--x-ink); cursor:pointer; }
    .icon-button:hover,.icon-button:focus-visible { background:#eff3f4; outline:none; }
    .sheet-body { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain; padding-bottom:var(--safe-bottom); }
    .xd-top { display:grid; grid-template-columns:52px minmax(0,1fr) 52px; align-items:center; min-height:53px; padding:0 4px; background:#fff; color:#0f1419; }
    .xd-top h2 { margin:0; text-align:center; font-size:17px; line-height:1.2; font-weight:700; }
    .xd-top-more { display:grid; place-items:center; height:44px; color:#0f1419; }
    .xd-top-more svg { width:22px; height:22px; }
    .sheet.article .xd-top { display:none; }
    .x-frame { background:#fff; color:#0f1419; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif; }
    .xd { padding:12px 10px 0; }
    .xd + .xd { border-top:1px solid var(--x-line); padding-top:14px; }
    .xd-author { display:flex; align-items:center; gap:10px; min-width:0; }
    .tw-avatar { display:grid; place-items:center; border-radius:50%; background:var(--avatar,#1d9bf0); color:#fff; font-weight:750; }
    .xd-avatar { flex:0 0 44px; width:44px; height:44px; font-size:18px; }
    .xd-names { flex:1; min-width:0; }
    .xd-name { display:flex; align-items:center; gap:3px; min-width:0; font-size:16px; line-height:20px; font-weight:700; }
    .xd-name span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tw-verified { flex:0 0 auto; width:18px; height:18px; color:var(--x-link); }
    .xd-handle { margin-top:1px; overflow:hidden; color:var(--x-muted); font-size:15px; line-height:20px; text-overflow:ellipsis; white-space:nowrap; }
    .xd-brand { flex:0 0 auto; margin-left:8px; color:#0f1419; font-size:21px; font-weight:800; letter-spacing:-.02em; }
    .xd .xd-name { font-size:17px; line-height:22px; }
    .xd .xd-handle { font-size:16px; }
    .xd-text { margin-top:25px; font-size:17px; line-height:24px; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; }
    .xd-text a, .xa-body a { color:var(--x-link); text-decoration:none; }
    .tw-empty { color:var(--danger); font-size:15px; }
    .xd-media { display:flex; flex-direction:column; gap:5px; margin-top:19px; }
    .xd-row { display:flex; gap:5px; }
    .xd-cell { flex:var(--r,1) 1 0; min-width:0; aspect-ratio:var(--r,1); padding:0; border:1px solid rgba(207,217,222,.7); border-radius:10px; overflow:hidden; background:#f7f9f9; }
    .xd-cell img { display:block; width:100%; height:100%; object-fit:cover; }
    .xd-meta { margin-top:23px; color:var(--x-muted); font-size:15px; line-height:20px; }
    .xd-meta b { color:#0f1419; font-weight:700; }
    .xd-actions { display:flex; align-items:center; justify-content:space-between; padding:14px 0; color:var(--x-muted); }
    .xd-actions svg { display:block; width:22px; height:22px; }
    .xa-hero { position:relative; }
    .xa-cover { display:block; width:100%; padding:0; border:0; aspect-ratio:5/2; background:linear-gradient(135deg,#e8f5fe,#f1ecff); overflow:hidden; }
    .xa-cover img { display:block; width:100%; height:100%; object-fit:cover; }
    .xa-cover.none { aspect-ratio:auto; height:56px; background:#fff; }
    .xa-back { position:absolute; top:10px; left:12px; display:grid; place-items:center; width:36px; height:36px; border:0; border-radius:50%; background:rgba(15,20,25,.72); color:#fff; cursor:pointer; }
    .xa-back svg { width:20px; height:20px; }
    .xa-title { margin:18px 16px 0; font-size:24px; line-height:1.3; font-weight:800; letter-spacing:-.01em; overflow-wrap:anywhere; word-break:break-word; }
    .xa-title.empty { color:var(--danger); font-size:18px; }
    .xa-author { display:flex; align-items:center; gap:10px; min-width:0; margin:16px 16px 0; }
    .xa-author .xd-avatar { flex-basis:40px; width:40px; height:40px; font-size:16px; }
    .xa-follow { display:inline-flex; align-items:center; flex:0 0 auto; margin-left:auto; min-height:32px; padding:0 16px; border-radius:999px; background:#0f1419; color:#fff; font-size:15px; font-weight:700; }
    .xa-body { margin:18px 16px 0; font-size:17px; line-height:1.55; overflow-wrap:anywhere; word-break:break-word; }
    .xa-body p { margin:0 0 26px; white-space:pre-wrap; }
    .xa-body strong { font-weight:700; }
    .xa-img { display:block; width:100%; margin:0 0 26px; border-radius:16px; overflow:hidden; background:#f7f9f9; }
    .xa-img img { display:block; width:100%; height:auto; }
    .xa-pill { position:sticky; bottom:calc(12px + var(--safe-bottom)); z-index:2; display:flex; align-items:center; justify-content:space-between; margin:18px 16px 12px; padding:12px 20px; border-radius:999px; background:#fff; box-shadow:0 2px 14px rgba(0,0,0,.14),0 0 0 1px rgba(0,0,0,.04); color:var(--x-muted); font-size:15px; }
    .xa-pill span { display:inline-flex; align-items:center; gap:6px; }
    .xa-pill svg { width:21px; height:21px; }
    /* 大图（兜底：长按存储到照片） */
    .lightbox { position:fixed; inset:0; z-index:70; display:flex; flex-direction:column; background:rgba(0,0,0,.94); color:#fff; }
    .lb-top { display:flex; align-items:center; gap:8px; padding:calc(8px + var(--safe-top)) 10px 8px 16px; font-size:14px; }
    .lb-top .icon-button { margin-left:auto; color:#fff; background:rgba(255,255,255,.12); }
    .lb-stage { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; padding:8px; }
    .lb-stage img { max-width:100%; max-height:100%; object-fit:contain; -webkit-touch-callout:default; -webkit-user-select:auto; user-select:auto; }
    .lb-bottom { padding:10px 16px calc(14px + var(--safe-bottom)); text-align:center; }
    .lb-hint { margin:0 auto 10px; max-width:420px; padding:10px 14px; border-radius:12px; background:rgba(255,255,255,.14); font-size:15px; font-weight:600; line-height:1.5; }
    .lb-actions { display:flex; justify-content:center; gap:10px; flex-wrap:wrap; }
    .lb-actions a,.lb-actions button { display:inline-flex; align-items:center; justify-content:center; min-height:44px; min-width:88px; padding:0 16px; border:1px solid rgba(255,255,255,.3); border-radius:12px; background:transparent; color:#fff; font-size:14px; font-weight:600; text-decoration:none; cursor:pointer; }
    /* 复制兜底：手动拷贝 */
    .manual-copy { position:fixed; inset:0; z-index:90; display:flex; align-items:flex-end; justify-content:center; padding:16px 16px calc(16px + var(--safe-bottom)); background:rgba(17,17,19,.4); }
    .manual-copy .modal { width:min(520px,100%); }
    .manual-copy textarea { width:100%; height:160px; margin:4px 0 14px; padding:10px 12px; border:1px solid var(--line); border-radius:12px; font-size:16px; line-height:1.5; resize:vertical; }
    @media (max-width:760px) {
      .shell { max-width:none; }
      .app-header { align-items:center; gap:12px; padding:calc(18px + var(--safe-top)) 16px 16px; }
      h1 { font-size:24px; }
      .app-subtitle { font-size:13px; }
      .save-state span:last-child { display:none; }
      .inapp-tip { margin:12px 16px 0; }
      .workspace { display:block; min-height:auto; }
      .accounts-panel { border-right:0; }
      .panel-label { padding:18px 16px 10px; }
      .account-row { padding:16px; }
      .detail-panel { position:fixed; inset:0; z-index:20; display:none; overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain; background:#fafafa; }
      .detail-panel.open { display:block; }
      .detail-header { gap:8px; min-height:56px; padding:calc(6px + var(--safe-top)) 12px 6px; }
      .back-button { display:grid; }
      .detail-count { display:none; }
      .status-tabs { padding:0 8px; }
      .type-filter { padding:12px 12px 0; }
      .render-warn { margin:12px 12px 0; }
      .note-list { gap:12px; padding:12px 12px calc(32px + var(--safe-bottom)); }
      .note-main { padding:16px; }
      .note-title { font-size:18px; }
      .actions { gap:8px; }
      .action-button { min-height:48px; padding:6px 8px; font-size:15px; }
      .status-rail { padding:10px 16px; }
      .sheet-backdrop { align-items:stretch; padding:0; }
      .sheet { width:100%; max-height:none; height:100%; border-radius:0; padding-top:var(--safe-top); box-shadow:none; }
    }
    @media (max-width:360px) {
      .xa-pill { padding:12px 14px; font-size:14px; }
    }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto!important; transition:none!important; } }
  </style>
</head>
<body>
  <div class="shell">
    <header class="app-header">
      <div>
        <h1>X 发布台</h1>
        <p class="app-subtitle">先预览，再复制去 X 发布 · 生成于 __GENERATED__</p>
      </div>
      <div class="save-state"><i class="save-dot"></i><span>状态实时回写飞书</span></div>
    </header>
    <div class="inapp-tip" id="inappTip" hidden>你正在 App 内置浏览器里打开：跳转 X、保存图片可能受限。建议点右上角「…」→「在 Safari 中打开」。</div>
    <div class="workspace">
      <aside class="accounts-panel" aria-label="账号列表">
        <div class="panel-label">发布账号</div>
        <div class="account-list" id="accountList"></div>
      </aside>
      <main class="detail-panel" id="detailPanel">
        <header class="detail-header">
          <button class="back-button" id="backButton" type="button" aria-label="返回账号列表"></button>
          <div class="detail-title" id="detailTitle"></div>
          <div class="detail-actions">
            <div class="detail-count" id="detailCount"></div>
            <button class="refresh-button" id="refreshButton" type="button" aria-label="刷新" title="刷新">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
              <span>刷新</span>
            </button>
          </div>
        </header>
        <nav class="status-tabs" aria-label="发布状态">
          <button class="status-tab active" data-status-filter="pending" type="button">待发布 <span>0</span></button>
          <button class="status-tab" data-status-filter="published" type="button">已发布 <span>0</span></button>
        </nav>
        <div class="type-filter">
          <div class="tabs" id="tabs">
            <button class="tab active" data-filter="all" type="button">全部 0</button>
          </div>
        </div>
        __FAILURES__
        <section class="note-list" id="noteList" aria-live="polite"></section>
      </main>
    </div>
  </div>

  <div class="sheet-backdrop" id="previewModal" role="dialog" aria-modal="true" aria-label="发布预览" hidden>
    <div class="sheet" id="previewSheet">
      <header class="xd-top">
        <button class="icon-button" id="previewClose" data-close-preview type="button" aria-label="关闭预览"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg></button>
        <h2 id="previewTitle">帖子</h2>
        <span class="xd-top-more" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></span>
      </header>
      <div class="sheet-body" id="previewBody"></div>
    </div>
  </div>

  <div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="查看原图" hidden>
    <div class="lb-top"><span id="lbCount"></span><button class="icon-button" id="lbClose" type="button" aria-label="关闭大图"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <div class="lb-stage"><img id="lbImg" alt=""></div>
    <div class="lb-bottom">
      <p class="lb-hint" id="lbHint">长按图片 → 选「存储到照片」</p>
      <div class="lb-actions">
        <button id="lbPrev" type="button">上一张</button>
        <button id="lbSave" type="button">存到相册</button>
        <a id="lbDownload" href="#" download>下载原图</a>
        <button id="lbNext" type="button">下一张</button>
      </div>
    </div>
  </div>

  <div class="manual-copy" id="manualCopy" role="dialog" aria-modal="true" aria-labelledby="manualTitle" hidden>
    <div class="modal">
      <h2 id="manualTitle">自动复制没成功</h2>
      <p>文字已全选，长按选中区域 →「拷贝」即可。</p>
      <textarea id="manualText" readonly></textarea>
      <div class="modal-actions" style="grid-template-columns:1fr"><button class="cancel-button" id="manualClose" type="button">关闭</button></div>
    </div>
  </div>

  <div class="save-queue" id="saveQueue" role="status" hidden><span id="sqText"></span><button class="sq-next" id="sqNext" type="button">保存下一张</button><button class="sq-close" id="sqClose" type="button">结束</button></div>
  <div class="toast" id="toast" role="status">已复制</div>
  <div class="modal-backdrop" id="publishModal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <div class="modal">
      <h2 id="modalTitle">这条发完了吗？</h2>
      <p>确认后这条帖子会移到“已发布”，飞书发布库同步更新。</p>
      <div class="modal-actions">
        <button class="cancel-button" id="cancelPublish" type="button">还没发完</button>
        <button class="confirm-button" id="confirmPublish" type="button">发完了</button>
      </div>
    </div>
  </div>
  <script>
    const BOARD_TOKEN = "__TOKEN__";
    const notes = __NOTES__;
    const accounts = __ACCOUNTS__;
    const state = { selectedAccount:accounts.length?accounts[0].id:"", statusFilter:"pending", typeFilter:"all", pendingPublish:null, previewId:null, lightbox:null, lastFocus:null };
    const $ = (selector, root=document) => root.querySelector(selector);
    const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
    const UA = navigator.userAgent || "";
    const IS_IOS = /iPhone|iPad|iPod/.test(UA) || (/Macintosh/.test(UA) && navigator.maxTouchPoints > 1);
    const IS_TOUCH = IS_IOS || /Android/i.test(UA) || (window.matchMedia && matchMedia("(pointer:coarse)").matches);
    const IN_APP = /Lark|Feishu|MicroMessenger|DingTalk|WeCom|wxwork|QQ\/|Weibo|FBAN|FBAV|Instagram|Line\//i.test(UA);
    const ARTICLE_EDITOR = "https://x.com/compose/articles";
    const icons = {
      back:'<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
      next:'<svg viewBox="0 0 24 24" class="chevron" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
      check:'<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
      verified:'<svg class="tw-verified" viewBox="0 0 24 24" aria-label="认证账号" role="img"><path fill="currentColor" d="M12 1.6l2.4 1.8 3-.1.9 2.9 2.4 1.8-1 2.8 1 2.8-2.4 1.8-.9 2.9-3-.1L12 22.4l-2.4-1.8-3 .1-.9-2.9-2.4-1.8 1-2.8-1-2.8 2.4-1.8.9-2.9 3 .1z"/><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m8.2 12.2 2.6 2.6 5-5.4"/></svg>',
      reply:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M4 11.5a7.5 7.5 0 0 1 7.5-7.5h1A7.5 7.5 0 0 1 20 11.5c0 3-1.8 5.5-4.4 6.7L9 21.5v-2.6h-.2A4.8 4.8 0 0 1 4 14z"/></svg>',
      repost:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4 4 7l3 3"/><path d="M4 7h11a4 4 0 0 1 4 4v1"/><path d="m17 20 3-3-3-3"/><path d="M20 17H9a4 4 0 0 1-4-4v-1"/></svg>',
      like:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M12 20s-7.5-4.4-7.5-10A4.2 4.2 0 0 1 12 7.6 4.2 4.2 0 0 1 19.5 10c0 5.6-7.5 10-7.5 10z"/></svg>',
      views:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M5 20V12M10 20V5M15 20v-9M20 20v-5"/></svg>',
      bookmark:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5h12v17l-6-4-6 4z"/></svg>',
      share:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7.5 7.5 12 3l4.5 4.5"/><path d="M4.5 14v5.5h15V14"/></svg>',
      save:'<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7.5 10.5 12 15l4.5-4.5"/><path d="M5 19h14"/></svg>'
    };
    function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
    function accountLabel(id) { return accounts.find(a=>a.id===id)?.label || "账号"; }
    function accountColor(id) { return accounts.find(a=>a.id===id)?.color || "#1d9bf0"; }
    function formatTime(value) { if (!value) return ""; const d=new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(d); }
    function planLabel(value) { const m=/^\d{4}-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(value||""); return m?`计划 ${m[1]}-${m[2]} ${m[3]}`:(value?`计划 ${String(value).replace("T"," ").slice(5,16)}`:""); }
    function byPlanTime(a,b) { const x=a.planTime||"", y=b.planTime||""; if(x===y) return 0; if(!x) return 1; if(!y) return -1; return x<y?-1:1; }
    function noteStatus(note) { return note.status==="已发布" ? "published" : "pending"; }
    function findNote(id) { return notes.find(n=>n.id===id); }
    function storageGet(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
    function storageSet(key, value) { try { value==null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch {} }

    // ---- 内容类型口径：长文=标题+正文；线程=正文+串；其余（短帖/图帖/视频/转帖+观点）=只发正文 ----
    function kindOf(note) { if (note.type==="长文") return "article"; if (note.type==="线程") return "thread"; return "post"; }
    // 发布保真：只去掉首尾空白，正文中间的换行、空行、空格一律原样保留
    function clean(text) { return String(text||"").replace(/^\s+|\s+$/g,""); }
    function copyPayload(note, kind) {
      if (kind==="title") return clean(note.title);
      if (kind==="body") return clean(note.body);
      if (kind==="thread") return clean(note.thread);
      if (kind==="all") return [clean(note.body), clean(note.thread)].filter(Boolean).join("\n");
      return "";
    }
    const copyLabels = { title:"标题", body:"正文", thread:"串/首条回复", all:"整串" };
    function intentUrl(text) { return "https://x.com/intent/tweet?text=" + encodeURIComponent(text); }
    function handleOf(note) { const raw=(note.account||"").trim(); if (!raw) return {name:"未指定账号", handle:"@account"}; const h=raw.startsWith("@")?raw:"@"+raw; const display=accounts.find(a=>a.id===note.accountId)?.displayName; return {name:display||h.slice(1), handle:h}; }
    // 按 X 帖子详情页的实际渲染规则（2026-10 在 x.com 实测 DOM）：
    // 链接隐藏 https:// 与 www.、超长截断加「…」；@提及、#话题（含中文）、$cashtag 着 X 蓝；其余文字（含换行/空行）原样
    const X_TLDS="com|net|org|io|ai|co|cn|me|dev|app|xyz|so|gg|tv|info|biz|us|uk|jp|hk|tw|sg|de|fr|ly|to|fm|im|sh|be|link|site|online|top|vip|club|shop|store|tech|cc|art|blog|news|page|pro|one|world|live|life|fun|wiki|edu|gov";
    const X_TOKEN_RE=new RegExp(String.raw`(https?:\/\/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)|((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:${X_TLDS})(?![A-Za-z0-9\-])(?:\/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*)?)|([@＠][A-Za-z0-9_]{1,15})|([#＃][\p{L}\p{M}\p{N}_]+)|(\$[A-Za-z]{1,6}(?:[._][A-Za-z]{1,2})?)`, "giu");
    function xDisplayUrl(url) { const d=url.replace(/^https?:\/\//i,"").replace(/^www\./i,""); return d.length>26?d.slice(0,25)+"…":d; }
    function linkify(text, inert) {
      let out="", last=0, m; X_TOKEN_RE.lastIndex=0;
      while ((m=X_TOKEN_RE.exec(text))) {
        let token=m[0]; const prev=m.index>0?text[m.index-1]:"";
        const glued=/[A-Za-z0-9_]/.test(prev);
        if (m[1]||m[2]) {
          // URL 末尾的标点不算链接（与 X 一致）；左括号不配对时去掉右括号
          let t=token.replace(/[.,:;!?'"]+$/,""); while (t.endsWith(")") && (t.match(/\(/g)||[]).length<(t.match(/\)/g)||[]).length) t=t.slice(0,-1).replace(/[.,:;!?'"]+$/,"");
          if (m[2] && (glued || prev==="@" || prev===".")) { X_TOKEN_RE.lastIndex=m.index+1; continue; }
          token=t;
        } else if (m[3]) { if (glued) { X_TOKEN_RE.lastIndex=m.index+token.length; continue; } }
        else if (m[4]) { if (glued || prev==="&" || /^[#＃][\p{N}_]+$/u.test(token)) { X_TOKEN_RE.lastIndex=m.index+token.length; continue; } }
        else if (m[5]) { if (glued || /[A-Za-z0-9]/.test(text[m.index+token.length]||"")) { X_TOKEN_RE.lastIndex=m.index+token.length; continue; } }
        out += escapeHtml(text.slice(last, m.index));
        const isUrl=!!(m[1]||m[2]); const label=escapeHtml(isUrl?xDisplayUrl(token):token);
        if (inert) out += `<span class="lk">${label}</span>`;
        else if (isUrl) out += `<a href="${escapeHtml(m[1]?token:"https://"+token)}" target="_blank" rel="noopener">${label}</a>`;
        else out += `<a href="#" tabindex="-1" data-noop>${label}</a>`;
        last = m.index + token.length; X_TOKEN_RE.lastIndex=last;
      }
      return out + escapeHtml(text.slice(last));
    }

    // ---- 复制：全部在点击手势的同一个同步调用栈内完成（iOS 关键） ----
    function copyViaExecCommand(text) {
      let fired=false;
      const onCopy=event=>{ if(!event.clipboardData) return; event.clipboardData.setData("text/plain", text); event.preventDefault(); fired=true; };
      const active=document.activeElement;
      const ta=document.createElement("textarea");
      ta.value=text; ta.setAttribute("readonly",""); ta.setAttribute("aria-hidden","true"); ta.tabIndex=-1;
      // iOS：元素必须在视口内且可选中；16px 防止聚焦时页面放大；readonly 防止弹键盘
      ta.style.cssText="position:fixed;top:0;left:0;width:2em;height:2em;margin:0;padding:0;border:0;outline:0;box-shadow:none;background:transparent;color:transparent;opacity:.01;font-size:16px;z-index:-1;-webkit-user-select:text;user-select:text;";
      document.body.appendChild(ta);
      document.addEventListener("copy", onCopy, true);
      let ok=false;
      try {
        ta.focus({preventScroll:true});
        ta.select();
        ta.setSelectionRange(0, text.length); // iOS 上 select() 不建立选区，必须显式设范围
        ok=document.execCommand("copy");
      } catch { ok=false; }
      finally {
        document.removeEventListener("copy", onCopy, true);
        ta.remove();
        try { window.getSelection()?.removeAllRanges(); } catch {}
        if (active && active!==document.body && active.isConnected && active.focus) { try { active.focus({preventScroll:true}); } catch {} }
      }
      return ok || fired;
    }
    function copyText(text) {
      text=clean(text);
      if (!text) return Promise.resolve(false);
      if (copyViaExecCommand(text)) return Promise.resolve(true);
      // 兜底 Async Clipboard：仍在同一同步栈里发起（不先 await 任何东西），手势不丢
      if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
        try { return navigator.clipboard.writeText(text).then(()=>true, ()=>false); } catch {}
      }
      return Promise.resolve(false);
    }
    function showManualCopy(text) {
      const box=$("#manualCopy"), ta=$("#manualText");
      ta.value=clean(text); box.hidden=false; lockScroll();
      ta.focus({preventScroll:true}); ta.setSelectionRange(0, ta.value.length);
    }
    function doCopy(note, kind) {
      const text=copyPayload(note, kind);
      if (!text) { showToast(`这条没有${copyLabels[kind]||"内容"}可复制`); return Promise.resolve(false); }
      return copyText(text).then(ok=>{ if (ok) showToast(`已复制${copyLabels[kind]||""}`); else showManualCopy(text); return ok; });
    }

    // ---- 图片保存：优先 Web Share（iOS 分享面板「存储图像」进相册），兜底大图+长按提示 ----
    const fileReady=new Map(), filePending=new Map();
    function mimeOf(src) { const ext=(src.split(".").pop()||"").toLowerCase(); return ({png:"image/png",jpg:"image/jpeg",jpeg:"image/jpeg",gif:"image/gif",webp:"image/webp",heic:"image/heic"})[ext] || "image/png"; }
    function loadFile(src) {
      if (fileReady.has(src)) return Promise.resolve(fileReady.get(src));
      if (filePending.has(src)) return filePending.get(src);
      const p=fetch(src,{credentials:"same-origin"}).then(r=>{ if(!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob(); })
        .then(blob=>{ const file=new File([blob], decodeURIComponent(src.split("/").pop()||"image.png"), {type:blob.type&&blob.type.startsWith("image/")?blob.type:mimeOf(src)}); fileReady.set(src,file); return file; })
        .finally(()=>filePending.delete(src));
      filePending.set(src,p); return p;
    }
    function prefetchFiles(srcs) { if (!navigator.share) return; srcs.forEach(src=>{ loadFile(src).catch(()=>{}); }); }
    function canShareFiles(files) { try { return !!(navigator.share && navigator.canShare && navigator.canShare({files})); } catch { return false; } }
    function shareFiles(files, srcs, late) {
      if (!canShareFiles(files)) { openLightbox(srcs, 0, "fallback"); return; }
      navigator.share({files}).then(()=>showToast(files.length>1?`已处理 ${files.length} 张图片`:"已处理图片"), err=>{
        if (err && err.name==="AbortError") return; // 用户取消分享面板
        if (err && err.name==="NotAllowedError" && late) { showToast("图片已准备好，请再点一次「保存」"); return; }
        openLightbox(srcs, 0, "fallback");
      });
    }
    function saveImages(srcs) {
      if (!srcs.length) return;
      if (!navigator.share || !navigator.canShare) { openLightbox(srcs, 0, "fallback"); return; }
      const ready=srcs.map(s=>fileReady.get(s));
      if (ready.every(Boolean)) { shareFiles(ready, srcs, false); return; } // 已预取：同步调起分享，手势有效
      showToast("正在准备图片…");
      Promise.all(srcs.map(loadFile)).then(files=>shareFiles(files, srcs, true), ()=>openLightbox(srcs, 0, "fallback"));
    }
    // 保存全部图片：优先一次多文件分享（iOS 分享面板「存储 N 张图像」一次存完）；
    // 不支持多文件分享 → 逐张分享（每张需一次点击，Safari 规定分享必须在点击手势内）；完全不支持分享 → 电脑逐张下载 / iOS 大图逐张长按
    function saveAllImages(srcs) {
      if (!srcs.length) return;
      if (srcs.length===1) { saveImages(srcs); return; }
      if (!navigator.share || !navigator.canShare) { saveOneByOne(srcs); return; }
      const go=(files, late)=>{
        if (canShareFiles(files)) {
          navigator.share({files}).then(()=>showToast(`已处理 ${files.length} 张图片`), err=>{
            if (err && err.name==="AbortError") return;
            if (err && err.name==="NotAllowedError" && late) { showToast("图片已准备好，请再点一次「保存全部图片」"); return; }
            startQueue(srcs, files, true);
          });
        } else if (canShareFiles([files[0]])) startQueue(srcs, files, late);
        else saveOneByOne(srcs);
      };
      const ready=srcs.map(s=>fileReady.get(s));
      if (ready.every(Boolean)) { go(ready, false); return; } // 已预取：同步调起分享，手势有效
      showToast("正在准备图片…");
      Promise.all(srcs.map(loadFile)).then(files=>go(files, true), ()=>saveOneByOne(srcs));
    }
    function startQueue(srcs, files, late) {
      state.queue={srcs, files, i:0};
      if (late) showQueue(`图片已准备好（共 ${files.length} 张）`); else shareQueueNext();
    }
    function showQueue(text) {
      const q=state.queue; if (!q) return;
      $("#sqText").textContent=text; $("#sqNext").textContent=`保存第 ${q.i+1} / ${q.files.length} 张`; $("#saveQueue").hidden=false;
    }
    function endQueue() { state.queue=null; $("#saveQueue").hidden=true; }
    function shareQueueNext() {
      const q=state.queue; if (!q) return;
      const f=q.files[q.i];
      navigator.share({files:[f]}).then(()=>{
        q.i++;
        if (q.i>=q.files.length) { endQueue(); showToast(`${q.files.length} 张图片已逐张处理完`); }
        else showQueue(`已处理 ${q.i} / ${q.files.length} 张`);
      }, err=>{
        if (err && err.name==="AbortError") { showQueue(`第 ${q.i+1} 张已取消，可继续`); return; }
        showQueue(`第 ${q.i+1} 张没保存成功，请再点一次`);
      });
    }
    function saveOneByOne(srcs) {
      if (IS_IOS || IS_TOUCH) { openLightbox(srcs, 0, "fallback"); return; } // 手机无分享：大图里逐张长按存储
      srcs.forEach((src,i)=>setTimeout(()=>{ const a=document.createElement("a"); a.href=src; a.download=decodeURIComponent(src.split("/").pop()||`image-${i+1}.png`); a.style.display="none"; document.body.appendChild(a); a.click(); a.remove(); }, i*400));
      showToast(`已开始逐张下载 ${srcs.length} 张图片`);
    }
    function openLightbox(srcs, index, mode) {
      state.lightbox={srcs, index:index||0, mode:mode||"view"}; prefetchFiles(srcs);
      $("#lightbox").hidden=false; lockScroll(); updateLightbox(); $("#lbClose").focus({preventScroll:true});
    }
    function updateLightbox() {
      const lb=state.lightbox; if(!lb) return;
      const src=lb.srcs[lb.index];
      $("#lbImg").src=src; $("#lbImg").alt=`图片 ${lb.index+1}`;
      $("#lbCount").textContent=lb.srcs.length>1?`${lb.index+1} / ${lb.srcs.length}`:"";
      $("#lbDownload").href=src; $("#lbDownload").setAttribute("download", decodeURIComponent(src.split("/").pop()||"image.png"));
      $("#lbPrev").hidden=$("#lbNext").hidden=lb.srcs.length<2;
      $("#lbHint").textContent = IS_TOUCH
        ? (lb.mode==="fallback" ? (lb.srcs.length>1 ? `当前环境不能一次存全部：长按图片 → 选「存储到照片」，再点「下一张」（共 ${lb.srcs.length} 张）` : "当前环境不能直接存相册：长按图片 → 选「存储到照片」") : "长按图片 → 选「存储到照片」")
        : "电脑上：点「下载原图」，或右键图片另存为";
      $("#lbDownload").hidden = IS_IOS; // iOS 的 download 只会进「文件」不进相册，避免误导
      $("#lbSave").hidden = lb.mode==="fallback" || !navigator.share || !navigator.canShare;
    }
    function closeLightbox() { state.lightbox=null; $("#lightbox").hidden=true; $("#lbImg").removeAttribute("src"); unlockScroll(); }

    function lockScroll() { document.documentElement.classList.add("lock"); }
    function unlockScroll() { if ($("#previewModal").hidden && $("#lightbox").hidden && $("#manualCopy").hidden && !$("#publishModal").classList.contains("show")) document.documentElement.classList.remove("lock"); }

    function countsFor(accountId) {
      const result={pending:0,published:0};
      notes.filter(n=>n.accountId===accountId).forEach(n=>{ result[noteStatus(n)]++; });
      return result;
    }
    function renderAccounts() {
      $("#accountList").innerHTML=accounts.map(account=>{
        const counts=countsFor(account.id); const initial=account.label.replace(/^@/,"").slice(0,1).toUpperCase();
        return `<button class="account-row ${state.selectedAccount===account.id?"active":""}" data-account="${account.id}" type="button">
          <span class="avatar" style="--avatar:${account.color}">${escapeHtml(initial)}</span>
          <span class="account-text"><span class="account-name">${escapeHtml(account.label)}</span><span class="account-counts"><span>待发布 ${counts.pending}</span><span>已发布 ${counts.published}</span></span></span>${icons.next}
        </button>`;
      }).join("");
    }
    function openButton(note) {
      const kind=kindOf(note);
      // 长文（用户定案）：X App 无长文创作入口，只走网页。一次点击内先把标题写进剪贴板，再 window.open 新标签打开长文编辑页
      if (kind==="article") return `<a class="action-button primary" href="${ARTICLE_EDITOR}" target="_blank" rel="noopener" data-go-article="${note.id}">去发布长文</a>`;
      const text=copyPayload(note,"body");
      if (!text) return `<button class="action-button" type="button" disabled>正文为空</button>`;
      return `<a class="action-button" href="${escapeHtml(intentUrl(text))}" target="_blank" rel="noopener" data-open-copy="body" data-id="${note.id}">打开 X 发帖</a>`;
    }
    // 卡片（Round 4 用户定案）：每张卡片固定大小、统一版式——元信息行 / 标题 / 正文区 / 图片区 / 按钮区 / 状态栏各就各位
    // 标题固定 2 行、正文区固定行数截断，图片区固定 4 格 cover 填充；完整内容看预览
    function statusRail(note, timeText) {
      const status=noteStatus(note);
      const action=status==="published"
        ? `<button class="undo-button" data-undo="${note.id}" type="button">撤销已发布</button>`
        : `<button class="publish-button" data-publish="${note.id}" type="button">发完了</button>`;
      return `<div class="status-rail"><span class="status-icon">${icons.check}</span><span class="status-copy"><span class="status-name">${status==="published"?"已发布":"待发布"}</span><span class="status-time">${escapeHtml(timeText)}</span></span>${action}</div>`;
    }
    function mediaZone(note) {
      const images=note.images||[]; const slots=[];
      for (let i=0;i<4;i++) {
        if (i<images.length) { const more=i===3&&images.length>4?`<span class="thumb-more">+${images.length-4}</span>`:"";
          slots.push(`<button class="thumb-cell" type="button" data-zoom="${note.id}" data-index="${i}" aria-label="查看图片 ${i+1}"><img src="${escapeHtml(images[i])}" alt="图片${i+1}" loading="lazy" decoding="async">${more}</button>`); }
        else slots.push(`<span class="thumb-cell thumb-empty" aria-hidden="true">${i===0&&!images.length?"无配图":""}</span>`);
      }
      const save=images.length>1
        ? `<button class="save-button save-all" type="button" data-save-all="${note.id}">${icons.save}保存全部图片（${images.length} 张）</button>`
        : images.length===1
          ? `<button class="save-button save-all" type="button" data-save-all="${note.id}">${icons.save}保存图片</button>`
          : `<button class="save-button save-all" type="button" disabled>${icons.save}无图片可保存</button>`;
      return `<div class="media-zone"><div class="thumb-row">${slots.join("")}</div>${save}</div>`;
    }
    function actionRows(note) {
      const kind=kindOf(note); const id=note.id;
      const btn=(cls,attrs,label)=>`<button class="action-button${cls}" ${attrs} type="button">${label}</button>`;
      const preview=btn(" xblue",`data-preview="${id}"`,"预览发布效果");
      let row1, row2;
      if (kind==="article") { row1=[openButton(note), btn("",`data-copy="body" data-id="${id}"`,"复制正文")]; row2=[preview, btn("",`data-copy="title" data-id="${id}"`,"复制标题")]; }
      else if (kind==="thread") { row1=[btn(" primary",`data-copy="all" data-id="${id}"`,"复制整串"), btn("",`data-copy="body" data-id="${id}"`,"复制首条"), btn("",`data-copy="thread" data-id="${id}"`,"复制串/回复")]; row2=[preview, openButton(note)]; }
      else { row1=[btn(" primary",`data-copy="body" data-id="${id}"`,"复制正文")]; if (clean(note.thread)) row1.push(btn("",`data-copy="thread" data-id="${id}"`,"复制首条回复")); row2=[preview, openButton(note)]; }
      return `<div class="actions fixed-actions"><div class="act-row" style="--cols:${row1.length}">${row1.join("")}</div><div class="act-row" style="--cols:${row2.length}">${row2.join("")}</div></div>`;
    }
    function noteCard(note) {
      const status=noteStatus(note); const kind=kindOf(note);
      const plan=planLabel(note.planTime);
      const typeBadge=note.type?`<span class="badge type" data-kind="${kind}">${escapeHtml(note.type)}</span>`:"";
      const body=clean(note.body), thread=clean(note.thread);
      const internal=kind==="article"?"":`<span class="internal-tag" title="短帖/线程的标题只是内部标识，不会发出去">内部标识</span>`;
      const threadHtml=thread?`<div class="thread-text"><b>${kind==="thread"?"串":"首条回复"}</b>${escapeHtml(thread)}</div>`:"";
      const timeText=status==="published"?`发布于 ${formatTime(note.publishedAt)||note.publishedAt}`:"";
      return `<article class="note-card fixed" data-id="${note.id}" data-status="${status}" data-kind="${kind}">
        <div class="note-main">
          <div class="note-meta"><span class="meta-acc">${escapeHtml(accountLabel(note.accountId))}</span>${typeBadge}${plan?`<span class="plan-chip">${escapeHtml(plan)}</span>`:""}<span class="badge">${escapeHtml(note.id)}</span></div>
          <div class="preview-hit" data-preview="${note.id}" title="点击预览发到 X 的效果">
            <h2 class="note-title">${escapeHtml(note.title||"（无标题）")}${internal}</h2>
            <div class="note-text${thread?" has-thread":""}"><div class="body-text">${body?escapeHtml(body):'<span class="text-empty">（正文为空）</span>'}</div>${threadHtml}</div>
          </div>
          ${mediaZone(note)}
          ${actionRows(note)}
        </div>
        ${statusRail(note, timeText)}
      </article>`;
    }
    function renderDetail() {
      if (!accounts.length) { $("#detailTitle").textContent="暂无账号"; $("#noteList").innerHTML=`<div class="empty"><strong>板上暂无内容</strong><span>发布库里有待发布/已上板/已发布记录后会显示在这里。</span></div>`; return; }
      $("#detailTitle").textContent=accountLabel(state.selectedAccount);
      const accountNotes=notes.filter(n=>n.accountId===state.selectedAccount);
      const counts=countsFor(state.selectedAccount);
      $$("[data-status-filter]").forEach(btn=>{ const key=btn.dataset.statusFilter; btn.classList.toggle("active",key===state.statusFilter); $("span",btn).textContent=counts[key]; });
      $("#detailCount").textContent=`共 ${accountNotes.length} 条`;
      const types=[...new Set(accountNotes.map(n=>n.type).filter(Boolean))];
      if(state.typeFilter!=="all"&&!types.includes(state.typeFilter)) state.typeFilter="all";
      $("#tabs").innerHTML=`<button class="tab ${state.typeFilter==="all"?"active":""}" data-filter="all" type="button">全部 ${accountNotes.length}</button>`+types.map(t=>{
        const c=accountNotes.filter(n=>n.type===t).length;
        return `<button class="tab ${state.typeFilter===t?"active":""}" data-filter="${escapeHtml(t)}" type="button">${escapeHtml(t)} ${c}</button>`;
      }).join("");
      const visible=accountNotes.filter(n=>noteStatus(n)===state.statusFilter).filter(n=>state.typeFilter==="all"||n.type===state.typeFilter).sort(byPlanTime);
      const emptyTitle=state.statusFilter==="published"?"还没有已发布的帖子":"这里还没有待发布帖子";
      const emptyHint=state.statusFilter==="published"?"点“发完了”的帖子会显示在这里。":"发布库状态为待发布/已上板时会显示在这里。";
      $("#noteList").innerHTML=visible.length?visible.map(noteCard).join(""):`<div class="empty"><strong>${emptyTitle}</strong><span>${emptyHint}</span></div>`;
      // 预取可见卡片的图片，保证点「保存」时能在手势内同步调起分享面板
      const idle=window.requestIdleCallback||(fn=>setTimeout(fn,300));
      idle(()=>prefetchFiles(visible.flatMap(n=>n.images||[])));
    }
    function render() { renderAccounts(); renderDetail(); if (state.previewId) renderPreview(); }

    // ---- 发布预览（仿 X） ----
    function fmtStamp(iso) {
      let d=iso?new Date(iso):new Date(); if (Number.isNaN(d.valueOf())) d=new Date();
      const p=Object.fromEntries(new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(d).map(x=>[x.type,x.value]));
      return {full:`${p.hour}:${p.minute} · ${p.year}/${p.month}/${p.day}`, short:`${p.month}月${p.day}日`};
    }
    function authorRow(note) {
      const who=handleOf(note);
      return `<div class="xd-author"><span class="tw-avatar xd-avatar" style="--avatar:${accountColor(note.accountId)}">${escapeHtml(who.name.slice(0,1).toUpperCase())}</span><div class="xd-names"><div class="xd-name"><span>${escapeHtml(who.name)}</span>${icons.verified}</div><div class="xd-handle">${escapeHtml(who.handle)}</div></div><span class="xd-brand">𝕏.com</span></div>`;
    }
    function detailActions() {
      return `<div class="xd-actions" aria-hidden="true">${icons.reply}${icons.repost}${icons.like}${icons.bookmark}${icons.share}</div>`;
    }
    // X 拼图：每张图按自身宽高比等高排成一行（参考图：左竖图+右横图）；3 张放不下时首张独占一行，4 张两行两列
    function ratioOf(note, i) { const sz=note.imageSizes&&note.imageSizes[i]; const r=sz&&sz[0]&&sz[1]?sz[0]/sz[1]:1; return Math.min(2.4, Math.max(0.45, r)); }
    function mosaic(note, idxs, opts) {
      opts=opts||{}; if (!idxs.length) return "";
      const rs=idxs.map(i=>ratioOf(note,i));
      let rows;
      if (idxs.length<=2) rows=[idxs];
      else if (idxs.length===3) rows=(rs[0]+rs[1]+rs[2]<=2.6)?[idxs]:[[idxs[0]],[idxs[1],idxs[2]]];
      else rows=[[idxs[0],idxs[1]],[idxs[2],idxs[3]]];
      const last=idxs[idxs.length-1];
      return `<div class="xd-media">${rows.map(row=>`<div class="xd-row">${row.map(i=>{ let r=ratioOf(note,i); if (idxs.length===1) r=Math.max(r,opts.minSingle||0.75);
        const img=`<img src="${escapeHtml(note.images[i])}" alt="" decoding="async"${opts.zoom?' loading="lazy"':""}>`; const more=opts.more>0&&i===last?`<span class="xd-more">+${opts.more}</span>`:"";
        const min=idxs.length===1?` data-min="${opts.minSingle||0.75}"`:"";
        return opts.zoom?`<button type="button" class="xd-cell" style="--r:${r.toFixed(3)}"${min} data-zoom="${note.id}" data-index="${i}" aria-label="查看图片 ${i+1}">${img}${more}</button>`:`<div class="xd-cell" style="--r:${r.toFixed(3)}"${min}>${img}</div>`; }).join("")}</div>`).join("")}</div>`;
    }
    function detailPost(note, text, idxs, emptyText) {
      return `<article class="xd">${authorRow(note)}${text?`<div class="xd-text">${linkify(text)}</div>`:`<div class="xd-text tw-empty">${emptyText}</div>`}${mosaic(note, idxs)}<div class="xd-meta">${fmtStamp(note.planTime).full} · <b>0</b> 次查看</div>${detailActions()}</article>`;
    }
    function inlineRich(text) { return text.split(/(\*\*[^*\n]+\*\*)/g).map(part=>/^\*\*[^*\n]+\*\*$/.test(part)?`<strong>${linkify(part.slice(2,-2))}</strong>`:linkify(part)).join(""); }
    function articleHtml(note) {
      const who=handleOf(note); const images=note.images||[]; const body=clean(note.body); const title=clean(note.title);
      const paras=body.split(/\n+/).map(x=>x.trim()).filter(Boolean);
      const shown=paras;
      // 文内图：第 2 张起依次插在第 2、4、6… 段后，余下的放在末尾（示意位置）
      const inline=images.slice(1).map((src,k)=>({src, index:k+1}));
      let bodyHtml="";
      const inlineImg=im=>`<div class="xa-img"><img src="${escapeHtml(im.src)}" alt="" decoding="async"></div>`;
      shown.forEach((para,i)=>{ bodyHtml+=`<p>${inlineRich(para)}</p>`; if (i%2===1 && inline.length) bodyHtml+=inlineImg(inline.shift()); });
      inline.forEach(im=>{ bodyHtml+=inlineImg(im); });
      if (!shown.length) bodyHtml='<p class="tw-empty">正文为空</p>';
      const cover=images.length
        ? `<div class="xa-cover"><img src="${escapeHtml(images[0])}" alt="封面"></div>`
        : `<div class="xa-cover none"></div>`;
      const n=v=>`<span>${v}</span>`;
      return `<article class="xa">
        <div class="xa-hero">${cover}<button class="xa-back" data-close-preview type="button" aria-label="关闭预览"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg></button></div>
        <h1 class="xa-title ${title?"":"empty"}">${title?escapeHtml(title):"标题为空"}</h1>
        <div class="xa-author"><span class="tw-avatar xd-avatar" style="--avatar:${accountColor(note.accountId)}">${escapeHtml(who.name.slice(0,1).toUpperCase())}</span><div class="xd-names"><div class="xd-name"><span>${escapeHtml(who.name)}</span>${icons.verified}</div><div class="xd-handle">${escapeHtml(who.handle)} · ${fmtStamp(note.planTime).short}</div></div><span class="xa-follow">关注</span></div>
        <div class="xa-body">${bodyHtml}</div>
        <div class="xa-pill" aria-hidden="true">${n(icons.reply+"0")}${n(icons.repost+"0")}${n(icons.like+"0")}${n(icons.bookmark+"0")}${n(icons.share)}</div>
      </article>`;
    }
    function renderPreview() {
      const note=findNote(state.previewId); if (!note) { closePreview(); return; }
      const kind=kindOf(note); const images=note.images||[];
      $("#previewSheet").classList.toggle("article", kind==="article");
      $("#previewTitle").textContent="帖子";
      $("#previewModal").setAttribute("aria-label", `${kind==="article"?"长文":kind==="thread"?"线程":"帖子"}发布预览`);
      let mock;
      if (kind==="article") mock=articleHtml(note);
      else {
        const idxs=images.slice(0,4).map((_,i)=>i);
        mock=detailPost(note, clean(note.body), idxs, "正文为空") + (clean(note.thread)?detailPost(note, clean(note.thread), [], ""):"");
      }
      // 纯预览：只呈现仿 X 页面，不放任何操作按钮或说明（操作都在卡片上）
      $("#previewBody").innerHTML=`<div class="x-frame">${mock}</div>`;
    }
    function openPreview(id, trigger) {
      if (!findNote(id)) return;
      state.previewId=id; state.lastFocus=trigger||document.activeElement;
      renderPreview(); $("#previewModal").hidden=false; lockScroll(); document.documentElement.classList.add("preview-open");
      $("#previewBody").scrollTop=0; const closer=[...$$("[data-close-preview]")].find(el=>el.offsetParent!==null); if (closer) closer.focus({preventScroll:true});
    }
    function closePreview() {
      state.previewId=null; $("#previewModal").hidden=true; document.documentElement.classList.remove("preview-open"); unlockScroll();
      const f=state.lastFocus; state.lastFocus=null; if (f && f.isConnected && f.focus) { try { f.focus({preventScroll:true}); } catch {} }
    }
    function showToast(message, ms) { const el=$("#toast"); el.textContent=message; el.classList.add("show"); clearTimeout(showToast.timer); showToast.timer=setTimeout(()=>el.classList.remove("show"),ms||1800); }

    async function api(action, boardNo) {
      const response=await fetch("/x-api/state",{method:"POST",headers:{"Content-Type":"application/json","X-Board-Token":BOARD_TOKEN},body:JSON.stringify({board_no:boardNo,action})});
      const payload=await response.json().catch(()=>({ok:false,error:"invalid_response"}));
      if(!response.ok||!payload.ok) throw new Error(payload.error||`HTTP ${response.status}`);
      return payload;
    }
    async function publishNote(id) {
      const note=findNote(id); if(!note) return;
      const result=await api("published",id);
      note.status="已发布"; note.publishedAt=result.published_at||new Date().toISOString(); render();
    }
    async function undoPublish(id) {
      const note=findNote(id); if(!note) return;
      await api("undo",id);
      note.status="已上板"; note.publishedAt=""; render();
    }

    // ---- 事件：整页委托，卡片与预览共用同一套按钮 ----
    document.addEventListener("click", event=>{
      const t=event.target;
      if (t.closest("[data-noop]")) { event.preventDefault(); return; }
      if (t.closest("[data-close-preview]")) { closePreview(); return; }
      const copy=t.closest("[data-copy]");
      if (copy) { const note=findNote(copy.dataset.id); if(note) doCopy(note, copy.dataset.copy); return; }
      const goArticle=t.closest("[data-go-article]");
      if (goArticle) {
        // 同一手势内：先同步写剪贴板（标题），再 window.open；被拦截时退回本页跳转，保证能到编辑页
        event.preventDefault();
        const note=findNote(goArticle.dataset.goArticle); const title=note?copyPayload(note,"title"):"";
        let copied=!!title && copyViaExecCommand(title);
        if (!copied && title && window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) { try { navigator.clipboard.writeText(title).catch(()=>{}); copied=true; } catch {} }
        const win=window.open(ARTICLE_EDITOR, "_blank");
        if (win) { try { win.opener=null; } catch {} } else { window.location.href=ARTICLE_EDITOR; }
        showToast(copied?"标题已复制，已打开 X 长文编辑页":"已打开 X 长文编辑页；标题没复制上，可点「复制标题」", 3000);
        return;
      }
      const open=t.closest("[data-open-copy]");
      if (open) {
        // 不阻止默认跳转：真实 <a> 点击才能触发 X 的通用链接（进 App）且不被弹窗拦截；同时同步复制兜底
        const note=findNote(open.dataset.id); const kind=open.dataset.openCopy;
        if (note) { const text=copyPayload(note, kind); if (text && copyViaExecCommand(text)) showToast(`正在打开 X · ${copyLabels[kind]}已复制，预填丢失可直接粘贴`, 3000); }
        return;
      }
      const saveAll=t.closest("[data-save-all]");
      if (saveAll) { const note=findNote(saveAll.dataset.saveAll); if(note) saveAllImages((note.images||[]).slice()); return; }
      const queueNext=t.closest("#sqNext");
      if (queueNext) { shareQueueNext(); return; }
      if (t.closest("#sqClose")) { endQueue(); return; }
      const save=t.closest("[data-save]");
      if (save) { const note=findNote(save.dataset.save); if(!note) return; const srcs=save.dataset.index==="all"?note.images.slice():[note.images[Number(save.dataset.index)]].filter(Boolean); saveImages(srcs); return; }
      const zoom=t.closest("[data-zoom]");
      if (zoom) { const note=findNote(zoom.dataset.zoom); if(note&&note.images.length) openLightbox(note.images, Number(zoom.dataset.index)||0, "view"); return; }
      const preview=t.closest("[data-preview]");
      if (preview) { openPreview(preview.dataset.preview, preview); return; }
      const publish=t.closest("[data-publish]");
      if (publish) { state.pendingPublish=publish.dataset.publish; $("#publishModal").classList.add("show"); lockScroll(); $("#confirmPublish").focus({preventScroll:true}); return; }
      const undo=t.closest("[data-undo]");
      if (undo) { undo.disabled=true; undoPublish(undo.dataset.undo).then(()=>showToast("已撤销发布标记"), error=>showToast(`撤销失败：${error.message}`)).finally(()=>{ undo.disabled=false; }); }
    });
    document.addEventListener("load", event=>{
      const img=event.target; if (!(img instanceof HTMLImageElement) || !img.naturalWidth) return;
      const cell=img.closest(".xd-cell"); if (!cell) return;
      const real=Math.min(2.4, Math.max(0.45, img.naturalWidth/img.naturalHeight));
      const r=cell.dataset.min?Math.max(real, parseFloat(cell.dataset.min)):real;
      if (Math.abs(r-parseFloat(cell.style.getPropertyValue("--r")||"1"))>0.02) cell.style.setProperty("--r", r.toFixed(3));
    }, true);
    $("#backButton").innerHTML=icons.back;
    $("#accountList").addEventListener("click",event=>{ const row=event.target.closest("[data-account]"); if(!row)return; state.selectedAccount=row.dataset.account; state.statusFilter="pending"; state.typeFilter="all"; storageSet("activeXAccount",state.selectedAccount); $("#detailPanel").classList.add("open"); render(); });
    $("#backButton").addEventListener("click",()=>{ storageSet("activeXAccount",null); $("#detailPanel").classList.remove("open"); });
    $(".status-tabs").addEventListener("click",event=>{ const button=event.target.closest("[data-status-filter]"); if(!button)return; state.statusFilter=button.dataset.statusFilter; renderDetail(); });
    $("#tabs").addEventListener("click",event=>{ const button=event.target.closest(".tab"); if(!button)return; state.typeFilter=button.dataset.filter; renderDetail(); });
    $("#previewModal").addEventListener("click",event=>{ if(event.target===event.currentTarget) closePreview(); });
    $("#lbClose").addEventListener("click",closeLightbox);
    $("#lightbox").addEventListener("click",event=>{ if(event.target===event.currentTarget||event.target.classList.contains("lb-stage")) closeLightbox(); });
    $("#lbSave").addEventListener("click",()=>{ const lb=state.lightbox; if(lb) saveImages([lb.srcs[lb.index]]); });
    $("#lbPrev").addEventListener("click",()=>{ const lb=state.lightbox; if(!lb) return; lb.index=(lb.index-1+lb.srcs.length)%lb.srcs.length; updateLightbox(); });
    $("#lbNext").addEventListener("click",()=>{ const lb=state.lightbox; if(!lb) return; lb.index=(lb.index+1)%lb.srcs.length; updateLightbox(); });
    $("#manualClose").addEventListener("click",()=>{ $("#manualCopy").hidden=true; unlockScroll(); });
    $("#manualText").addEventListener("focus",event=>{ const ta=event.target; setTimeout(()=>ta.setSelectionRange(0,ta.value.length),0); });
    $("#cancelPublish").addEventListener("click",()=>{ state.pendingPublish=null; $("#publishModal").classList.remove("show"); unlockScroll(); });
    $("#confirmPublish").addEventListener("click",async()=>{ if(!state.pendingPublish)return; const id=state.pendingPublish; state.pendingPublish=null; $("#publishModal").classList.remove("show"); unlockScroll(); const button=$("#confirmPublish"); button.disabled=true; try { await publishNote(id); showToast("已标记发布并回写飞书"); } catch(error) { showToast(`保存失败：${error.message}`); } finally { button.disabled=false; } });
    document.addEventListener("keydown",event=>{
      if (event.key!=="Escape") return;
      if (!$("#manualCopy").hidden) { $("#manualCopy").hidden=true; unlockScroll(); }
      else if ($("#publishModal").classList.contains("show")) $("#cancelPublish").click();
      else if (!$("#lightbox").hidden) closeLightbox();
      else if (!$("#previewModal").hidden) closePreview();
    });
    $("#refreshButton").addEventListener("click",()=>window.location.reload());
    if (IN_APP) $("#inappTip").hidden=false;
    const savedAccount=storageGet("activeXAccount");
    if (savedAccount && accounts.some(a=>a.id===savedAccount)) {
      state.selectedAccount=savedAccount;
      if (window.innerWidth<=760) $("#detailPanel").classList.add("open");
    }
    render();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
