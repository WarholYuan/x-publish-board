#!/usr/bin/env python3
"""X 发布板自检（doctor）：卡住时先跑它，别猜。

按这套系统真实出过事的顺序逐项验（飞书授权 → 配置 → 运行环境 → 回采 → 令牌与服务），
每项给一句人话结论和修法，最后生成一行「诊断码」——把诊断码和报告文件发给
帮你的人，对方不用看你的机器就知道坏在哪。

用法：
  python3 scripts/doctor.py                      # 自动找 config.env（当前目录 → /opt/xboard → /opt/xcollect）
  python3 scripts/doctor.py --config /path/config.env
  python3 scripts/doctor.py --out /path/report.txt   # 报告落盘位置（默认 ./doctor-report.txt）

只读：不改飞书任何数据、不动任何服务。唯一一个 POST 是拿假令牌试回采接口，
会被服务端当场拒绝（403），就是要验「拒绝得对不对」。
只用 Python 标准库，零依赖。退出码：有 FAIL 为 1，全 OK/WARN/SKIP 为 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OK, FAIL, WARN, SKIP = "OK", "FAIL", "WARN", "SKIP"
SYMBOL = {OK: "✓", FAIL: "✗", WARN: "⚠", SKIP: "–"}

REQUIRED_KEYS = [
    "FEISHU_BASE_TOKEN", "FEISHU_PUBLISH_TABLE_ID", "FEISHU_TOPIC_TABLE_ID",
    "FEISHU_DAILY_TABLE_ID", "FEISHU_BENCHMARK_TABLE_ID", "X_TARGET_ACCOUNT",
]


class Result:
    def __init__(self, no: int, title: str):
        self.no, self.title = no, title
        self.status = OK
        self.lines: list[str] = []
        self.fix: str = ""

    def add(self, text: str) -> None:
        self.lines.append(text)

    def fail(self, text: str, fix: str = "") -> None:
        self.status = FAIL
        self.add(text)
        if fix:
            self.fix = fix

    def warn(self, text: str, fix: str = "") -> None:
        if self.status != FAIL:
            self.status = WARN
        self.add(text)
        if fix and not self.fix:
            self.fix = fix


def parse_config(path: Path) -> tuple[dict, list[str]]:
    """读 config.env，返回 (键值, shell 加载会出问题的行号列表)。"""
    cfg: dict[str, str] = {}
    bad_lines: list[str] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # shell 安全性：值里有空格又没被引号包住，source 时会断
        if re.search(r"\s", value) and not (
                (value.startswith("'") and value.endswith("'")) or
                (value.startswith('"') and value.endswith('"'))):
            bad_lines.append(f"第 {i} 行 {key}：值里有空格但没加引号")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        cfg[key] = value
    return cfg, bad_lines


def run(cmd: list[str], timeout: int = 30, env: dict | None = None,
        cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=cwd)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def http(method: str, url: str, timeout: int = 8,
         headers: dict | None = None, body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2048).decode("utf-8", "replace")
    except OSError as e:
        return -1, str(e)


def lark_base(cfg: dict, *args: str, timeout: int = 30,
              env: dict | None = None, cwd: str | None = None) -> tuple[int, str]:
    lark = cfg.get("LARK_CLI", "lark-cli")
    return run([lark, "base", *args, "--as", "user", "--format", "json"],
               timeout=timeout, env=env, cwd=cwd)


# ---------------------------------------------------------------- 检查 1：飞书授权
def check_auth(cfg: dict) -> Result:
    r = Result(1, "飞书授权（装的时候要本人扫码两轮，且会过期）")
    lark = cfg.get("LARK_CLI", "lark-cli")
    if not (Path(lark).exists() or shutil.which(lark)):
        r.fail(f"lark-cli 找不到：{lark}",
               "先装 lark-cli 并把它的实际路径填进 config.env 的 LARK_CLI。")
        return r
    code, out = run([lark, "auth", "status"], timeout=20)
    try:
        data = json.loads(out[out.index("{"):])
        user = (data.get("identities") or {}).get("user") or {}
    except (ValueError, AttributeError):
        user = {}
    if not user.get("available"):
        r.fail("飞书用户身份没登录（或已失效）。",
               "本人在这台机器上跑一次：lark-cli auth login，按提示扫码授权。")
        return r
    ts = user.get("tokenStatus", "")
    if ts and ts != "valid":
        r.fail(f"飞书授权状态是 {ts}，不是 valid。",
               "本人在这台机器上重新跑：lark-cli auth login 扫码。")
        return r
    r.add(f"lark-cli 在，用户身份可用，令牌状态 {ts or 'valid'}。")
    r.add("提醒：授权会过期，过期后板子会静默停更——板子不动了先跑本自检。")
    return r


# ---------------------------------------------------------------- 检查 2：配置
def check_config(cfg: dict, bad_lines: list[str], cfg_path: Path) -> Result:
    r = Result(2, "配置值（6 个值全靠手抄，抄错不报错、只是行为不对）")
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        r.fail(f"config.env 里这些值是空的：{'、'.join(missing)}",
               "照 docs/prerequisites.md 把飞书 Base token、四张表 ID、X 账号填齐。")
    for b in bad_lines:
        r.fail(f"配置格式坑：{b}",
               "把这一行的值用单引号整体包住，例如 X_DISPLAY_NAMES_JSON='{...}'。")
    raw_json = cfg.get("X_DISPLAY_NAMES_JSON", "")
    if raw_json:
        try:
            json.loads(raw_json)
        except ValueError:
            r.fail("X_DISPLAY_NAMES_JSON 不是合法 JSON。",
                   '改成这个形状：\'{"@你的handle": "你的昵称"}\'（含单引号）。')
    handle = cfg.get("X_TARGET_ACCOUNT", "")
    if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle.lstrip("@")):
        r.warn(f"X 账号写法可疑：{handle}（应为不带 @ 的 handle）。")
    domain, site = cfg.get("SITE_DOMAIN", ""), cfg.get("SITE_URL", "")
    if domain and site and domain not in site:
        r.warn(f"SITE_URL（{site}）里不含 SITE_DOMAIN（{domain}），两处可能填岔了。")
    if r.status == OK:
        r.add("必填值齐全，JSON 合法，没有未加引号的空格坑。")
    # 四张表是否真的存在于这个 Base 里（顺带验 Base token 对不对）
    if not missing:
        code, out = lark_base(cfg, "+table-list",
                              "--base-token", cfg["FEISHU_BASE_TOKEN"])
        if code != 0:
            r.fail("拿 config 里的 Base token 列不出表，Base token 多半填错或授权没覆盖这个 Base。",
                   "重新从飞书 Base 链接 /base/ 后面复制 token，并确认 lark-cli 登录的是有权限的账号。")
        else:
            absent = [k for k in REQUIRED_KEYS[1:5] if cfg[k] not in out]
            if absent:
                names = {"FEISHU_PUBLISH_TABLE_ID": "发布库",
                         "FEISHU_TOPIC_TABLE_ID": "选题池",
                         "FEISHU_DAILY_TABLE_ID": "账号日快照",
                         "FEISHU_BENCHMARK_TABLE_ID": "对标账号"}
                r.fail("这些表的 ID 在 Base 里找不到：" +
                       "、".join(names[k] for k in absent),
                       "在飞书里打开对应的表，用 lark-cli base +table-list 重新对一遍 ID。")
            else:
                r.add("四张表的 ID 在 Base 里都对得上。")
            floors_id = cfg.get("FEISHU_FLOORS_TABLE_ID", "")
            if floors_id:
                if floors_id in out:
                    r.add("楼层表的 ID 也对得上，串帖逐楼功能开着。")
                else:
                    r.fail("楼层表（FEISHU_FLOORS_TABLE_ID）的 ID 在 Base 里找不到。",
                           "在飞书里确认「楼层」表还在，用 lark-cli base +table-list 重新对一遍 ID；"
                           "不用串帖逐楼就把这一项清空，发布板会退回旧模式。")
            else:
                r.add("楼层表未配置：串帖逐楼功能关着，整串写在发布库「串/首条回复」里也能发（旧模式）。")
    _ = cfg_path
    return r


# ---------------------------------------------------------------- 检查 3：运行环境
def check_env(cfg: dict, config_ok: bool) -> Result:
    r = Result(3, "运行环境（手动跑得通 ≠ 定时/后台跑得通）")
    if not config_ok:
        r.status = SKIP
        r.add("配置那关没过（检查 2），先把配置修好再验环境——不然验出来的是配置的错，会误导。")
        return r
    lark = cfg.get("LARK_CLI", "lark-cli")
    lark_dir = str(Path(lark).resolve().parent) if "/" in lark else ""
    clean_env = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": ":".join(p for p in [lark_dir, "/usr/local/bin", "/usr/bin", "/bin"] if p),
    }
    code, out = lark_base(
        cfg, "+record-list", "--base-token", cfg.get("FEISHU_BASE_TOKEN", ""),
        "--table-id", cfg.get("FEISHU_PUBLISH_TABLE_ID", ""), "--limit", "1",
        env=clean_env, cwd="/tmp")
    if code != 0:
        tail = out.strip().splitlines()[-1][:160] if out.strip() else ""
        r.fail("在「干净环境」（只有 HOME/PATH、从 /tmp 目录）下读飞书失败，"
               "这正是定时任务跑不通的样子。",
               "给定时/后台任务补齐 HOME 与 PATH（把 lark-cli 所在目录加进 PATH），"
               f"并确认不是在受保护目录里跑。原始报错尾巴：{tail}")
    else:
        r.add("干净环境 + 换目录实测能读到发布库，定时通道这一关过。")
    out_dir = cfg.get("XBOARD_OUT_DIR", "")
    if out_dir:
        p = Path(out_dir)
        if not p.exists():
            r.warn(f"板页输出目录还不存在：{out_dir}（渲染还没成功跑过？）",
                   "先手动跑一次 render_board.py，看它报什么。")
        elif not os.access(p, os.W_OK):
            r.warn(f"板页输出目录不可写：{out_dir}")
    return r


# ---------------------------------------------------------------- 检查 4：回采
def check_collect(cfg: dict) -> Result:
    r = Result(4, "回采链路（不匹配是静默的，日志里才看得见）")
    handle = cfg.get("X_TARGET_ACCOUNT", "").lstrip("@")
    if handle:
        code, out = http("GET", f"https://api.fxtwitter.com/{handle}", timeout=12)
        if code == 200 and '"followers"' in out:
            r.add("FxTwitter 公开接口可达，能读到你的账号资料（兜底回采的源头是活的）。")
        else:
            r.warn(f"FxTwitter 现在读不到你的账号（HTTP {code}）——第三方接口，"
                   "它挂了公开数据就回填不了，等它恢复或换官方 API。",
                   "先确认 handle 没填错；没错就是对方服务问题，过一阵再跑自检。")
    state_dir = Path(cfg.get("XCOLLECT_STATE_DIR", "/opt/xcollect"))
    candidates = [state_dir / "fx_poll.log", Path("data/logs/fx_poll.log"),
                  Path("/opt/xcollect/fx_poll.log")]
    log = next((c for c in candidates if c.is_file()), None)
    if log is None:
        r.status = SKIP if r.status == OK else r.status
        r.add("没找到 fx_poll.log，跳过回采日志检查（兜底脚本还没跑过？）。")
        return r
    age_h = (time.time() - log.stat().st_mtime) / 3600
    text = log.read_text(encoding="utf-8", errors="replace")
    done_lines = [ln for ln in text.splitlines() if "fx_poll done" in ln]
    if age_h > 3:
        r.warn(f"fx_poll.log 已经 {age_h:.0f} 小时没更新了——兜底脚本没在按小时跑。",
               "查定时任务（cron / launchd / 任务计划）是否还在，手动跑一次 fx_poll.py 看报错。")
    else:
        r.add(f"fx 日志 {age_h:.1f} 小时内有更新，兜底在跑。")
    if done_lines:
        last = done_lines[-1].split("fx_poll done")[-1].strip()
        r.add(f"最近一轮兜底结果原文：{last[:200]}")
        try:
            stats = json.loads(last[last.index("{"):])
        except ValueError:
            stats = {}
        posts = stats.get("posts") or 0
        matched = (stats.get("auto_matched") or 0) + (stats.get("fills") or 0)
        if posts and not matched:
            r.warn(f"最近一轮有 {posts} 条帖子参与匹配却一条都没对上。",
                   "多半是发帖后大改了正文（相似度不够），或帖子链接还没贴回飞书；"
                   "先手动把一条帖子链接贴回飞书发布库，再跑一轮看日志。")
    return r


# ---------------------------------------------------------------- 检查 5：令牌与服务
def _token_file(r: Result, cfg: dict, key: str, label: str) -> None:
    p = Path(cfg.get(key, ""))
    if not str(p):
        return
    if not p.is_file() or p.stat().st_size < 10:
        r.fail(f"{label}令牌文件不存在或为空：{p}",
               "让对应服务自己生成：删掉空文件后重启服务，它会重建（权限应为 600）。")
        return
    mode = p.stat().st_mode & 0o077
    if mode:
        r.warn(f"{label}令牌文件权限过松（{oct(p.stat().st_mode & 0o777)}），应为 600。",
               f"chmod 600 {p}")


def _health(r: Result, name: str, port: int, path: str = "/health") -> bool:
    code, _ = http("GET", f"http://127.0.0.1:{port}{path}", timeout=5)
    if code == 200:
        r.add(f"{name}在本机 {port} 端口活着。")
        return True
    r.fail(f"{name}（127.0.0.1:{port}）没响应——服务没在跑。",
           "把服务起起来（服务器版：systemctl status 查；本地版：重跑 start_local 脚本）。")
    return False


def check_tokens_services(cfg: dict) -> Result:
    r = Result(5, "令牌与服务（不一致就是 403，用户侧只看到「点了没反应」）")
    _token_file(r, cfg, "XBOARD_TOKEN_FILE", "板页状态")
    _token_file(r, cfg, "XCOLLECT_TOKEN_FILE", "回采")
    state_port = int(cfg.get("XBOARD_STATE_PORT", "8791"))
    collect_port = int(cfg.get("XCOLLECT_PORT", "8795"))
    _health(r, "状态服务", state_port)
    if _health(r, "回采接收端", collect_port):
        code, _ = http("POST", f"http://127.0.0.1:{collect_port}/ingest",
                       headers={"Content-Type": "application/json",
                                "X-Collect-Token": "doctor-wrong-token"},
                       body=b"{}")
        if code == 403:
            r.add("拿假令牌试上报被正确拒绝（403）——鉴权在工作，真令牌不一致时也会这样，先对令牌。")
        else:
            r.warn(f"拿假令牌试上报返回了 {code}（预期 403），鉴权可能没生效，去看 ingest 服务日志。")
    if cfg.get("XLOCAL_PORT"):
        code, _ = http("GET", f"http://127.0.0.1:{cfg['XLOCAL_PORT']}/health",
                       timeout=5)
        if code == 200:
            r.add("本地网关活着。")
        else:
            r.warn("config 里配了本地网关端口但网关没响应——本地模式要先起 gateway。",
                   "重跑 start_local 脚本，或单独 python3 local/gateway.py。")
    return r


def find_config(arg: str | None) -> Path | None:
    if arg:
        p = Path(arg)
        return p if p.is_file() else None
    for cand in [Path("config.env"), Path("/opt/xboard/config.env"),
                 Path("/opt/xcollect/config.env")]:
        if cand.is_file():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="X 发布板自检 doctor")
    ap.add_argument("--config", help="config.env 路径（默认自动找）")
    ap.add_argument("--out", default="doctor-report.txt", help="报告落盘路径")
    args = ap.parse_args()

    cfg_path = find_config(args.config)
    if cfg_path is None:
        print("✗ 找不到 config.env。先复制 config.env.example（服务器版）或 "
              "local/config.local.example（本地版）为 config.env 并填值，"
              "或用 --config 指明路径。")
        return 1
    cfg, bad_lines = parse_config(cfg_path)
    # 让相对路径以 config 所在目录为基准解析一部分检查（令牌/状态目录多为绝对路径，本地版是相对仓库根）
    r_auth = check_auth(cfg)
    r_config = check_config(cfg, bad_lines, cfg_path)
    results = [
        r_auth,
        r_config,
        check_env(cfg, config_ok=r_config.status != FAIL),
        check_collect(cfg),
        check_tokens_services(cfg),
    ]
    digest = hashlib.sha256(
        json.dumps([(x.no, x.status) for x in results]).encode()).hexdigest()[:6]
    code_line = "XPB-DOC1 " + " ".join(f"{x.no}:{x.status}" for x in results) \
        + f" #{digest}"

    lines = [f"X 发布板自检报告（config：{cfg_path}）", ""]
    for x in results:
        lines.append(f"[{SYMBOL[x.status]}] {x.no}. {x.title} —— {x.status}")
        for ln in x.lines:
            lines.append(f"    {ln}")
        if x.fix:
            lines.append(f"    修法：{x.fix}")
    lines += ["", f"诊断码：{code_line}",
              "把这一行诊断码连同本报告文件发给帮你的人，就不用远程看你的机器了。"]
    report = "\n".join(lines) + "\n"
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    print(f"（报告已存到 {args.out}）")
    return 1 if any(x.status == FAIL for x in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
