/* X 数据回采 content script
 * 只读：仅读取当前页面已经渲染出来的 DOM / 内嵌文本，不调用任何写接口，
 * 不读取 cookies、不注入页面脚本、不拦截网络请求。
 *
 * 采集范围（只认 @YOUR_X_HANDLE 本人）：
 *  - /YOUR_X_HANDLE          主页帖子列表（id/链接/发布时间/正文）
 *  - /<...>/status/<id>  单帖详情页可见指标
 *  - x.com/i/account_analytics...  Analytics 账号页 / 单帖活动页指标
 *
 * 注意：X 前端 class 名经常变，这里以 data-testid / aria-label / 可见文本
 * 多路兜底；实际登录态 DOM 需用户装载后按报告里的「待实测」项确认。
 */
(() => {
  'use strict';

  const TARGET = 'YOUR_X_HANDLE';
  const TARGET_AT = '@YOUR_X_HANDLE';
  const MAX_POSTS = 30;
  const MAX_TEXT = 8000;

  const LABELS = {
    views: ['impressions', 'post impressions', 'views', 'view count', '曝光', '展示', '帖子曝光', '浏览量', '观看次数', '展现'],
    likes: ['likes', 'like', '点赞', '喜欢', '赞'],
    bookmarks: ['bookmarks', 'bookmark', '收藏', '书签'],
    reposts: ['reposts', 'repost', 'retweets', 'retweet', '转发', '转帖', '转推'],
    replies: ['replies', 'reply', '回复', '评论'],
    quotes: ['quotes', 'quote', '引用'],
    profile_visits: ['profile visits', 'profile clicks', 'profile visit', '主页访问', '个人资料访问', '资料页访问', '主页点击', '个人主页访问'],
    link_clicks: ['link clicks', 'link click', '链接点击', '网址链接点击', '链接点击数'],
    followers: ['followers', 'follower', '粉丝', '粉丝总数', '关注者', '关注人数'],
    verified_followers: ['verified followers', 'verified follower', '认证粉丝', '认证粉丝数', '已认证粉丝', '蓝v粉丝', '蓝v认证粉丝'],
  };

  const EMBED_KEYS = {
    views: ['impression_count', 'impressions', 'view_count', 'views_count', 'viewCount'],
    likes: ['favorite_count', 'like_count', 'likes'],
    bookmarks: ['bookmark_count', 'bookmarks'],
    reposts: ['retweet_count', 'repost_count', 'retweets'],
    replies: ['reply_count', 'replies'],
    quotes: ['quote_count', 'quotes'],
    profile_visits: ['profile_visits', 'profile_visit_count', 'profile_clicks'],
    link_clicks: ['url_link_clicks', 'link_clicks', 'link_click_count'],
    followers: ['followers_count', 'followers'],
    verified_followers: ['verified_followers_count', 'verified_followers', 'blue_verified_followers_count'],
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 「认证/verified」是粉丝总数解析的硬排除标记：任何带此标记的标签或数值块
  // 都不能作为 total followers 的来源（X Analytics 会把两者渲染成相邻卡片）。
  const VERIFIED_MARKER_RE = /verified|认证|蓝\s*[vV]/i;
  const VERIFIED_LABEL_RE = /(?:verified\s+followers?|blue[_\s]*verified\s+followers?|认证粉丝|已认证粉丝|蓝\s*[vV]\s*粉丝)/i;

  function hasVerifiedMarker(text) {
    return VERIFIED_MARKER_RE.test(String(text || ''));
  }

  // 去掉「认证粉丝」字样后是否还剩下真正的粉丝总数标签。
  // 用来识别「粉丝总数 + 认证粉丝」这种把两个标签放在同一容器里的包装节点。
  function hasNonVerifiedFollowerLabel(text) {
    const without = String(text || '').replace(new RegExp(VERIFIED_LABEL_RE.source, 'ig'), ' ');
    const norm = normLabel(without);
    return LABELS.followers.some((a) => matchLabelAlias(norm, a));
  }

  function normLabel(text) {
    return String(text || '')
      .toLowerCase()
      .replace(/[\s:：·•|/\\]+/g, '')
      .replace(/[（(].*?[)）]/g, '')
      .trim();
  }

  function parseCount(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number') return Number.isFinite(value) ? Math.round(value) : null;
    let s = String(value).trim().replace(/\u00a0/g, ' ');
    if (!s) return null;
    s = s.replace(/,/g, '').replace(/，/g, '').replace(/\s+/g, '');
    const m = s.match(/(-?\d+(?:\.\d+)?)([KkMmBb万亿]?)/);
    if (!m) return null;
    let n = parseFloat(m[1]);
    if (!Number.isFinite(n)) return null;
    const unit = m[2];
    if (unit === 'K' || unit === 'k') n *= 1e3;
    else if (unit === 'M' || unit === 'm') n *= 1e6;
    else if (unit === 'B' || unit === 'b') n *= 1e9;
    else if (unit === '万') n *= 1e4;
    else if (unit === '亿') n *= 1e8;
    return Math.round(n);
  }

  function firstNumberIn(text, label) {
    let s = String(text || '');
    if (label) {
      const clean = String(label).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      s = s.replace(new RegExp(clean, 'ig'), ' ');
    }
    // 只取第一段数字，避免把 "1,234 / 5,678" 或 "66 46" 之类拼在一起
    const m = s.match(/(\d[\d,，]*(?:\.\d+)?(?:\s*[KkMmBb万亿])?)/);
    return m ? parseCount(m[1]) : null;
  }

  function visible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function textOf(el) {
    if (!el) return '';
    return String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  // 读数值容器时在相邻元素之间补空格，避免 <span>66</span><span>46</span>
  // 被 textContent 拼成 "6646"。同一元素内部的文本节点保持原样。
  function spacedTextOf(el) {
    if (!el) return '';
    const parts = [];
    for (const node of (el.childNodes || [])) {
      if (node.nodeType === 3) {
        parts.push(node.nodeValue || '');
      } else if (node.nodeType === 1) {
        parts.push(' ', spacedTextOf(node), ' ');
      }
    }
    const joined = parts.length ? parts.join('') : String(el.textContent || '');
    return joined.replace(/\s+/g, ' ').trim();
  }

  // 从容器文本里剔除「认证粉丝」子树，只留下真正的总数部分。
  // 例：「66 粉丝总数 认证粉丝 46」→「66 粉丝总数」。
  // 如果整个容器就是认证块，会只剩数字（调用方必须再校验是否还带粉丝标签）。
  function textWithoutVerified(el) {
    if (!el) return '';
    const full = textOf(el);
    if (!hasVerifiedMarker(full)) return full;
    const parts = [];
    const kids = el.childNodes || [];
    for (const node of kids) {
      if (node.nodeType === 3) {
        parts.push(node.nodeValue || '');
        continue;
      }
      if (node.nodeType !== 1) continue;
      const tag = String(node.tagName || '').toUpperCase();
      if (tag === 'SCRIPT' || tag === 'STYLE') continue;
      const nodeText = textOf(node);
      const aria = node.getAttribute ? (node.getAttribute('aria-label') || '') : '';
      if (hasVerifiedMarker(nodeText) || hasVerifiedMarker(aria)) continue;
      const sub = textWithoutVerified(node);
      if (sub) parts.push(sub);
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }

  // 只取「认证粉丝」标签旁边的数字，避免同容器里先出现总数时取错。
  function numberNearVerifiedLabel(text) {
    const s = String(text || '');
    let m = s.match(new RegExp(
      `(?:${VERIFIED_LABEL_RE.source})\\s*[:：]?\\s*(\\d[\\d,，]*(?:\\.\\d+)?\\s*[KkMmBb万亿]?)`, 'i'));
    if (m) return parseCount(m[1]);
    m = s.match(new RegExp(
      `(\\d[\\d,，]*(?:\\.\\d+)?\\s*[KkMmBb万亿]?)\\s*(?:条|次|个)?\\s*(?:${VERIFIED_LABEL_RE.source})`, 'i'));
    if (m) return parseCount(m[1]);
    // 「46 粉丝（认证）」「46 followers (verified)」这类把认证写成括注的 aria
    m = s.match(/(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)\s*(?:条|次|个)?\s*(?:followers?|粉丝)\s*[（(][^）)]*(?:认证|verified)[^）)]*[)）]/i);
    if (m) return parseCount(m[1]);
    return null;
  }

  // aria-label 版总数解析：先按分隔符切段，整段带认证标记的直接丢弃；
  // 没有分隔符的混合串再剥掉「<数字> 认证粉丝」片段后取总数。
  function ariaFollowerValue(aria) {
    const text = String(aria || '');
    for (const seg of text.split(/[,，;；、|]/)) {
      if (hasVerifiedMarker(seg)) continue;
      let m = seg.match(/(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)\s*(?:条|次|个)?\s*(?:followers?|粉丝)/i);
      let value = m ? parseCount(m[1]) : null;
      if (value === null) {
        m = seg.match(/(?:粉丝总数|粉丝|followers?)\s*[:：]?\s*(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)/i);
        value = m ? parseCount(m[1]) : null;
      }
      if (value === null) continue;
      if (m && /^\s*[（(][^）)]*(?:认证|verified)[^）)]*[)）]/i.test(seg.slice(m.index + m[0].length))) continue;
      return value;
    }
    const stripped = text.replace(
      /(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)\s*(?:条|次|个)?\s*(?:verified\s+followers?|blue[_\s]*verified\s+followers?|认证粉丝|已认证粉丝|蓝\s*[vV]\s*粉丝)\s*/ig,
      ' ',
    );
    if (!hasVerifiedMarker(stripped)) {
      const m = stripped.match(/(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)\s*(?:条|次|个)?\s*(?:followers?|粉丝)/i)
        || stripped.match(/(?:粉丝总数|粉丝|followers?)\s*[:：]?\s*(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)/i);
      if (m) return parseCount(m[1]);
    }
    return null;
  }

  function ariaVerifiedValue(aria) {
    return numberNearVerifiedLabel(aria);
  }

  function collectCandidateNodes(scope) {
    const out = [];
    const nodes = scope.querySelectorAll('div,span,p,dt,dd,li,td,th,h1,h2,h3,h4,strong,b');
    for (const el of nodes) {
      if (el.children.length > 3) continue;
      const text = textOf(el);
      if (!text || text.length > 48) continue;
      // 标签节点通常不含数字；含数字的往往是「标签+数值」的父容器，跳过避免错位
      if (/\d/.test(text)) continue;
      out.push({ el, text, norm: normLabel(text) });
      if (out.length > 4000) break;
    }
    return out;
  }

  function matchLabelAlias(norm, alias) {
    const a = normLabel(alias);
    if (!a || !norm) return false;
    if (norm === a) return true;
    // 只对较长的英文/中文标签做前缀匹配，避免「赞」误命中一大段文本
    if (a.length >= 3 && (norm.startsWith(a) || norm.endsWith(a))) return true;
    return false;
  }

  function extractByLabels(scope) {
    const found = {};
    const raw = {};
    const sources = {};
    const nodes = collectCandidateNodes(scope);
    for (const [key, aliases] of Object.entries(LABELS)) {
      for (const node of nodes) {
        if (key === 'followers' && hasVerifiedMarker(node.norm)) continue;
        if (key === 'verified_followers' && hasVerifiedMarker(node.norm)
            && hasNonVerifiedFollowerLabel(node.text)) continue;
        if (!aliases.some((a) => matchLabelAlias(node.norm, a))) continue;
        const labelText = node.text;
        const candidates = [];
        const next = node.el.nextElementSibling;
        if (next) candidates.push({ el: next, text: spacedTextOf(next), weight: 3 });
        const parent = node.el.parentElement;
        if (parent) candidates.push({ el: parent, text: spacedTextOf(parent), weight: 2 });
        const prev = node.el.previousElementSibling;
        if (prev) candidates.push({ el: prev, text: spacedTextOf(prev), weight: 1 });
        // 并列标签/数值两列（如 <div class=labels>粉丝总数 认证粉丝</div>
        // <div class=values>66 46</div>）按标签下标取对应数值。
        const labelParent = node.el.parentElement;
        if (labelParent && labelParent.children.length > 1 && !/\d/.test(textOf(labelParent))) {
          const values = labelParent.nextElementSibling;
          const idx = Array.prototype.indexOf.call(labelParent.children, node.el);
          if (values && idx >= 0 && values.children.length > idx) {
            const valEl = values.children[idx];
            if (valEl && /\d/.test(textOf(valEl))) {
              candidates.push({ el: valEl, text: spacedTextOf(valEl), weight: 2 });
            }
          }
        }
        // label 和数字在同一个叶子节点里
        candidates.push({ el: node.el, text: labelText, weight: 1 });
        let best = null;
        for (const cand of candidates) {
          if (!cand.text) continue;
          let text = cand.text;
          let value = null;
          let verifiedSpecific = false;
          if (key === 'followers') {
            // 数值块里带「认证/verified」时，先剔除认证子树；剔除后必须
            // 仍保留一个非认证的粉丝标签，否则只剩 46 这种数字，不能当总数。
            if (hasVerifiedMarker(text)) {
              text = textWithoutVerified(cand.el);
              if (!text) continue;
              const cleanNorm = normLabel(text);
              if (!LABELS.followers.some((a) => matchLabelAlias(cleanNorm, a))) continue;
            }
            value = firstNumberIn(text, labelText);
          } else if (key === 'verified_followers') {
            if (hasVerifiedMarker(text)) {
              value = numberNearVerifiedLabel(text);
              verifiedSpecific = value !== null;
            }
            if (value === null) value = firstNumberIn(text, labelText);
          } else {
            value = firstNumberIn(text, labelText);
          }
          if (value === null) continue;
          const score = cand.weight + (text.length <= 24 ? 1 : 0);
          if (!best || score > best.score) {
            best = { value, score, from: text, el: cand.el, verifiedSpecific };
          }
        }
        if (best) {
          // 总数与认证数不能来自同一个纯数字兜底节点；若同一容器同时带
          // 明确的「认证粉丝」标签，则允许（那仍是认证块自己的数字）。
          if (key === 'verified_followers' && !best.verifiedSpecific
              && sources.followers === best.el && best.value === found.followers) {
            continue;
          }
          found[key] = best.value;
          sources[key] = best.el;
          raw[labelText] = best.from;
          break;
        }
      }
    }
    return { metrics: found, labels: raw };
  }

  function extractAriaMetrics(scope) {
    const found = {};
    const raw = [];
    const els = scope.querySelectorAll('[aria-label]');
    const words = [
      ['views', 'views?|impressions?|view count|曝光|观看|浏览量'],
      ['likes', 'likes?|点赞|喜欢'],
      ['bookmarks', 'bookmarks?|收藏'],
      ['reposts', 'reposts?|retweets?|转发|转帖'],
      ['replies', 'replies?|reply|回复|评论'],
      ['quotes', 'quotes?|引用'],
      ['profile_visits', 'profile visits?|profile clicks?|主页访问|个人资料访问'],
      ['link_clicks', 'link clicks?|链接点击'],
      ['followers', 'followers?|粉丝'],
      ['verified_followers', 'verified followers?|认证粉丝'],
    ];
    for (const el of els) {
      const aria = String(el.getAttribute('aria-label') || '').trim();
      if (!aria || aria.length > 300) continue;
      for (const [key, word] of words) {
        if (found[key] !== undefined) continue;
        if (key === 'followers') {
          const n = ariaFollowerValue(aria);
          if (n !== null) {
            found[key] = n;
            raw.push(aria);
          }
          continue;
        }
        if (key === 'verified_followers') {
          const n = ariaVerifiedValue(aria);
          if (n !== null) {
            found[key] = n;
            raw.push(aria);
          }
          continue;
        }
        const re = new RegExp(`(\\d[\\d,，]*(?:\\.\\d+)?\\s*[KkMmBb万亿]?)\\s*(?:条|次|个)?\\s*(?:${word})`, 'i');
        const m = aria.match(re);
        if (m) {
          const n = parseCount(m[1]);
          if (n !== null) {
            found[key] = n;
            raw.push(aria);
          }
        }
      }
    }
    return { metrics: found, aria: raw.slice(0, 12) };
  }

  function extractButtons(scope) {
    const found = {};
    const map = {
      replies: ['reply'],
      reposts: ['retweet', 'unretweet'],
      likes: ['like', 'unlike'],
      bookmarks: ['bookmark', 'removeBookmark'],
    };
    for (const [key, testids] of Object.entries(map)) {
      for (const id of testids) {
        const el = scope.querySelector(`[data-testid="${id}"]`);
        if (!el) continue;
        const n = firstNumberIn(textOf(el) || el.getAttribute('aria-label') || '');
        if (n !== null) {
          found[key] = n;
          break;
        }
      }
    }
    // 新版 UI 可能没有 data-testid：用 aria-label 找到按钮，再从父节点读数字
    if (Object.keys(found).length < 4) {
      const ariaEls = Array.from(scope.querySelectorAll('[aria-label]'));
      const aliasMap = {
        replies: ['reply', '回复', '评论'],
        reposts: ['repost', 'retweet', '转发', '转帖'],
        likes: ['like', '点赞', '喜欢'],
        bookmarks: ['bookmark', '收藏'],
      };
      for (const [key, aliases] of Object.entries(aliasMap)) {
        if (found[key] !== undefined) continue;
        const el = ariaEls.find((e) => {
          const norm = normLabel(e.getAttribute('aria-label') || '');
          return aliases.some((a) => matchLabelAlias(norm, a));
        });
        if (!el) continue;
        const candidates = [el.parentElement, el.nextElementSibling,
          el.parentElement ? el.parentElement.parentElement : null];
        for (const cand of candidates) {
          if (!cand) continue;
          const n = firstNumberIn(textOf(cand));
          if (n !== null) { found[key] = n; break; }
        }
      }
    }
    // 浏览量入口
    for (const sel of ['[data-testid="analyticsButton"]', 'a[href*="/analytics"]', 'a[href*="/view"]']) {
      const el = scope.querySelector(sel);
      if (!el) continue;
      const n = firstNumberIn(textOf(el) || el.getAttribute('aria-label') || '');
      if (n !== null) {
        found.views = n;
        break;
      }
    }
    return found;
  }

  function extractVisibleViews(scope) {
    const body = String(scope.innerText || '').slice(0, 20000);
    const patterns = [
      /(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)\s*(?:次)?\s*(?:views?|impressions?|浏览量|观看次数|曝光)/i,
      /(?:views?|impressions?|浏览量|观看次数|曝光)\s*[:：]?\s*(\d[\d,，]*(?:\.\d+)?\s*[KkMmBb万亿]?)/i,
    ];
    for (const re of patterns) {
      const m = body.match(re);
      if (m) {
        const n = parseCount(m[1]);
        if (n !== null) return n;
      }
    }
    return null;
  }

  let htmlCache = null;

  // 从匹配位置向前回溯所在 JSON 对象的键路径（粗略配对花括号，忽略字符串里的括号）。
  // 只用来判断 followers 键是否位于 verified/blue_verified 对象内部。
  function enclosingJsonKeys(html, index) {
    const keys = [];
    let depth = 0;
    const start = Math.max(0, index - 20000);
    for (let i = index - 1; i >= start; i--) {
      const ch = html[i];
      if (ch === '}') {
        depth += 1;
      } else if (ch === '{') {
        if (depth > 0) {
          depth -= 1;
          continue;
        }
        const pre = html.slice(Math.max(start, i - 160), i);
        const km = pre.match(/["']([A-Za-z0-9_]+)["']\s*:\s*$/);
        if (km) keys.push(km[1]);
      }
    }
    return keys;
  }

  function isVerifiedEmbeddedMatch(html, key, index) {
    if (/verified/i.test(key)) return true;
    return enclosingJsonKeys(html, index).some((k) => /verified/i.test(k));
  }

  function extractEmbedded(keys) {
    if (!htmlCache) htmlCache = document.documentElement.innerHTML || '';
    const out = {};
    for (const [metric, keyList] of Object.entries(keys)) {
      for (const key of keyList) {
        const re = new RegExp(`(?:\\\\?["'])${key}(?:\\\\?["'])\\s*[:=]\\s*["']?(\\d[\\d,.]*[KkMmBb万亿]?)`, 'gi');
        let m;
        let accepted = null;
        while ((m = re.exec(htmlCache))) {
          // followers 绝不能读 verified_followers_count 或 verified 对象里的 followers 键
          if (metric === 'followers' && isVerifiedEmbeddedMatch(htmlCache, key, m.index)) continue;
          const n = parseCount(m[1]);
          if (n !== null) {
            accepted = n;
            break;
          }
        }
        if (accepted !== null) {
          out[metric] = accepted;
          break;
        }
      }
    }
    return out;
  }

  function extractAllMetrics(scope) {
    const buttons = extractButtons(scope);
    const aria = extractAriaMetrics(scope);
    const labels = extractByLabels(scope);
    const embedded = extractEmbedded(EMBED_KEYS);
    const metrics = {};
    const raw = { buttons, aria: aria.aria, labels: labels.labels, embedded_used: [] };
    for (const key of Object.keys(LABELS)) {
      // 优先级：按钮上的互动数 > aria-label > 可见标签 > 内嵌数据
      if (buttons[key] !== undefined) metrics[key] = buttons[key];
      else if (aria.metrics[key] !== undefined) metrics[key] = aria.metrics[key];
      else if (labels.metrics[key] !== undefined) metrics[key] = labels.metrics[key];
      else if (embedded[key] !== undefined) {
        metrics[key] = embedded[key];
        raw.embedded_used.push(key);
      }
    }
    if (metrics.views === undefined) {
      const v = extractVisibleViews(scope);
      if (v !== null) metrics.views = v;
    }
    if (Object.keys(metrics).length === 0) {
      const emb = extractEmbedded({ fallback_views: ['impression_count', 'view_count'] });
      if (emb.fallback_views !== undefined) metrics.views = emb.fallback_views;
    }
    return { metrics, raw };
  }

  function detectLoggedInHandle() {
    const patterns = [
      'a[data-testid="AppTabBar_Profile_Link"]',
      'a[aria-label="Profile"]',
      'a[aria-label="个人资料"]',
      'a[data-testid="SideNav_AccountSwitcher_Button"]',
      'a[data-testid*="Profile"]',
    ];
    for (const sel of patterns) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const href = el.getAttribute('href') || '';
      const m = href.match(/^\/([A-Za-z0-9_]+)\/?$/);
      if (m) return m[1];
      const text = textOf(el);
      const tm = text.match(/@([A-Za-z0-9_]{1,15})/);
      if (tm) return tm[1];
    }
    // 新版导航：Primary nav 里 aria-label=Profile 的链接就是当前登录账号
    const nav = document.querySelector('nav[aria-label="Primary"], nav');
    if (nav) {
      for (const a of nav.querySelectorAll('a[href^="/"]')) {
        const al = normLabel(a.getAttribute('aria-label') || '');
        if (al !== 'profile' && al !== '个人资料' && al !== '主页') continue;
        const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9_]+)\/?$/);
        if (m) return m[1];
      }
    }
    const switchBtn = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
    if (switchBtn) {
      const tm = textOf(switchBtn).match(/@([A-Za-z0-9_]{1,15})/);
      if (tm) return tm[1];
    }
    return null;
  }

  function pageType() {
    const path = location.pathname.replace(/\/+$/, '') || '/';
    const isAccountAnalytics = /\/i\/account_analytics/i.test(path) || /\/account_analytics/i.test(path);
    const statusMatch = path.match(/\/status\/(\d{10,25})/i);
    const analyticsPostMatch = path.match(/\/(?:i\/)?(?:account_analytics\/post|tweet_analytics|tweet)\/(\d{10,25})/i)
      || path.match(/\/status\/(\d{10,25})\/analytics/i);
    const profileMatch = path.match(/^\/([A-Za-z0-9_]{1,15})$/);
    if (isAccountAnalytics && (analyticsPostMatch || statusMatch)) {
      return { kind: 'post_analytics', postId: (analyticsPostMatch || statusMatch)[1] };
    }
    if (isAccountAnalytics) {
      // 兜底：/i/account_analytics/<id>、?post_id=<id> 等未枚举形态
      const anyId = path.match(/(\d{10,25})/);
      const q = new URLSearchParams(location.search || '');
      const qid = q.get('post_id') || q.get('tweet_id') || q.get('id');
      const chosen = anyId ? anyId[1] : (qid && /^\d{10,25}$/.test(qid) ? qid : null);
      if (chosen) return { kind: 'post_analytics', postId: chosen };
      return { kind: 'account_analytics' };
    }
    if (analyticsPostMatch) return { kind: 'post_analytics', postId: analyticsPostMatch[1] };
    if (statusMatch) return { kind: 'post', postId: statusMatch[1] };
    if (profileMatch && profileMatch[1].toLowerCase() === TARGET.toLowerCase()) {
      return { kind: 'profile', handle: profileMatch[1] };
    }
    return { kind: 'other' };
  }

  function extractPosts() {
    const articles = Array.from(document.querySelectorAll(
      'article[data-testid="tweet"], article[role="article"], article',
    ));
    const seen = new Set();
    const items = [];
    for (const art of articles) {
      let href = '';
      const timeEl = art.querySelector('time[datetime]');
      if (timeEl) {
        const anchor = timeEl.closest('a[href*="/status/"]');
        if (anchor) href = anchor.getAttribute('href') || '';
      }
      if (!href) {
        const links = Array.from(art.querySelectorAll('a[href*="/status/"]'))
          .map((a) => a.getAttribute('href') || '')
          .filter((h) => /^\/[A-Za-z0-9_]+\/status\/\d{10,25}/.test(h));
        href = links.find((h) => h.toLowerCase().startsWith(`/${TARGET.toLowerCase()}/status/`)) || links[0] || '';
      }
      const m = href.match(/^\/([A-Za-z0-9_]+)\/status\/(\d{10,25})/);
      if (!m) continue;
      if (m[1].toLowerCase() !== TARGET.toLowerCase()) continue;
      const id = m[2];
      if (seen.has(id)) continue;
      let textEl = art.querySelector('[data-testid="tweetText"]') || art.querySelector('[lang]');
      // 新版 X UI 正文是没有 testid 的 div[dir=auto]，用 whitespace-pre-wrap 优先兜底
      if (!textEl) {
        const dirNodes = Array.from(art.querySelectorAll('div[dir="auto"]'));
        textEl = dirNodes.find((n) => /whitespace-pre-wrap|text-body/.test(String(n.className || '')))
          || dirNodes.find((n) => {
            const t = String(n.innerText || n.textContent || '').trim();
            return t && !t.startsWith('@') && t.length >= 2;
          })
          || null;
      }
      const text = textEl ? String(textEl.innerText || textEl.textContent || '').trim() : '';
      if (!text) continue;
      const createdAt = timeEl ? timeEl.getAttribute('datetime') : null;
      items.push({
        id,
        url: `https://x.com/${TARGET}/status/${id}`,
        text: text.slice(0, MAX_TEXT),
        created_at: createdAt,
        author: TARGET,
      });
      seen.add(id);
      if (items.length >= MAX_POSTS) break;
    }
    return items;
  }

  function makePayloads(kind, payload) {
    return [{ type: kind, account: TARGET_AT, captured_at: new Date().toISOString(), ...payload }];
  }

  function analyticsScope() {
    return document.querySelector('main') || document.body;
  }

  function findPostIdOnPage() {
    const timeLink = document.querySelector('article a[href*="/status/"] time, article[data-testid="tweet"] a[href*="/status/"] time');
    const anchor = timeLink ? timeLink.closest('a[href*="/status/"]') : null;
    if (anchor) {
      const m = (anchor.getAttribute('href') || '').match(/\/status\/(\d{10,25})/);
      if (m) return m[1];
    }
    const any = document.querySelector('a[href*="/status/"]');
    if (any) {
      const m = (any.getAttribute('href') || '').match(/\/status\/(\d{10,25})/);
      if (m) return m[1];
    }
    return null;
  }

  function metricsPayload(postId, scope, pageLabel) {
    const { metrics, raw } = extractAllMetrics(scope || document);
    // 只保留至少一个有效数字的情况；0 是有效值
    if (!Object.keys(metrics).length) return null;
    return makePayloads('metrics', {
      post_id: postId,
      url: `https://x.com/${TARGET}/status/${postId}`,
      metrics,
      raw: {
        page: pageLabel,
        page_url: location.href,
        ...raw,
      },
    })[0];
  }

  async function waitFor(fn, timeout = 8000, interval = 250) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      try {
        if (fn()) return true;
      } catch (_) { /* ignore */ }
      await sleep(interval);
    }
    return false;
  }

  async function collect() {
    const handle = detectLoggedInHandle();
    if (!handle) {
      return {
        ok: false,
        skipped: true,
        reason: 'logged_in_handle_not_found',
        detail: '未找到已登录账号的导航元素（SideNav_AccountSwitcher_Button / AppTabBar_Profile_Link）',
        page: pageType(),
      };
    }
    if (handle.toLowerCase() !== TARGET.toLowerCase()) {
      return {
        ok: false,
        skipped: true,
        reason: 'not_target_account',
        handle,
        detail: `当前登录 @${handle}，不是 @${TARGET}，已跳过`,
        page: pageType(),
      };
    }

    const page = pageType();
    if (page.kind === 'other') {
      return { ok: true, handle, page, payloads: [], status: 'unsupported_page' };
    }

    if (page.kind === 'profile') {
      await waitFor(() => document.querySelector('article[data-testid="tweet"], article[role="article"], article'), 9000);
      await sleep(600);
      const items = extractPosts();
      if (!items.length) {
        return { ok: true, handle, page, payloads: [], status: 'no_posts_visible' };
      }
      return {
        ok: true,
        handle,
        page,
        payloads: makePayloads('posts', { items }),
        status: `posts:${items.length}`,
      };
    }

    if (page.kind === 'post') {
      await waitFor(() => document.querySelector('article[data-testid="tweet"], article[role="article"], article'), 9000);
      await sleep(800);
      const art = document.querySelector('article[data-testid="tweet"], article[role="article"], article') || document;
      const payload = metricsPayload(page.postId, art, 'post_detail');
      if (!payload) {
        return { ok: true, handle, page, payloads: [], status: 'no_metrics_visible' };
      }
      return { ok: true, handle, page, payloads: [payload], status: 'post_metrics' };
    }

    if (page.kind === 'post_analytics') {
      await waitFor(() => document.body && document.body.innerText.length > 80, 9000);
      await sleep(1800);
      const postId = page.postId || findPostIdOnPage();
      if (!postId) {
        return { ok: true, handle, page, payloads: [], status: 'no_post_id' };
      }
      const payload = metricsPayload(postId, analyticsScope(), 'post_analytics');
      if (!payload) {
        return { ok: true, handle, page, payloads: [], status: 'no_metrics_visible' };
      }
      return { ok: true, handle, page, payloads: [payload], status: 'analytics_post_metrics' };
    }

    if (page.kind === 'account_analytics') {
      await waitFor(() => document.body && document.body.innerText.length > 80, 9000);
      await sleep(1800);
      const scope = analyticsScope();
      const { metrics } = extractAllMetrics(scope);
      const followers = metrics.followers;
      const verified = metrics.verified_followers;
      if (followers === undefined && verified === undefined) {
        return { ok: true, handle, page, payloads: [], status: 'no_followers_visible' };
      }
      const payload = makePayloads('account', {
        followers: followers === undefined ? null : followers,
        verified_followers: verified === undefined ? null : verified,
        raw: {
          page: 'account_analytics',
          page_url: location.href,
          followers_verified_found: [followers, verified],
        },
      })[0];
      return { ok: true, handle, page, payloads: [payload], status: 'account_followers' };
    }

    return { ok: true, handle, page, payloads: [], status: 'unhandled' };
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== 'XCOLLECT_COLLECT') return undefined;
    collect()
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({
        ok: false,
        error: String(err && err.message ? err.message : err),
        page: (() => { try { return pageType(); } catch (_) { return null; } })(),
      }));
    return true;
  });

  // 页面内只读自检：popup「立即采集」会通过 background 发消息触发；
  // 不主动上报，避免用户没配置令牌时产生无意义请求。
  window.__xcollectContentReady = true;
  // 仅在 content script 的隔离世界内暴露，供本地纯函数自测/排障；页面 JS 读不到。
  try {
    globalThis.__xcollectInternals = {
      parseCount, pageType, detectLoggedInHandle, extractPosts,
      extractByLabels, extractAriaMetrics, extractAllMetrics,
    };
  } catch (_) { /* ignore */ }
})();
