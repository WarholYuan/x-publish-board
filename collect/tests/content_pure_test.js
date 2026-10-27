'use strict';
const assert = require('assert');
const path = require('path');

const handlers = [];
global.chrome = { runtime: { onMessage: { addListener: (fn) => handlers.push(fn) } } };
global.window = { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) };
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  documentElement: { innerHTML: '' },
  body: { innerText: '' },
};
global.location = { pathname: '/', href: 'https://x.com/' };

require(path.join(__dirname, '..', '..', 'extension', 'content.js'));
const I = globalThis.__xcollectInternals;
assert(I, 'internals exported');
assert.strictEqual(handlers.length, 1, 'one message listener');

// parseCount
const cases = [
  ['1.2K', 1200], ['3,456', 3456], ['7万', 70000], ['1.5M', 1500000],
  ['2亿', 200000000], ['0', 0], ['123 views', 123], ['1,234 次观看', 1234],
  ['abc', null], ['', null], [null, null], [12, 12],
];
for (const [input, want] of cases) {
  assert.strictEqual(I.parseCount(input), want, `parseCount(${JSON.stringify(input)})`);
}

// pageType
const pages = [
  ['/YOUR_X_HANDLE', 'profile'],
  ['/YOUR_X_HANDLE/', 'profile'],
  ['/YOUR_X_HANDLE/status/1000000000000000001', 'post'],
  ['/i/status/1000000000000000001', 'post'],
  ['/i/account_analytics', 'account_analytics'],
  ['/i/account_analytics/', 'account_analytics'],
  ['/i/account_analytics/post/1000000000000000001', 'post_analytics'],
  ['/i/account_analytics/1000000000000000001', 'post_analytics'],
  ['/i/tweet_analytics/1000000000000000001', 'post_analytics'],
  ['/YOUR_X_HANDLE/status/1000000000000000001/analytics', 'post_analytics'],
  ['/home', 'other'],
  ['/OTHER_HANDLE', 'other'],
];
for (const [p, kind] of pages) {
  global.location.pathname = p;
  global.location.search = '';
  assert.strictEqual(I.pageType().kind, kind, `pageType(${p})`);
}
global.location.pathname = '/i/account_analytics';
global.location.search = '?post_id=1000000000000000001';
assert.strictEqual(I.pageType().kind, 'post_analytics', 'account analytics query id');

// detectLoggedInHandle: profile nav link
global.document.querySelector = (sel) => {
  if (sel.includes('AppTabBar_Profile_Link')) return { getAttribute: () => '/YOUR_X_HANDLE' };
  return null;
};
assert.strictEqual(I.detectLoggedInHandle(), 'YOUR_X_HANDLE');

// switch-account button text fallback
global.document.querySelector = (sel) => {
  if (sel.includes('SideNav_AccountSwitcher_Button')) {
    return { getAttribute: () => null, innerText: 'Your Name\n@YOUR_X_HANDLE' };
  }
  return null;
};
assert.strictEqual(I.detectLoggedInHandle(), 'YOUR_X_HANDLE');

// no evidence -> null (must skip, not guess)
global.document.querySelector = () => null;
assert.strictEqual(I.detectLoggedInHandle(), null);

console.log('content_pure_test: PASS 20 parseCount + 13 pageType + 3 handle cases');
