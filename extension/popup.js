/* X 数据回采 popup */
'use strict';

const TOKEN_KEY = 'collectToken';
const LAST_KEY = 'lastCollect';

const $ = (id) => document.getElementById(id);

function fmt(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function setTokenState(configured) {
  $('tokenDot').className = `dot ${configured ? 'ok' : ''}`;
  $('tokenStatus').textContent = configured ? '已配置（留空保存不会覆盖）' : '未配置：请填入部署端提供的令牌';
}

function setBusy(busy) {
  $('collect').disabled = busy;
  $('save').disabled = busy;
  $('clear').disabled = busy;
  $('collect').textContent = busy ? '采集中…' : '立即采集';
}

function renderLast(last) {
  if (!last) {
    $('lastAt').textContent = '—';
    $('lastTrigger').textContent = '—';
    $('lastDetail').textContent = '—';
    $('lastTabs').textContent = '—';
    return;
  }
  $('lastAt').textContent = fmt(last.at);
  $('lastTrigger').textContent = last.trigger === 'alarm' ? '每小时自动' : '手动';
  const detail = last.detail || (last.ok ? '完成' : (last.error || '失败'));
  $('lastDetail').textContent = detail;
  $('lastDetail').className = last.ok ? 'ok' : 'err';
  $('lastTabs').textContent = `${last.tabs || 0} 个 / 上报 ${last.uploaded || 0} 条`;
  const notes = Array.isArray(last.notes) ? last.notes : [];
  const skipped = notes.filter((n) => n.note === 'not_target_account' || n.note === 'logged_in_handle_not_found');
  if (skipped.length) {
    const reasons = skipped.map((n) => (n.note === 'not_target_account'
      ? `当前登录 @${n.handle || '?'} 不是 @YOUR_X_HANDLE，已跳过`
      : '未识别到已登录账号，已跳过')).join('；');
    $('skip').textContent = reasons;
  } else {
    $('skip').textContent = '';
  }
}

async function refresh() {
  const obj = await chrome.storage.local.get([TOKEN_KEY, LAST_KEY]);
  const token = String(obj[TOKEN_KEY] || '').trim();
  setTokenState(Boolean(token));
  renderLast(obj[LAST_KEY] || null);
}

async function saveToken() {
  const value = $('token').value.trim();
  if (!value) {
    const obj = await chrome.storage.local.get(TOKEN_KEY);
    if (obj[TOKEN_KEY]) {
      $('result').textContent = '令牌未改动。';
      $('result').className = 'result muted';
    } else {
      $('result').textContent = '请先填入令牌。';
      $('result').className = 'result err';
    }
    return;
  }
  await chrome.storage.local.set({ [TOKEN_KEY]: value });
  $('token').value = '';
  setTokenState(true);
  $('result').textContent = '令牌已保存到本机。';
  $('result').className = 'result ok';
}

async function clearToken() {
  await chrome.storage.local.remove(TOKEN_KEY);
  $('token').value = '';
  setTokenState(false);
  $('result').textContent = '令牌已清除。';
  $('result').className = 'result muted';
}

async function collectNow() {
  setBusy(true);
  $('result').textContent = '正在触发已打开的 X 页面采集…';
  $('result').className = 'result muted';
  try {
    const res = await chrome.runtime.sendMessage({ type: 'COLLECT_NOW' });
    if (!res) {
      $('result').textContent = '后台没有响应，请重试。';
      $('result').className = 'result err';
      return;
    }
    if (res.ok) {
      $('result').textContent = res.uploaded
        ? `完成：上报 ${res.uploaded} 条。`
        : (res.detail || '完成：暂无可采数据。');
      $('result').className = 'result ok';
    } else {
      $('result').textContent = `失败：${res.detail || res.error || '未知错误'}`;
      $('result').className = 'result err';
    }
    await refresh();
  } catch (err) {
    $('result').textContent = `失败：${String(err && err.message ? err.message : err)}`;
    $('result').className = 'result err';
  } finally {
    setBusy(false);
  }
}

$('save').addEventListener('click', () => { saveToken().catch((e) => { $('result').textContent = String(e); }); });
$('clear').addEventListener('click', () => { clearToken().catch((e) => { $('result').textContent = String(e); }); });
$('collect').addEventListener('click', () => { collectNow(); });
$('token').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); saveToken(); }
});

refresh().catch((err) => {
  $('result').textContent = `读取本地配置失败：${String(err && err.message ? err.message : err)}`;
  $('result').className = 'result err';
});
