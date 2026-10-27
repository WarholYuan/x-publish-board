'use strict';
/* 用 jsdom 合成 X 页面结构，验证 content.js 的 DOM 提取逻辑。
 * 运行：NODE_PATH=/tmp/xctest/node_modules node content_dom_test.js
 * 真实登录态 DOM 仍需用户装载后实测（报告「待实测」）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'extension', 'content.js'), 'utf8');

function load(html, url) {
  const dom = new JSDOM(html, { url, runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.chrome = { runtime: { onMessage: { addListener: () => {} } } };
  dom.window.eval(SOURCE);
  return dom.window;
}

function profileHtml() {
  return `<!doctype html><html><body>
  <nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
  <main>
    <article>
      <a href="/YOUR_X_HANDLE/status/1000000000000000001"><time datetime="2026-10-11T13:27:03.000Z">Oct 11</time></a>
      <div dir="auto" class="max-w-full text-body whitespace-pre-wrap">给我的 Grok bot 换了个新头像</div>
      <div role="group" aria-label="0 replies, 0 reposts, 1 like, 0 bookmarks, 163 views"></div>
    </article>
    <article>
      <a href="/OTHER_HANDLE/status/1000000000000000003"><time datetime="2026-10-11T13:30:00.000Z">Oct 11</time></a>
      <div dir="auto" class="whitespace-pre-wrap">别人的帖子，不应采集</div>
    </article>
    <article>
      <a href="/YOUR_X_HANDLE/status/1000000000000000002"><time datetime="2026-10-15T05:32:48.000Z">Oct 15</time></a>
      <div dir="auto" class="whitespace-pre-wrap">250了🤡这个手气</div>
    </article>
  </main></body></html>`;
}

const pWin = load(profileHtml(), 'https://x.com/YOUR_X_HANDLE');
const p = pWin.__xcollectInternals;
assert.strictEqual(p.detectLoggedInHandle(), 'YOUR_X_HANDLE');
assert.strictEqual(p.pageType().kind, 'profile');
const posts = p.extractPosts();
assert.strictEqual(posts.length, 2, `posts=${JSON.stringify(posts)}`);
assert.strictEqual(posts[0].id, '1000000000000000001');
assert.strictEqual(posts[0].text, '给我的 Grok bot 换了个新头像');
assert.strictEqual(posts[0].created_at, '2026-10-11T13:27:03.000Z');
assert.strictEqual(posts[1].id, '1000000000000000002');

const postHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><article>
  <a href="/YOUR_X_HANDLE/status/1000000000000000001"><time datetime="2026-10-11T13:27:03.000Z"></time></a>
  <div dir="auto" class="whitespace-pre-wrap">给我的 Grok bot 换了个新头像</div>
  <div role="group" aria-label="10 replies, 2 reposts, 3 likes, 1 bookmark, 1,234 views"></div>
  <button data-testid="reply"><span>10</span></button>
  <button data-testid="retweet"><span>2</span></button>
  <button data-testid="like"><span>3</span></button>
  <button data-testid="bookmark"><span>1</span></button>
</article></main></body></html>`;
const qWin = load(postHtml, 'https://x.com/YOUR_X_HANDLE/status/1000000000000000001');
const q = qWin.__xcollectInternals;
assert.strictEqual(q.pageType().kind, 'post');
const m = q.extractAllMetrics(qWin.document.querySelector('article'));
assert.strictEqual(m.metrics.views, 1234);
assert.strictEqual(m.metrics.replies, 10);
assert.strictEqual(m.metrics.reposts, 2);
assert.strictEqual(m.metrics.likes, 3);
assert.strictEqual(m.metrics.bookmarks, 1);


const ariaOnlyHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><article>
  <a href="/YOUR_X_HANDLE/status/1000000000000000001"><time datetime="2026-10-11T13:27:03.000Z"></time></a>
  <div dir="auto" class="whitespace-pre-wrap">给我的 Grok bot 换了个新头像</div>
  <div role="group" aria-label="10 replies, 2 reposts, 3 likes, 1 bookmark, 1,234 views"></div>
</article></main></body></html>`;
const aoWin = load(ariaOnlyHtml, 'https://x.com/YOUR_X_HANDLE/status/1000000000000000001');
const ao = aoWin.__xcollectInternals.extractAllMetrics(aoWin.document.querySelector('article'));
assert.strictEqual(ao.metrics.views, 1234);
assert.strictEqual(ao.metrics.replies, 10);
assert.strictEqual(ao.metrics.likes, 3);
assert.strictEqual(ao.metrics.bookmarks, 1);

const analyticsHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main>
  <div><span>Impressions</span><span>12,345</span></div>
  <div><span>Likes</span><span>678</span></div>
  <div><span>Bookmarks</span><span>90</span></div>
  <div><span>Reposts</span><span>12</span></div>
  <div><span>Replies</span><span>34</span></div>
  <div><span>Profile visits</span><span>56</span></div>
  <div><span>Link clicks</span><span>7</span></div>
</main></body></html>`;
const aWin = load(analyticsHtml, 'https://x.com/i/account_analytics/post/1000000000000000001');
const a = aWin.__xcollectInternals;
assert.strictEqual(a.pageType().kind, 'post_analytics');
const am = a.extractAllMetrics(aWin.document.querySelector('main'));
assert.deepStrictEqual({ ...am.metrics }, {
  views: 12345, likes: 678, bookmarks: 90, reposts: 12, replies: 34,
  profile_visits: 56, link_clicks: 7,
});

const accountHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main>
  <div><span>Verified followers</span><span>12</span></div>
  <div><span>Followers</span><span>66</span></div>
</main></body></html>`;
const acWin = load(accountHtml, 'https://x.com/i/account_analytics');
const ac = acWin.__xcollectInternals;
assert.strictEqual(ac.pageType().kind, 'account_analytics');
const acm = ac.extractAllMetrics(acWin.document.querySelector('main'));
assert.strictEqual(acm.metrics.followers, 66);
assert.strictEqual(acm.metrics.verified_followers, 12);

// 复现 2026-10-27 实测：Analytics 账号概览里「粉丝总数 66」的标签后面紧邻
// 「认证粉丝 46」子卡片，旧解析把子卡片的 46 当成总数（followers=46）。
const overviewHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main>
  <section class="followers-card">
    <div class="total-number"><span>66</span></div>
    <h2><span>粉丝总数</span></h2>
    <div class="breakdown">
      <div class="metric"><span>认证粉丝</span><span>46</span></div>
    </div>
  </section>
</main></body></html>`;
const ovWin = load(overviewHtml, 'https://x.com/i/account_analytics');
const ov = ovWin.__xcollectInternals;
assert.strictEqual(ov.pageType().kind, 'account_analytics');
const ovm = ov.extractAllMetrics(ovWin.document.querySelector('main'));
assert.strictEqual(ovm.metrics.followers, 66, `overview followers=${JSON.stringify(ovm.metrics)}`);
assert.strictEqual(ovm.metrics.verified_followers, 46, `overview verified=${JSON.stringify(ovm.metrics)}`);

const overviewEnHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main>
  <section class="followers-card">
    <div class="total-number"><span>66</span></div>
    <h2><span>Followers</span></h2>
    <div class="breakdown">
      <div class="metric"><span>Verified followers</span><span>46</span></div>
    </div>
  </section>
</main></body></html>`;
const ovEnWin = load(overviewEnHtml, 'https://x.com/i/account_analytics');
const ovEn = ovEnWin.__xcollectInternals.extractAllMetrics(ovEnWin.document.querySelector('main'));
assert.strictEqual(ovEn.metrics.followers, 66, `overview-en followers=${JSON.stringify(ovEn.metrics)}`);
assert.strictEqual(ovEn.metrics.verified_followers, 46, `overview-en verified=${JSON.stringify(ovEn.metrics)}`);

// 并列标签/数值两列：按标签下标取对应数值，不能把认证数当总数。
const parallelHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><div class="stats">
  <div class="labels"><span>粉丝总数</span><span>认证粉丝</span></div>
  <div class="values"><span>66</span><span>46</span></div>
</div></main></body></html>`;
const parWin = load(parallelHtml, 'https://x.com/i/account_analytics');
const parm = parWin.__xcollectInternals.extractAllMetrics(parWin.document.querySelector('main'));
assert.strictEqual(parm.metrics.followers, 66, `parallel followers=${JSON.stringify(parm.metrics)}`);
assert.strictEqual(parm.metrics.verified_followers, 46, `parallel verified=${JSON.stringify(parm.metrics)}`);

// 页面只有认证粉丝块时，总数必须留空，不能把 46 当总数。
const verifiedOnlyHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><section>
  <div class="total-number"><span>46</span></div>
  <h2><span>认证粉丝</span></h2>
</section></main></body></html>`;
const voWin = load(verifiedOnlyHtml, 'https://x.com/i/account_analytics');
const vom = voWin.__xcollectInternals.extractAllMetrics(voWin.document.querySelector('main'));
assert.strictEqual(vom.metrics.followers, undefined, `verified-only followers=${JSON.stringify(vom.metrics)}`);
assert.strictEqual(vom.metrics.verified_followers, 46, `verified-only verified=${JSON.stringify(vom.metrics)}`);

// aria-label 路径：认证块的「46 粉丝（认证）」不能被裸 粉丝 别名命中。
const ariaFollowerHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main>
  <div class="metric"><span>粉丝总数</span><span>66</span></div>
  <div class="metric" aria-label="46 粉丝（认证）"><span>认证粉丝</span><span>46</span></div>
</main></body></html>`;
const afWin = load(ariaFollowerHtml, 'https://x.com/i/account_analytics');
const afm = afWin.__xcollectInternals.extractAllMetrics(afWin.document.querySelector('main'));
assert.strictEqual(afm.metrics.followers, 66, `aria followers=${JSON.stringify(afm.metrics)}`);
assert.strictEqual(afm.metrics.verified_followers, 46, `aria verified=${JSON.stringify(afm.metrics)}`);

const mixedAriaHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><div aria-label="66 粉丝，认证粉丝 46"></div></main></body></html>`;
const maWin = load(mixedAriaHtml, 'https://x.com/i/account_analytics');
const mam = maWin.__xcollectInternals.extractAllMetrics(maWin.document.querySelector('main'));
assert.strictEqual(mam.metrics.followers, 66, `mixed-aria followers=${JSON.stringify(mam.metrics)}`);
assert.strictEqual(mam.metrics.verified_followers, 46, `mixed-aria verified=${JSON.stringify(mam.metrics)}`);

// 内嵌 JSON：verified 对象里的 followers_count 不能被总数路径读到。
const embedNestedHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><div>概览</div><script type="application/json">{"verified":{"followers_count":46},"followers_count":66}</script></main>
</body></html>`;
const enWin = load(embedNestedHtml, 'https://x.com/i/account_analytics');
const enm = enWin.__xcollectInternals.extractAllMetrics(enWin.document.querySelector('main'));
assert.strictEqual(enm.metrics.followers, 66, `embedded-nested followers=${JSON.stringify(enm.metrics)}`);

const embedBothHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><div>概览</div><script type="application/json">{"verified_followers_count":46,"followers_count":66}</script></main>
</body></html>`;
const ebWin = load(embedBothHtml, 'https://x.com/i/account_analytics');
const ebm = ebWin.__xcollectInternals.extractAllMetrics(ebWin.document.querySelector('main'));
assert.strictEqual(ebm.metrics.followers, 66, `embedded-both followers=${JSON.stringify(ebm.metrics)}`);
assert.strictEqual(ebm.metrics.verified_followers, 46, `embedded-both verified=${JSON.stringify(ebm.metrics)}`);

const embedHtml = `<!doctype html><html><body>
<nav aria-label="Primary"><a href="/YOUR_X_HANDLE" aria-label="Profile">Profile</a></nav>
<main><div>概览</div><script type="application/json">{"impression_count":987,"like_count":6}</script></main>
</body></html>`;
const eWin = load(embedHtml, 'https://x.com/i/account_analytics');
const e = eWin.__xcollectInternals;
const em = e.extractAllMetrics(eWin.document.querySelector('main'));
assert.strictEqual(em.metrics.views, 987);
assert.strictEqual(em.metrics.likes, 6);

console.log('content_dom_test: PASS profile posts, post buttons, aria-only metrics, analytics labels, account followers, follower-total regression (verified block / aria / embedded JSON), embedded fallback');
