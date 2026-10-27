#!/usr/bin/env python3
"""X 数据回采共享核心。

- 飞书发布库 / 账号日快照的读写（经 lark-cli --as user）
- 帖子链接正文匹配（正文相似度 + 发布时间 ±24h）
- 快照落盘 /opt/xcollect/snapshots.jsonl
- 1h / 24h / 72h 三窗口回填（本地 fills.jsonl 做幂等）
- 日志 /opt/xcollect/ingest.log

ingest_server.py 与 fx_poll.py 共用本模块，保证同一套口径。
"""
from __future__ import annotations

import contextlib
import difflib
import json
import logging
import logging.handlers
import math
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")
PUBLISH_TABLE = os.environ.get("FEISHU_PUBLISH_TABLE_ID", "")
DAILY_TABLE = os.environ.get("FEISHU_DAILY_TABLE_ID", "")
# 楼层表（串帖逐楼）：可选；未配置时回采跳过逐楼回填
FLOORS_TABLE = os.environ.get("FEISHU_FLOORS_TABLE_ID", "")
TARGET_ACCOUNT = os.environ.get("X_TARGET_ACCOUNT", "your_handle").lstrip("@")
TARGET_ACCOUNT_AT = "@" + TARGET_ACCOUNT

SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_DIR = Path(os.environ.get("XCOLLECT_STATE_DIR", "/opt/xcollect"))
SNAPSHOTS_FILE = STATE_DIR / "snapshots.jsonl"
FILLS_FILE = STATE_DIR / "fills.jsonl"
LOG_FILE = STATE_DIR / "ingest.log"
LARK = os.environ.get("LARK_CLI", "/opt/node24/bin/lark-cli")
NODE_BIN = "/opt/node24/bin"

PUBLISH_FIELDS = [
    "状态",
    "发布时间",
    "正文",
    "标题/首句",
    "帖子链接",
    "账号",
    "曝光",
    "点赞",
    "收藏",
    "转发",
    "回复",
    "主页访问",
    "链接点击",
    "1小时互动",
    "24小时曝光",
]
DAILY_FIELDS = ["日期", "账号", "粉丝总数", "认证粉丝数"]
FLOOR_FIELDS = ["楼层", "所属帖", "楼号", "本楼链接", "点赞", "收藏", "回复"]

# 三个回填窗口（小时）
WINDOW_1H = (0.75, 1.5)
WINDOW_24H = (20.0, 28.0)
WINDOW_72H = (66.0, 84.0)

WRITE_LOCK = threading.RLock()
log = logging.getLogger("xcollect")

