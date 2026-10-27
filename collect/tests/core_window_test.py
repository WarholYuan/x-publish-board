#!/usr/bin/env python3
"""离线验证共享回填规则：窗口边界、首份口径、失败重试、幂等账本。"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os
os.environ.setdefault('X_TARGET_ACCOUNT', 'YOUR_X_HANDLE')
sys.path.insert(0, os.environ.get('XCOLLECT_DIR', '/opt/xcollect'))
import core

SH = ZoneInfo('Asia/Shanghai')


def main() -> int:
    core.configure_logging('ingest')
    core.log.disabled = True
    tmp = Path(tempfile.mkdtemp(prefix='xcollect-core-test-'))
    core.SNAPSHOTS_FILE = tmp / 'snapshots.jsonl'
    core.FILLS_FILE = tmp / 'fills.jsonl'

    assert core.window_for(0.74) is None
    assert core.window_for(0.75) == '1h'
    assert core.window_for(1.5) == '1h'
    assert core.window_for(1.51) is None
    assert core.window_for(20) == '24h'
    assert core.window_for(28) == '24h'
    assert core.window_for(66) == '72h'
    assert core.window_for(84) == '72h'
    assert core.window_for(84.1) is None
    assert core.iso_beijing(core.parse_dt('Thu Oct 15 05:32:48 +0000 2026')) == '2026-10-15T13:32:48+08:00'

    pub = datetime(2026, 10, 1, 10, 0, tzinfo=SH)
    first_at = pub + timedelta(hours=24)
    second_at = pub + timedelta(hours=25)

    # 第一次回填按预期失败：模拟飞书写失败，不应留下 fills 账本
    calls = {'n': 0, 'fields': None}
    real_update = core.update_record

    def failing_update(table, rid, fields):
        calls['n'] += 1
        calls['fields'] = fields
        raise core.LarkError('simulated feishu failure')

    core.update_record = failing_update
    snap1, dup1 = core.append_snapshot(
        record_id='recTEST', post_id='1000000000000000001', published_at=pub,
        captured_at=first_at, metrics={'views': 100}, raw={'m': 1}, source='fxtwitter')
    assert not dup1 and snap1['window'] == '24h'
    try:
        core.apply_window_fill(snap1)
        raise AssertionError('expected simulated failure')
    except core.LarkError:
        pass
    assert not core.FILLS_FILE.exists(), 'failed fill must not write ledger'
    assert calls['fields'] == {'24小时曝光': 100}

    # 晚到快照触发重试，但必须用窗口内第一份的 100，而不是晚到的 200
    snap2, dup2 = core.append_snapshot(
        record_id='recTEST', post_id='1000000000000000001', published_at=pub,
        captured_at=second_at, metrics={'views': 200}, raw={'m': 2}, source='fxtwitter')
    assert not dup2

    def ok_update(table, rid, fields):
        calls['n'] += 1
        calls['fields'] = fields

    core.update_record = ok_update
    fill = core.apply_window_fill(snap2)
    assert fill['filled'] is True
    assert fill['fields'] == {'24小时曝光': 100}, fill
    assert fill['used_snapshot_at'] == core.iso_beijing(first_at), fill

    # 幂等：同一窗口再调用不再写飞书
    before = calls['n']
    again = core.apply_window_fill(snap2)
    assert again['reason'] == 'already_filled'
    assert calls['n'] == before

    # 完全相同的快照去重
    snap3, dup3 = core.append_snapshot(
        record_id='recTEST', post_id='1000000000000000001', published_at=pub,
        captured_at=first_at, metrics={'views': 100}, raw={'m': 1}, source='fxtwitter')
    assert dup3 is True and snap3['collected_at'] == core.iso_beijing(first_at)

    # 1h 互动 = 赞+藏+转+评；72h 全套 + 已复盘
    assert core._fields_for_window('1h', {'likes': 1, 'bookmarks': 2, 'reposts': 3, 'replies': 4}) == {'1小时互动': 10}
    full = core._fields_for_window('72h', {
        'views': 163, 'likes': 1, 'bookmarks': 0, 'reposts': 0, 'replies': 0,
        'profile_visits': 5, 'link_clicks': 2,
    })
    assert full == {'状态': ['已复盘'], '曝光': 163, '点赞': 1, '收藏': 0,
                    '转发': 0, '回复': 0, '主页访问': 5, '链接点击': 2}, full


    # 72h 定格：先公开指标，后扩展私有字段补齐；后到只补缺，不覆盖已有值
    pub72 = datetime(2026, 10, 5, 10, 0, tzinfo=SH)
    s72a, _ = core.append_snapshot(
        record_id='rec72', post_id='1000000000000000001', published_at=pub72,
        captured_at=pub72 + timedelta(hours=67),
        metrics={'views': 500, 'likes': 5}, raw={}, source='fxtwitter')
    f72a = core.apply_window_fill(s72a)
    assert f72a['filled'] and f72a['fields'] == {'状态': ['已复盘'], '曝光': 500, '点赞': 5}, f72a

    s72b, _ = core.append_snapshot(
        record_id='rec72', post_id='1000000000000000001', published_at=pub72,
        captured_at=pub72 + timedelta(hours=70),
        metrics={'views': 999, 'likes': 99, 'profile_visits': 7, 'link_clicks': 3},
        raw={}, source='ingest')
    f72b = core.apply_window_fill(s72b)
    assert f72b['filled'] and f72b['reason'] == 'supplemented', f72b
    assert f72b['fields'] == {'主页访问': 7, '链接点击': 3}, f72b
    combined = core._fills_for('rec72', '72h')
    assert combined['fields'] == {'状态': ['已复盘'], '曝光': 500, '点赞': 5,
                                  '主页访问': 7, '链接点击': 3}, combined
    f72c = core.apply_window_fill(s72b)
    assert f72c['reason'] == 'already_filled', f72c

    core.update_record = real_update
    print('core_window_test: PASS window bounds, Twitter date parse, first-snapshot retry, 72h supplement, dedupe, ledger idempotency, 1h/72h mapping')
    print('snapshots:')
    print(core.SNAPSHOTS_FILE.read_text().strip())
    print('fills:')
    print(core.FILLS_FILE.read_text().strip())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
