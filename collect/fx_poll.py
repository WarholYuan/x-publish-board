#!/usr/bin/env python3
"""FxTwitter 公开数据兜底轮询。

对发布库中「已有帖子链接、尚未 72h 定格」的记录，按小时拉公开指标写快照，
并复用 core.py 的同一套 1h / 24h / 72h 窗口回填规则。

用法：
  python3 /opt/xcollect/fx_poll.py                 # cron 每小时
  python3 /opt/xcollect/fx_poll.py --dry-run       # 只取数打印，不落盘/不回填
  python3 /opt/xcollect/fx_poll.py --simulate-at 2026-10-15T14:32:48+08:00
  python3 /opt/xcollect/fx_poll.py --skip-account  # 不写账号日快照（测试用）
  python3 /opt/xcollect/fx_poll.py --skip-match    # 不跑主页时间线自动匹配（测试用）

除「已有链接的记录」外，本脚本还会在存在「已发布且链接为空」的近 72h 记录时，
用 FxTwitter `/2/profile/<user>/statuses` 拉最近 20 条帖子做一次公开兜底匹配；
匹配逻辑与扩展上报共用 core.match_posts_report。

幂等：同一记录 30 分钟内重复运行不会重复抓取/写快照；回填由 core 的
record+window 账本保证只写一次。可被 cron 安全重跑。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

FX_STATUS = "https://api.fxtwitter.com/status/{post_id}"
FX_USER = "https://api.fxtwitter.com/{handle}"
FX_TIMELINE = "https://api.fxtwitter.com/2/profile/{handle}/statuses"
LOCK_FILE = Path(os.environ.get("XCOLLECT_FX_LOCK", "/run/xcollect-fx.lock"))
RECENT_SKIP_MINUTES = 30
MAX_AGE_HOURS = 84.0
log = logging.getLogger("xcollect.fx")


def http_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "xcollect-fx/1.0 (+" + os.environ.get("SITE_URL", "https://example.com/x/") + ")",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1024 * 1024)
        if resp.status != 200:
            raise RuntimeError(f"http {resp.status}")
        return json.loads(raw)


def fetch_post(post_id: str) -> dict | None:
    try:
        data = http_json(FX_STATUS.format(post_id=post_id))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError,
            RuntimeError) as exc:
        log.warning("fxtwitter post=%s fetch failed: %s", post_id, exc)
        return None
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        log.warning("fxtwitter post=%s: unexpected payload", post_id)
        return None
    if str(tweet.get("id") or "") != str(post_id):
        log.warning("fxtwitter post=%s: id mismatch (%s)", post_id, tweet.get("id"))
        return None
    author = (tweet.get("author") or {}).get("screen_name")
    if str(author or "").lower() != core.TARGET_ACCOUNT.lower():
        log.warning("fxtwitter post=%s: author=%s is not @%s", post_id, author,
                    core.TARGET_ACCOUNT)
        return None
    return tweet


def tweet_metrics(tweet: dict) -> dict:
    return {
        "views": core.parse_number(tweet.get("views")),
        "likes": core.parse_number(tweet.get("likes")),
        "bookmarks": core.parse_number(tweet.get("bookmarks")),
        "reposts": core.parse_number(tweet.get("retweets")),
        "replies": core.parse_number(tweet.get("replies")),
        "quotes": core.parse_number(tweet.get("quotes")),
    }


def tweet_raw(tweet: dict) -> dict:
    return {
        "source": "fxtwitter",
        "url": tweet.get("url"),
        "id": tweet.get("id"),
        "created_at": tweet.get("created_at"),
        "author": (tweet.get("author") or {}).get("screen_name"),
        "views": tweet.get("views"),
        "likes": tweet.get("likes"),
        "bookmarks": tweet.get("bookmarks"),
        "retweets": tweet.get("retweets"),
        "replies": tweet.get("replies"),
        "quotes": tweet.get("quotes"),
    }



def fetch_timeline() -> list[dict]:
    """FxTwitter 公开主页时间线，仅用于「空链接记录」的兜底匹配。"""
    try:
        data = http_json(FX_TIMELINE.format(handle=core.TARGET_ACCOUNT))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError,
            RuntimeError) as exc:
        log.warning("fxtwitter timeline fetch failed: %s", exc)
        return []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        log.warning("fxtwitter timeline: unexpected payload")
        return []
    items = []
    for r in results:
        if not isinstance(r, dict) or r.get("type") not in (None, "status"):
            continue
        author = (r.get("author") or {}).get("screen_name")
        if str(author or "").lower() != core.TARGET_ACCOUNT.lower():
            continue
        if not r.get("id") or not r.get("text"):
            continue
        items.append({
            "id": str(r.get("id")),
            "url": r.get("url") or f"https://x.com/{core.TARGET_ACCOUNT}/status/{r.get('id')}",
            "text": str(r.get("text") or ""),
            "created_at": r.get("created_at"),
            "author": core.TARGET_ACCOUNT,
        })
    return items


def match_recent_timeline(rows: list[dict], now: datetime, dry_run: bool) -> int:
    """对近 72h、已发布且帖子链接为空的记录，用公开时间线做一次匹配兜底。"""
    candidates = []
    for row in rows:
        if core.record_status(row) != "已发布" or core.record_link(row):
            continue
        pub = core.record_published_at(row)
        if pub is None:
            continue
        if abs((now - pub).total_seconds()) > 72 * 3600:
            continue
        candidates.append(row)
    if not candidates:
        return 0
    items = fetch_timeline()
    if not items:
        return 0
    log.info("timeline match: %d empty-link records, %d public posts",
             len(candidates), len(items))
    if dry_run:
        return 0
    summary = core.match_posts_report(items)
    log.info("timeline match done matched=%d no_match=%d ambiguous=%d skipped=%d",
             summary["matched"], summary["no_match"], summary["ambiguous"], summary["skipped"])
    return int(summary["matched"])


def recent_fx_snapshot(record_id: str, at: datetime) -> dict | None:
    for snap in core.load_snapshots():
        if snap.get("record_id") != record_id or snap.get("source") != "fxtwitter":
            continue
        ts = core.parse_dt(snap.get("collected_at"))
        if ts and abs((at - ts).total_seconds()) < RECENT_SKIP_MINUTES * 60:
            return snap
    return None


def maybe_account_snapshot(now: datetime, dry_run: bool) -> dict | None:
    """每天最多一行：若当天已有 @your_handle 行则跳过，否则用公开主页 followers 建行。

    verified_followers 公开接口没有，留给扩展的 Analytics 上报补写（core 会补空字段）。
    """
    try:
        rows = core.list_records(core.DAILY_TABLE, core.DAILY_FIELDS)
    except core.LarkError as exc:
        log.warning("account snapshot: list daily failed: %s", exc)
        return None
    today = core.date_key(now)
    for row in rows:
        dt = core.parse_dt(row.get("日期"))
        if dt and core.date_key(dt) == today and core.normalize_account(row.get("账号")) == core.TARGET_ACCOUNT_AT:
            if row.get("粉丝总数") is not None:
                log.info("account snapshot: %s already exists, skip", today)
                return None
            # 扩展先建了行但总数留空（ingest 的 followers 被防护忽略）：
            # 继续走下面的 FxTwitter 取数，由 core 的「只补空字段」逻辑补上总数。
            log.info("account snapshot: %s exists with empty 粉丝总数, fill from fxtwitter", today)
            break
    try:
        data = http_json(FX_USER.format(handle=core.TARGET_ACCOUNT))
    except Exception as exc:  # noqa: BLE001
        log.warning("account snapshot: fxtwitter user fetch failed: %s", exc)
        return None
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        log.warning("account snapshot: unexpected user payload")
        return None
    followers = core.parse_number(user.get("followers"))
    if followers is None:
        return None
    if dry_run:
        log.info("account snapshot dry-run: date=%s followers=%s", today, followers)
        return {"date": today, "followers": followers}
    result = core.handle_account_report(
        account=core.TARGET_ACCOUNT_AT, followers=followers, verified_followers=None,
        captured_at=now, source="fx_poll",
    )
    return result


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another fx_poll is running, exit")
        return None
    return fh


def poll_floors(dry_run: bool) -> dict:
    """楼层表逐楼回填：对填了「本楼链接」的楼层行，用 FxTwitter 拉最新赞/收藏/回复写回。

    口径与发布库不同：楼层不分 1/24/72h 窗口，每轮刷新为最新快照，值没变不写。
    未配置楼层表（FEISHU_FLOORS_TABLE_ID 为空）时直接跳过。
    """
    stats = {"floors": 0, "fetched": 0, "updated": 0, "failed": 0}
    if not core.FLOORS_TABLE:
        return stats
    try:
        rows = core.list_records(core.FLOORS_TABLE, core.FLOOR_FIELDS)
    except core.LarkError as exc:
        log.warning("floors: list failed: %s", exc)
        return stats
    for row in rows:
        pid = core.post_id_from_url(row.get("本楼链接"))
        if not pid:
            continue
        stats["floors"] += 1
        tweet = fetch_post(pid)
        if tweet is None:
            stats["failed"] += 1
            continue
        stats["fetched"] += 1
        m = tweet_metrics(tweet)
        want = {"点赞": m["likes"], "收藏": m["bookmarks"], "回复": m["replies"]}
        changed = {k: v for k, v in want.items()
                   if v is not None and core.parse_number(row.get(k)) != v}
        if not changed:
            continue
        log.info("floors: record=%s post=%s update=%s", row["_record_id"], pid, changed)
        if dry_run:
            stats["updated"] += 1
            continue
        try:
            core.update_record(core.FLOORS_TABLE, row["_record_id"], changed)
            stats["updated"] += 1
        except core.LarkError as exc:
            log.error("floors: write record=%s failed: %s", row["_record_id"], exc)
            stats["failed"] += 1
        time.sleep(0.4)
    log.info("floors done %s", json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--simulate-at", help="模拟采集时刻（ISO8601），仅用于窗口映射自测")
    parser.add_argument("--skip-account", action="store_true", help="不写账号日快照")
    parser.add_argument("--skip-match", action="store_true", help="不跑公开时间线兜底匹配")
    parser.add_argument("--post-id", help="只处理指定 post id")
    args = parser.parse_args()

    core.configure_logging("fx")
    lock = acquire_lock()
    if lock is None:
        return 0
    if args.simulate_at:
        now = core.parse_dt(args.simulate_at)
        if now is None:
            log.error("invalid --simulate-at: %s", args.simulate_at)
            return 2
        log.info("simulate mode: captured_at=%s", core.iso_beijing(now))
    else:
        now = core.now_beijing()

    try:
        rows = core.list_records(core.PUBLISH_TABLE, core.PUBLISH_FIELDS)
    except core.LarkError as exc:
        log.error("list publish records failed: %s", exc)
        return 1

    auto_matched = 0
    if not args.skip_match and not args.post_id:
        try:
            auto_matched = match_recent_timeline(rows, now, args.dry_run)
        except core.LarkError as exc:
            log.warning("timeline match failed: %s", exc)
        if auto_matched:
            try:
                rows = core.list_records(core.PUBLISH_TABLE, core.PUBLISH_FIELDS)
            except core.LarkError as exc:
                log.error("reload publish records failed: %s", exc)
                return 1

    targets: dict[str, list[dict]] = {}
    for row in rows:
        pid = core.post_id_from_url(row.get("帖子链接"))
        if not pid:
            continue
        if args.post_id and pid != args.post_id:
            continue
        pub = core.record_published_at(row)
        if pub is None:
            log.info("skip record=%s post=%s: no 发布时间", row["_record_id"], pid)
            continue
        hours = (now - pub).total_seconds() / 3600.0
        if hours < -0.25:
            log.info("skip record=%s post=%s: published in future (%.2fh)",
                     row["_record_id"], pid, hours)
            continue
        if hours > MAX_AGE_HOURS:
            if core.record_status(row) != "已复盘":
                log.warning("skip record=%s post=%s: %.1fh > %.0fh and not 已复盘",
                            row["_record_id"], pid, hours, MAX_AGE_HOURS)
            continue
        if core.record_status(row) == "已复盘":
            log.info("skip record=%s post=%s: already 已复盘", row["_record_id"], pid)
            continue
        targets.setdefault(pid, []).append(row)

    log.info("fx_poll start captured=%s records=%d posts=%d",
             core.iso_beijing(now), len(rows), len(targets))
    stats = {"posts": len(targets), "auto_matched": auto_matched, "fetched": 0,
             "snapshots": 0, "duplicates": 0, "fills": 0, "failed": 0,
             "skipped_recent": 0}
    for post_id, recs in targets.items():
        first = recs[0]
        if recent_fx_snapshot(first["_record_id"], now):
            stats["skipped_recent"] += 1
            log.info("skip post=%s: fxtwitter snapshot within %dmin",
                     post_id, RECENT_SKIP_MINUTES)
            continue
        tweet = fetch_post(post_id)
        if tweet is None:
            stats["failed"] += 1
            continue
        stats["fetched"] += 1
        metrics = tweet_metrics(tweet)
        log.info("fetched post=%s metrics=%s", post_id,
                 json.dumps(metrics, ensure_ascii=False, sort_keys=True))
        if args.dry_run:
            continue
        for row in recs:
            try:
                result = core.handle_metric_report(
                    post_id=post_id, metrics=metrics, captured_at=now,
                    source="fxtwitter", raw=tweet_raw(tweet), record=row,
                )
            except core.LarkError as exc:
                log.error("write record=%s post=%s failed: %s", row["_record_id"], post_id, exc)
                stats["failed"] += 1
                continue
            if result.get("duplicate"):
                stats["duplicates"] += 1
            else:
                stats["snapshots"] += 1
            fill = result.get("fill") or {}
            if fill.get("filled"):
                stats["fills"] += 1
        time.sleep(0.4)

    if not args.post_id:
        stats["floors"] = poll_floors(args.dry_run)

    if not args.skip_account:
        if args.dry_run:
            maybe_account_snapshot(now, True)
        else:
            try:
                maybe_account_snapshot(now, False)
            except core.LarkError as exc:
                log.warning("account snapshot failed: %s", exc)

    log.info("fx_poll done %s", json.dumps(stats, ensure_ascii=False, sort_keys=True))
    print(json.dumps({"ok": True, "captured_at": core.iso_beijing(now), **stats},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
