/* X 数据回采 background service worker
 * - alarms 每小时唤醒，在已打开的 X 标签页上触发 content script 采集
 * - 统一在这里读取本地令牌并 POST 到服务端（令牌不会传进页面）
 * - 只读采集：不调用 X 的任何写接口
 */
'use strict';

const ENDPOINT = 'https://YOUR_DOMAIN/collect-api/ingest';
const ALARM_NAME = 'xcollect-hourly';
const TOKEN_KEY = 'collectToken';
const LAST_KEY = 'lastCollect';
const MAX_PAYLOAD_BYTES = 480 * 1024;

let running = null;

function nowIso() {
  return new Date().toISOString();
}

async function getToken() {
  const obj = await chrome.storage.local.get(TOKEN_KEY);
  return String(obj[TOKEN_KEY] || '').trim();
}

async function saveLast(last) {
  await chrome.storage.local.set({ [LAST_KEY]: last });
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: color || '#1d9bf0' });
    chrome.action.setBadgeText({ text: text || '' });
  } catch (_) { /* ignore */ }
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing) {
    chrome.alarms.create(ALARM_NAME, { delayInMinutes: 1, periodInMinutes: 60 });
  }
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label || 'timeout'} after ${ms}ms`)), ms)),
  ]);
}

async function upload(payload, token) {
  const body = JSON.stringify(payload);
  if (body.length > MAX_PAYLOAD_BYTES) throw new Error('payload_too_large');
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 25000);
      let resp;
      try {
        resp = await fetch(ENDPOINT, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Collect-Token': token,
          },
          body,
          credentials: 'omit',
          cache: 'no-store',
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timer);
      }
      const text = await resp.text();
      let data = null;
      try { data = JSON.parse(text); } catch (_) { data = null; }
      if (!resp.ok) {
        const err = (data && data.error) || `http_${resp.status}`;
        // 4xx 不重试（令牌/参数问题），5xx 重试一次
        if (resp.status < 500) throw Object.assign(new Error(err), { noRetry: true });
        lastErr = new Error(err);
        continue;
      }
      return { ok: true, data };
    } catch (err) {
      lastErr = err;
      if (err && err.noRetry) break;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 900));
    }
  }
  throw lastErr || new Error('upload_failed');
}

async function collectTab(tab) {
  const res = await withTimeout(
    chrome.tabs.sendMessage(tab.id, { type: 'XCOLLECT_COLLECT' }),
    30000,
    'tab_message',
  );
  return res || { ok: false, error: 'empty_tab_response' };
}

async function runCollection(trigger) {
  if (running) return { ok: false, error: 'busy', detail: '上一次采集尚未结束' };
  running = (async () => {
    const started = Date.now();
    const at = nowIso();
    setBadge('', '#1d9bf0');
    const token = await getToken();
    if (!token) {
      const last = {
        at, trigger, ok: false, error: 'token_missing',
        detail: '未配置令牌：请在扩展 popup 里填入 collect token',
        tabs: 0, payloads: 0, uploaded: 0, errors: [],
      };
      await saveLast(last);
      setBadge('!', '#f4212e');
      return last;
    }

    let tabs = [];
    try {
      tabs = await chrome.tabs.query({ url: ['https://x.com/*', 'https://twitter.com/*'] });
    } catch (err) {
      const last = {
        at, trigger, ok: false, error: 'tab_query_failed',
        detail: String(err && err.message ? err.message : err),
        tabs: 0, payloads: 0, uploaded: 0, errors: [],
      };
      await saveLast(last);
      setBadge('!', '#f4212e');
      return last;
    }

    const notes = [];
    const errors = [];
    let payloads = 0;
    let uploaded = 0;
    const details = [];

    for (const tab of tabs) {
      let res;
      try {
        res = await collectTab(tab);
      } catch (err) {
        notes.push({ tabId: tab.id, note: 'content_unreachable', detail: String(err && err.message ? err.message : err) });
        continue;
      }
      const page = res.page && res.page.kind ? res.page.kind : 'unknown';
      if (res.skipped) {
        notes.push({ tabId: tab.id, note: res.reason || 'skipped', handle: res.handle || null, page });
        continue;
      }
      if (res.ok === false) {
        errors.push({ tabId: tab.id, error: res.error || res.reason || 'collect_failed', page });
        continue;
      }
      const list = Array.isArray(res.payloads) ? res.payloads : [];
      if (!list.length) {
        notes.push({ tabId: tab.id, note: res.status || 'no_data', page, handle: res.handle || null });
        continue;
      }
      for (const payload of list) {
        payloads += 1;
        try {
          const out = await upload(payload, token);
          uploaded += 1;
          details.push({
            tabId: tab.id,
            type: payload.type,
            post_id: payload.post_id || null,
            items: payload.items ? payload.items.length : undefined,
            result: out.data && out.data.type ? out.data.type : 'ok',
            matched: out.data && out.data.matched,
            fill: out.data && out.data.fill ? out.data.fill.reason : undefined,
          });
        } catch (err) {
          errors.push({
            tabId: tab.id,
            type: payload.type,
            error: String(err && err.message ? err.message : err).slice(0, 300),
          });
        }
      }
    }

    const ok = errors.length === 0;
    const last = {
      at,
      trigger: trigger || 'manual',
      ok,
      duration_ms: Date.now() - started,
      tabs: tabs.length,
      payloads,
      uploaded,
      notes,
      errors,
      details,
      detail: ok
        ? (uploaded ? `上报 ${uploaded} 条` : (tabs.length ? '已检查，暂无可采数据' : '没有打开的 X 标签页'))
        : `有 ${errors.length} 条上报失败`,
    };
    await saveLast(last);
    setBadge(ok ? '✓' : '!', ok ? '#00ba7c' : '#f4212e');
    return last;
  })();

  try {
    return await running;
  } finally {
    running = null;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  setBadge('', '#1d9bf0');
});

chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    runCollection('alarm').catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'COLLECT_NOW') return undefined;
  runCollection('manual')
    .then((res) => sendResponse(res))
    .catch((err) => sendResponse({
      ok: false,
      error: 'collect_failed',
      detail: String(err && err.message ? err.message : err),
    }));
  return true;
});

// service worker 冷启动时兜底补 alarm
ensureAlarm().catch(() => {});