URL_RE = re.compile(r"https?://[^\s)>\]]+")
STATUS_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)", re.I
)
STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d+)", re.I)
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{1,15}")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def configure_logging(component: str = "core") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / ("fx_poll.log" if component == "fx" else "ingest.log")
    fmt = logging.Formatter(
        f"%(asctime)s %(levelname)s [{component}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in root.handlers
    ):
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(log_path, maxBytes=4 * 1024 * 1024, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        with contextlib.suppress(OSError):
            os.chmod(log_path, 0o600)


def now_beijing() -> datetime:
    return datetime.now(SHANGHAI)


def parse_dt(value: Any) -> datetime | None:
    """把 ISO / 飞书日期 / 毫秒时间戳统一成 aware datetime（北京时区）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, SHANGHAI)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,16}", s):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, SHANGHAI)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                    "%a %b %d %H:%M:%S %z %Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI)


def iso_beijing(dt: datetime) -> str:
    return dt.astimezone(SHANGHAI).isoformat(timespec="seconds")


def date_key(dt: datetime) -> str:
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def parse_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    s = str(value).strip().replace(",", "").replace("，", "")
    s = s.replace("次", "").strip()
    mult = 1
    if s and s[-1] in "Kk":
        mult, s = 1_000, s[:-1]
    elif s and s[-1] in "Mm":
        mult, s = 1_000_000, s[:-1]
    elif s and s[-1] in "Bb":
        mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("万"):
        mult, s = 10_000, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100_000_000, s[:-1]
    try:
        n = float(s) * mult
    except ValueError:
        return None
    return int(round(n)) if abs(n - round(n)) < 1e-9 else n


def extract_url(value: Any) -> str | None:
    """飞书 link 字段读回是 markdown [text](url)，兼容纯 URL / dict。"""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("link", "url", "text"):
            if value.get(k):
                return extract_url(value[k])
        return None
    if isinstance(value, list):
        for item in value:
            u = extract_url(item)
            if u:
                return u
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"\((https?://[^)\s]+)\)", s)
    if m:
        return m.group(1)
    m = URL_RE.search(s)
    return m.group(0) if m else (s if s.startswith("http") else None)


def post_id_from_url(value: Any) -> str | None:
    u = extract_url(value)
    if not u:
        return None
    m = STATUS_ID_RE.search(u)
    return m.group(1) if m else None


def first_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def normalize_account(value: Any) -> str | None:
    v = first_value(value)
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if not s.startswith("@"):
        s = "@" + s
    return s


def normalize_text(text: Any) -> str:
    s = str(text or "")
    s = re.sub(r"https?://\S+", " ", s)
    s = MENTION_RE.sub(" ", s)
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = s.lower()
    # 去掉空白、标点、emoji，仅保留中日韩、字母、数字
    s = re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", s)
    return s


def text_similarity(a: Any, b: Any) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 长文截断，避免 SequenceMatcher 平方级开销
    if len(na) > 1200:
        na = na[:1200]
    if len(nb) > 1200:
        nb = nb[:1200]
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


# --------------------------------------------------------------------------
# 飞书读写
# --------------------------------------------------------------------------
class LarkError(RuntimeError):
    pass


def _lark_env() -> dict[str, str]:
    env = os.environ.copy()
    path = env.get("PATH", "")
    env["PATH"] = NODE_BIN + (":" + path if path else ":/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", "/root")
    env.setdefault("TZ", "Asia/Shanghai")
    return env


def run_lark(base_args: list[str], timeout: int = 120) -> dict:
    cmd = [LARK, "base", *base_args, "--as", "user", "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=_lark_env(), cwd=str(STATE_DIR))
    except subprocess.TimeoutExpired as exc:
        raise LarkError(f"lark-cli timeout after {timeout}s") from exc
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise LarkError(f"lark-cli rc={proc.returncode}: {(raw or proc.stderr)[:800]}")
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LarkError(f"lark-cli non-json output: {raw[:400]}") from exc
    if not out.get("ok"):
        raise LarkError(f"lark-cli error: {json.dumps(out.get('error'), ensure_ascii=False)[:800]}")
    return out


def list_records(table_id: str, fields: Iterable[str], page_size: int = 200) -> list[dict]:
    data_rows: list[dict] = []
    offset = 0
    for _ in range(20):
        args = ["+record-list", "--base-token", BASE_TOKEN, "--table-id", table_id,
                "--limit", str(page_size), "--offset", str(offset)]
        for f in fields:
            args += ["--field-id", str(f)]
        out = run_lark(args)
        data = out.get("data") or {}
        names = data.get("fields") or []
        ids = data.get("record_id_list") or []
        rows = data.get("data") or []
        for rid, row in zip(ids, rows):
            item = {"_record_id": rid}
            for name, value in zip(names, row):
                item[name] = value
            data_rows.append(item)
        if not data.get("has_more"):
            break
        offset += len(rows)
        if not rows:
            break
    return data_rows


def update_record(table_id: str, record_id: str, fields: dict) -> None:
    body = json.dumps({"update_records": {record_id: fields}}, ensure_ascii=False)
    run_lark(["+record-batch-update", "--base-token", BASE_TOKEN, "--table-id", table_id,
              "--json", body])


def create_records(table_id: str, rows: list[dict]) -> list[str]:
    body = json.dumps({"create_records": rows}, ensure_ascii=False)
    out = run_lark(["+record-batch-create", "--base-token", BASE_TOKEN,
                    "--table-id", table_id, "--json", body])
    return list((out.get("data") or {}).get("record_id_list") or [])


def delete_records(table_id: str, record_ids: list[str]) -> None:
    body = json.dumps({"record_id_list": record_ids}, ensure_ascii=False)
    run_lark(["+record-delete", "--base-token", BASE_TOKEN, "--table-id", table_id,
              "--json", body, "--yes"])


def record_status(row: dict) -> str | None:
    return first_value(row.get("状态"))


def record_link(row: dict) -> str | None:
    return extract_url(row.get("帖子链接"))


def record_published_at(row: dict) -> datetime | None:
    return parse_dt(row.get("发布时间"))


def record_text(row: dict) -> str:
    return str(row.get("正文") or row.get("标题/首句") or "")


# --------------------------------------------------------------------------
# 快照 / 回填记录
# --------------------------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError as exc:
        log.warning("read %s failed: %s", path, exc)
    return out


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_snapshots() -> list[dict]:
    return _read_jsonl(SNAPSHOTS_FILE)


def load_fills() -> list[dict]:
    return _read_jsonl(FILLS_FILE)


def window_for(hours: float | None) -> str | None:
    if hours is None or math.isnan(hours):
        return None
    if WINDOW_1H[0] <= hours <= WINDOW_1H[1]:
        return "1h"
    if WINDOW_24H[0] <= hours <= WINDOW_24H[1]:
        return "24h"
    if WINDOW_72H[0] <= hours <= WINDOW_72H[1]:
        return "72h"
    return None


def _metrics_present(metrics: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("views", "likes", "bookmarks", "reposts", "replies", "quotes",
                "profile_visits", "link_clicks"):
        val = parse_number(metrics.get(key))
        if val is not None:
            out[key] = val
    return out


def append_snapshot(*, record_id: str, post_id: str, published_at: datetime | None,
                    captured_at: datetime, metrics: dict, raw: dict | None,
                    source: str) -> tuple[dict, bool]:
    """写快照。完全相同的 (record, post, source, 采集时刻) 视为重复，幂等返回。"""
    clean = _metrics_present(metrics)
    hours = None
    if published_at is not None:
        hours = round((captured_at - published_at).total_seconds() / 3600.0, 3)
    snap = {
        "record_id": record_id,
        "post_id": post_id,
        "collected_at": iso_beijing(captured_at),
        "published_at": iso_beijing(published_at) if published_at else None,
        "hours_since_publish": hours,
        "window": window_for(hours),
        "source": source,
        "metrics": clean,
        "raw": raw or {},
    }
    with WRITE_LOCK:
        for old in load_snapshots():
            if (old.get("record_id") == record_id and old.get("post_id") == post_id
                    and old.get("source") == source
                    and old.get("collected_at") == snap["collected_at"]):
                return old, True
        _append_jsonl(SNAPSHOTS_FILE, snap)
    log.info("snapshot record=%s post=%s source=%s collected=%s hours=%s window=%s metrics=%s",
             record_id, post_id, source, snap["collected_at"], hours, snap["window"],
             json.dumps(clean, ensure_ascii=False, sort_keys=True))
    return snap, False


def _fills_for(record_id: str, window: str) -> dict | None:
    """返回该 record+window 的累计回填字段（72h 可能有多次补齐）。"""
    combined: dict | None = None
    for fill in load_fills():
        if fill.get("record_id") != record_id or fill.get("window") != window:
            continue
        if combined is None:
            combined = {"fields": {}, "events": [], "at": fill.get("at"),
                        "record_id": record_id, "window": window}
        combined["fields"].update(fill.get("fields") or {})
        combined["events"].append(fill)
        combined["at"] = fill.get("at") or combined.get("at")
    return combined


def _snapshots_in_window(record_id: str, post_id: str, window: str) -> list[dict]:
    items = []
    for idx, snap in enumerate(load_snapshots()):
        if snap.get("record_id") != record_id or snap.get("post_id") != post_id:
            continue
        if snap.get("window") != window:
            continue
        dt = parse_dt(snap.get("collected_at"))
        if dt is None:
            continue
        items.append((dt, idx, snap))
    items.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in items]


def _first_snapshot_in_window(record_id: str, post_id: str, window: str) -> dict | None:
    items = _snapshots_in_window(record_id, post_id, window)
    return items[0] if items else None


def _merge_window_metrics(snaps: list[dict]) -> dict:
    """72h 定格：每个指标取窗口内最早出现的值，缺的字段由后续快照补齐。"""
    merged: dict[str, Any] = {}
    for snap in snaps:
        for key, value in _metrics_present(snap.get("metrics") or {}).items():
            merged.setdefault(key, value)
    return merged


def _fields_for_window(window: str, metrics: dict) -> dict:
    if window == "1h":
        parts = [metrics.get(k) for k in ("likes", "bookmarks", "reposts", "replies")]
        present = [p for p in parts if p is not None]
        if not present:
            return {}
        return {"1小时互动": int(sum(present))}
    if window == "24h":
        if metrics.get("views") is None:
            return {}
        return {"24小时曝光": metrics["views"]}
    if window == "72h":
        fields: dict[str, Any] = {"状态": ["已复盘"]}
        mapping = {
            "views": "曝光",
            "likes": "点赞",
            "bookmarks": "收藏",
            "reposts": "转发",
            "replies": "回复",
            "profile_visits": "主页访问",
            "link_clicks": "链接点击",
        }
        for src, dst in mapping.items():
            if metrics.get(src) is not None:
                fields[dst] = metrics[src]
        return fields
    return {}


def apply_window_fill(snap: dict, record: dict | None = None) -> dict:
    """按窗口规则回填。返回 {filled, window, fields, reason}。

    幂等：同一 record + window 只回填一次（fills.jsonl）。
    窗口内以最早一份快照为准；晚到的快照不会覆盖。
    """
    record_id = snap["record_id"]
    post_id = snap["post_id"]
    window = snap.get("window")
    result = {"record_id": record_id, "post_id": post_id, "window": window,
              "filled": False, "fields": {}, "reason": ""}
    if window is None:
        result["reason"] = "outside_window_archive_only"
        log.info("fill skip record=%s post=%s: outside window, archive only (hours=%s)",
                 record_id, post_id, snap.get("hours_since_publish"))
        return result
    with WRITE_LOCK:
        old = _fills_for(record_id, window)
        snaps = _snapshots_in_window(record_id, post_id, window)
        if not snaps:
            result["reason"] = "no_snapshot"
            return result

        if window == "72h":
            # 72h 定格：每个字段取窗口内最早一份有该字段的快照；晚到快照只补
            # 缺失字段（典型：FxTwitter 先给了公开指标，扩展后补主页访问/链接点击），
            # 绝不覆盖已经写过的值。
            merged = _merge_window_metrics(snaps)
            desired = _fields_for_window("72h", merged)
            old_fields = (old.get("fields") or {}) if old else {}
            fields = {k: v for k, v in desired.items() if k not in old_fields}
            if not fields:
                result["reason"] = "already_filled"
                result["fields"] = old_fields
                log.info("fill skip record=%s window=72h: all fields already locked", record_id)
                return result
            update_record(PUBLISH_TABLE, record_id, fields)
            _append_jsonl(FILLS_FILE, {
                "at": iso_beijing(now_beijing()),
                "record_id": record_id,
                "post_id": post_id,
                "window": "72h",
                "collected_at": snaps[0].get("collected_at"),
                "fields": fields,
                "supplement": bool(old),
            })
            result.update(filled=True, fields=fields,
                          reason="supplemented" if old else "filled",
                          used_snapshot_at=snaps[0].get("collected_at"))
            log.info("fill ok record=%s post=%s window=72h source=%s%s fields=%s",
                     record_id, post_id, snaps[0].get("collected_at"),
                     " (supplement)" if old else "",
                     json.dumps(fields, ensure_ascii=False, sort_keys=True))
            return result

        if old:
            result["reason"] = "already_filled"
            result["fields"] = old.get("fields") or {}
            log.info("fill skip record=%s window=%s: already filled at %s",
                     record_id, window, old.get("at"))
            return result
        # 1h / 24h：以窗口内最早一份为准；若上一轮回填失败（无 fills 账本），
        # 晚到的快照可以触发用最早一份重试，但不会用晚到快照覆盖口径。
        source_snap = snaps[0]
        if source_snap.get("collected_at") != snap.get("collected_at"):
            log.info("fill retry record=%s window=%s: using first snapshot %s (triggered by %s)",
                     record_id, window, source_snap.get("collected_at"), snap.get("collected_at"))
        metrics = _metrics_present(source_snap.get("metrics") or {})
        fields = _fields_for_window(window, metrics)
        if not fields:
            result["reason"] = "no_relevant_metrics"
            log.info("fill skip record=%s window=%s: no relevant metrics in snapshot",
                     record_id, window)
            return result
        update_record(PUBLISH_TABLE, record_id, fields)
        _append_jsonl(FILLS_FILE, {
            "at": iso_beijing(now_beijing()),
            "record_id": record_id,
            "post_id": post_id,
            "window": window,
            "collected_at": source_snap.get("collected_at"),
            "fields": fields,
        })
        result.update(filled=True, fields=fields, reason="filled",
                      used_snapshot_at=source_snap.get("collected_at"))
        log.info("fill ok record=%s post=%s window=%s source=%s fields=%s",
                 record_id, post_id, window, source_snap.get("collected_at"),
                 json.dumps(fields, ensure_ascii=False, sort_keys=True))
        return result


# --------------------------------------------------------------------------
# 业务：帖子列表匹配 / 单帖指标 / 粉丝
# --------------------------------------------------------------------------
def _own_post(item: dict) -> tuple[str | None, str | None, str]:
    """校验是 @your_handle 本人帖子，返回 (post_id, url, text)。"""
    url = extract_url(item.get("url") or item.get("link"))
    pid = str(item.get("id") or item.get("post_id") or "").strip()
    if not pid and url:
        m = STATUS_ID_RE.search(url)
        pid = m.group(1) if m else ""
    handle = str(item.get("author") or item.get("screen_name") or "").lstrip("@")
    url_handle = ""
    if url:
        m = STATUS_URL_RE.search(url)
        url_handle = m.group(1) if m else ""
    if not handle:
        handle = url_handle
    if handle and handle.lower() != TARGET_ACCOUNT.lower():
        return None, None, ""
    if url_handle and url_handle.lower() != TARGET_ACCOUNT.lower():
        return None, None, ""
    if not pid:
        return None, None, ""
    canonical = f"https://x.com/{TARGET_ACCOUNT}/status/{pid}"
    text = str(item.get("text") or item.get("full_text") or "")
    return pid, canonical, text


def match_posts_report(items: list[dict]) -> dict:
    """帖子列表上报 -> 对状态=已发布且帖子链接为空的记录做正文+时间窗匹配。"""
    rows = list_records(PUBLISH_TABLE, PUBLISH_FIELDS)
    summary = {"received": len(items), "matched": 0, "skipped": 0, "ambiguous": 0,
               "no_match": 0, "details": []}
    for item in items:
        pid, url, text = _own_post(item)
        if not pid:
            summary["skipped"] += 1
            summary["details"].append({"post_id": item.get("id"), "result": "not_own_post"})
            log.warning("posts skip: not @%s own post or missing id (url=%s)",
                        TARGET_ACCOUNT, item.get("url"))
            continue
        created = parse_dt(item.get("created_at") or item.get("createdAt"))
        if created is None:
            summary["skipped"] += 1
            summary["details"].append({"post_id": pid, "result": "missing_created_at"})
            log.warning("posts skip post=%s: missing created_at", pid)
            continue
        scored: list[tuple[float, dict]] = []
        for row in rows:
            if record_status(row) != "已发布":
                continue
            if record_link(row):
                continue
            pub = record_published_at(row)
            if pub is None:
                continue
            if abs((pub - created).total_seconds()) > 24 * 3600:
                continue
            sim = max(text_similarity(text, record_text(row)),
                      text_similarity(text, row.get("标题/首句")))
            scored.append((sim, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_sim, best_row = (scored[0] if scored else (0.0, None))
        second_sim = scored[1][0] if len(scored) > 1 else 0.0
        threshold = 0.80
        if best_row is None or best_sim < threshold:
            summary["no_match"] += 1
            summary["details"].append({"post_id": pid, "result": "no_match",
                                       "best_similarity": round(best_sim, 4)})
            log.info("posts no_match post=%s best_sim=%.4f candidates=%d",
                     pid, best_sim, len(scored))
            continue
        if second_sim >= threshold and (best_sim - second_sim) < 0.08:
            summary["ambiguous"] += 1
            summary["details"].append({
                "post_id": pid, "result": "ambiguous",
                "best_record": best_row["_record_id"],
                "best_similarity": round(best_sim, 4),
                "second_record": scored[1][1]["_record_id"],
                "second_similarity": round(second_sim, 4),
            })
            log.warning("posts ambiguous post=%s best=%s(%.4f) second=%s(%.4f) -> skip",
                        pid, best_row["_record_id"], best_sim,
                        scored[1][1]["_record_id"], second_sim)
            continue
        update_record(PUBLISH_TABLE, best_row["_record_id"], {"帖子链接": url})
        best_row["帖子链接"] = url
        summary["matched"] += 1
        summary["details"].append({
            "post_id": pid, "result": "matched",
            "record_id": best_row["_record_id"],
            "similarity": round(best_sim, 4),
            "published_at": iso_beijing(record_published_at(best_row)),
        })
        log.info("posts matched post=%s -> record=%s sim=%.4f url=%s",
                 pid, best_row["_record_id"], best_sim, url)
    return summary


def find_records_by_post_id(post_id: str) -> list[dict]:
    rows = list_records(PUBLISH_TABLE, PUBLISH_FIELDS)
    return [r for r in rows if post_id_from_url(r.get("帖子链接")) == str(post_id)]


def handle_metric_report(*, post_id: str, metrics: dict, captured_at: datetime,
                         source: str, raw: dict | None = None,
                         record: dict | None = None) -> dict:
    if record is None:
        matches = find_records_by_post_id(post_id)
        if len(matches) > 1:
            log.warning("metrics ambiguous post=%s records=%s -> skip",
                        post_id, [m["_record_id"] for m in matches])
            return {"ok": False, "reason": "ambiguous_record", "post_id": post_id,
                    "records": [m["_record_id"] for m in matches]}
        if not matches:
            log.info("metrics no_record post=%s -> snapshot not stored", post_id)
            return {"ok": False, "reason": "record_not_found", "post_id": post_id}
        record = matches[0]
    pub = record_published_at(record)
    snap, dup = append_snapshot(
        record_id=record["_record_id"], post_id=str(post_id), published_at=pub,
        captured_at=captured_at, metrics=metrics, raw=raw, source=source,
    )
    fill = apply_window_fill(snap, record)
    return {"ok": True, "duplicate": dup, "snapshot": snap, "fill": fill,
            "record_id": record["_record_id"]}


def handle_posts_report(items: list[dict]) -> dict:
    return match_posts_report(items)


def _daily_rows() -> list[dict]:
    return list_records(DAILY_TABLE, DAILY_FIELDS)


def handle_account_report(*, account: str, followers: int | None,
                          verified_followers: int | None,
                          captured_at: datetime, source: str = "ingest") -> dict:
    """写账号日快照。

    粉丝总数只认 fx_poll（source="fx_poll"）。扩展 ingest 上报的 followers
    一律忽略，只接受 verified_followers 补写认证粉丝数；当日行不存在时仍可
    建行，但粉丝总数留空，等 fx_poll 补。
    """
    acct = normalize_account(account)
    if acct != TARGET_ACCOUNT_AT:
        log.warning("account skip: %s is not %s", acct, TARGET_ACCOUNT_AT)
        return {"ok": False, "reason": "not_target_account", "account": acct}
    dk = date_key(captured_at)
    ignored_followers = None
    if source != "fx_poll" and followers is not None:
        ignored_followers = followers
        log.warning(
            "account followers ignored date=%s account=%s source=%s followers=%s "
            "(粉丝总数只认 fx_poll，扩展上报不写总数，等 fx_poll 补)",
            dk, acct, source, followers,
        )
        followers = None
    if followers is None and verified_followers is None:
        return {"ok": False, "reason": "no_metrics", "account": acct,
                "date": dk, "ignored_followers": ignored_followers}
    rows = _daily_rows()
    existing = None
    for row in rows:
        row_date = parse_dt(row.get("日期"))
        if row_date and date_key(row_date) == dk and normalize_account(row.get("账号")) == acct:
            existing = row
            break
    if existing:
        updates: dict[str, Any] = {}
        if followers is not None and existing.get("粉丝总数") is None:
            updates["粉丝总数"] = followers
        if verified_followers is not None and existing.get("认证粉丝数") is None:
            updates["认证粉丝数"] = verified_followers
        if updates:
            update_record(DAILY_TABLE, existing["_record_id"], updates)
            log.info("account duplicate date=%s account=%s -> no new row, enriched %s",
                     dk, acct, json.dumps(updates, ensure_ascii=False))
            return {"ok": True, "duplicate": True, "created": False,
                    "record_id": existing["_record_id"], "date": dk, "enriched": updates,
                    "ignored_followers": ignored_followers}
        log.info("account duplicate date=%s account=%s -> no new row", dk, acct)
        return {"ok": True, "duplicate": True, "created": False,
                "record_id": existing["_record_id"], "date": dk, "enriched": {},
                "ignored_followers": ignored_followers}
    fields: dict[str, Any] = {"日期": f"{dk} 00:00", "账号": [acct]}
    if followers is not None:
        fields["粉丝总数"] = followers
    if verified_followers is not None:
        fields["认证粉丝数"] = verified_followers
    rids = create_records(DAILY_TABLE, [fields])
    log.info("account created date=%s account=%s followers=%s verified=%s record=%s",
             dk, acct, followers, verified_followers, rids[0] if rids else None)
    return {"ok": True, "duplicate": False, "created": True,
            "record_id": rids[0] if rids else None, "date": dk,
            "followers": followers, "verified_followers": verified_followers,
            "ignored_followers": ignored_followers}


def find_record(record_id: str) -> dict | None:
    rows = list_records(PUBLISH_TABLE, PUBLISH_FIELDS)
    for row in rows:
        if row["_record_id"] == record_id:
            return row
    return None


__all__ = [
    "BASE_TOKEN", "PUBLISH_TABLE", "DAILY_TABLE", "TARGET_ACCOUNT", "TARGET_ACCOUNT_AT",
    "configure_logging", "now_beijing", "parse_dt", "iso_beijing", "date_key",
    "parse_number", "extract_url", "post_id_from_url", "normalize_account",
    "normalize_text", "text_similarity", "run_lark", "list_records", "update_record",
    "create_records", "delete_records", "record_status", "record_link",
    "record_published_at", "record_text", "append_snapshot", "apply_window_fill",
    "match_posts_report", "find_records_by_post_id", "handle_metric_report",
    "handle_posts_report", "handle_account_report", "find_record", "load_snapshots",
    "load_fills", "window_for",
]
