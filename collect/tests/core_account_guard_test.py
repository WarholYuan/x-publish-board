#!/usr/bin/env python3
"""离线验证粉丝总数来源防护。

规则：
- 扩展 ingest 上报的 followers 一律忽略，只接受 verified_followers 补认证数；
- ingest 当日无行时仍可建行，但粉丝总数留空；
- fx_poll（source="fx_poll"）的 followers 照常写入，并可补 ingest 留下的空总数。
"""
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import os
os.environ.setdefault('X_TARGET_ACCOUNT', 'YOUR_X_HANDLE')
sys.path.insert(0, os.environ.get('XCOLLECT_DIR', '/opt/xcollect'))
import core

SH = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 10, 27, 4, 0, tzinfo=SH)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def main() -> int:
    capture = Capture()
    core.log.addHandler(capture)
    core.log.setLevel(logging.INFO)

    state = {'rows': [], 'updates': [], 'created': []}

    def fake_list(table, fields):
        assert table == core.DAILY_TABLE, table
        assert list(fields) == core.DAILY_FIELDS, fields
        return [dict(r) for r in state['rows']]

    def fake_update(table, rid, fields):
        assert table == core.DAILY_TABLE, table
        state['updates'].append((rid, dict(fields)))
        for row in state['rows']:
            if row['_record_id'] == rid:
                row.update(fields)

    def fake_create(table, batch):
        assert table == core.DAILY_TABLE, table
        assert len(batch) == 1, batch
        state['created'].append(dict(batch[0]))
        return ['recNEW']

    real = (core.list_records, core.update_record, core.create_records)
    core.list_records = fake_list
    core.update_record = fake_update
    core.create_records = fake_create
    try:
        # 1. 当日行存在、总数为空：ingest 的 999999 被忽略，只补认证数 46
        state.update(rows=[{'_record_id': 'rec1', '日期': '2026-10-27T00:00:00+08:00',
                            '账号': ['@YOUR_X_HANDLE'], '粉丝总数': None, '认证粉丝数': None}],
                     updates=[], created=[])
        capture.lines.clear()
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=999999, verified_followers=46,
            captured_at=NOW, source='ingest')
        assert result['ok'] and result['duplicate'] is True, result
        assert result['enriched'] == {'认证粉丝数': 46}, result
        assert result['ignored_followers'] == 999999, result
        assert state['updates'] == [('rec1', {'认证粉丝数': 46})], state['updates']
        row = state['rows'][0]
        assert row['粉丝总数'] is None and row['认证粉丝数'] == 46, row
        assert any('followers ignored' in line and '999999' in line
                   for line in capture.lines), capture.lines

        # 2. 当日行已有正确总数：ingest 不得覆盖，认证数已存在也不重复写
        state.update(rows=[{'_record_id': 'rec2', '日期': '2026-10-27T00:00:00+08:00',
                            '账号': ['@YOUR_X_HANDLE'], '粉丝总数': 66, '认证粉丝数': 46}],
                     updates=[], created=[])
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=999999, verified_followers=46,
            captured_at=NOW, source='ingest')
        assert result['duplicate'] is True and result['enriched'] == {}, result
        assert state['updates'] == [], state['updates']
        assert state['rows'][0]['粉丝总数'] == 66

        # 3. 当日无行：仍可建行，但粉丝总数留空，只写认证数
        state.update(rows=[], updates=[], created=[])
        capture.lines.clear()
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=999999, verified_followers=46,
            captured_at=NOW, source='ingest')
        assert result['created'] is True and result['followers'] is None, result
        assert result['ignored_followers'] == 999999, result
        assert len(state['created']) == 1, state['created']
        fields = state['created'][0]
        assert '粉丝总数' not in fields, fields
        assert fields['认证粉丝数'] == 46, fields
        assert fields['账号'] == ['@YOUR_X_HANDLE'], fields
        assert any('followers ignored' in line and '999999' in line
                   for line in capture.lines), capture.lines

        # 4. ingest 只报 followers：忽略后无有效指标，不建行
        state.update(rows=[], updates=[], created=[])
        capture.lines.clear()
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=999999, verified_followers=None,
            captured_at=NOW, source='ingest')
        assert result['ok'] is False and result['reason'] == 'no_metrics', result
        assert state['created'] == [] and state['updates'] == []
        assert any('followers ignored' in line and '999999' in line
                   for line in capture.lines), capture.lines

        # 5. 未显式传 source 时按 ingest 处理（fail-closed）
        state.update(rows=[], updates=[], created=[])
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=999999, verified_followers=46,
            captured_at=NOW)
        assert result['ignored_followers'] == 999999, result
        assert '粉丝总数' not in state['created'][0], state['created']

        # 6. fx_poll 路径：无行时 followers 照常写
        state.update(rows=[], updates=[], created=[])
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=66, verified_followers=None,
            captured_at=NOW, source='fx_poll')
        assert result['created'] is True and result['ignored_followers'] is None, result
        assert state['created'][0]['粉丝总数'] == 66, state['created']

        # 7. fx_poll 可补 ingest 留下的空总数，且不覆盖已有值
        state.update(rows=[{'_record_id': 'rec7', '日期': '2026-10-27T00:00:00+08:00',
                            '账号': ['@YOUR_X_HANDLE'], '粉丝总数': None, '认证粉丝数': 46}],
                     updates=[], created=[])
        result = core.handle_account_report(
            account='@YOUR_X_HANDLE', followers=66, verified_followers=None,
            captured_at=NOW, source='fx_poll')
        assert state['updates'] == [('rec7', {'粉丝总数': 66})], state['updates']
        assert result['ignored_followers'] is None, result

        # 8. 非目标账号仍拒绝
        result = core.handle_account_report(
            account='@other', followers=999999, verified_followers=46,
            captured_at=NOW, source='ingest')
        assert result['reason'] == 'not_target_account', result
    finally:
        core.list_records, core.update_record, core.create_records = real

    # 9/10. fx_poll 的「当天已有行」判断：总数已填才跳过；总数为空继续取数补写
    import fx_poll
    core.list_records = fake_list
    account_calls = []
    http_calls = []
    real_http_json = fx_poll.http_json
    real_handle = core.handle_account_report
    fx_poll.http_json = lambda url: (http_calls.append(url)
                                     or {'user': {'followers': 66}})
    core.handle_account_report = lambda **kw: (account_calls.append(kw)
                                               or {'ok': True, 'created': False})
    try:
        state['rows'] = [{'_record_id': 'rec9', '日期': '2026-10-27T00:00:00+08:00',
                          '账号': ['@YOUR_X_HANDLE'], '粉丝总数': 66, '认证粉丝数': 46}]
        assert fx_poll.maybe_account_snapshot(NOW, False) is None
        assert http_calls == [] and account_calls == [], (http_calls, account_calls)

        state['rows'] = [{'_record_id': 'rec10', '日期': '2026-10-27T00:00:00+08:00',
                          '账号': ['@YOUR_X_HANDLE'], '粉丝总数': None, '认证粉丝数': 46}]
        fx_poll.maybe_account_snapshot(NOW, False)
        assert len(http_calls) == 1, http_calls
        assert len(account_calls) == 1, account_calls
        assert account_calls[0]['followers'] == 66, account_calls
        assert account_calls[0]['source'] == 'fx_poll', account_calls
    finally:
        fx_poll.http_json = real_http_json
        core.handle_account_report = real_handle
        core.list_records = real[0]

    core.log.removeHandler(capture)

    print('core_account_guard_test: PASS ingest followers ignored (existing/empty/new/'
          'only-followers/default-source), verified enriched, fx_poll writes and fills, '
          'fx_poll skip only when total present, non-target rejected')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
