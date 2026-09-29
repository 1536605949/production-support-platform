/**
 * DOM smoke test for the C-end page (``demo_client/customer.html``).
 *
 * Why this is a separate script from ``ui_smoke_test.mjs``
 * -------------------------------------------------------
 * The two frontends have *opposite* contracts about the same data. The operator
 * console (``index.html``) is required to show intent, confidence, retrieved
 * passages, tool names, timings and the trace id — that is the whole point of
 * it. The C-end page is required to show *none* of that: a shopper must never
 * see "置信度 0.42" or "degraded=true".
 *
 * So the interesting assertion here is a negative one, and a negative assertion
 * is only worth anything if it is checked against the DOM that was actually
 * rendered — not against the page's source text. A page can pass a
 * "does the source mention 置信度" grep and still print it at runtime, because
 * the string was assembled from parts or came back from the API.
 *
 * Hence: run the real page in jsdom, drive real turns, then read
 * ``document.body.textContent``.
 *
 * The other reason this script exists: the C-end page had only ever been
 * verified as "HTTP 200 and the title is there". This project's own history
 * says that is not evidence — the UI layer of the gate has caught defects
 * pytest could not, and a page whose script dies on an undefined global still
 * returns 200.
 *
 * Run through the wrapper so jsdom is located in exactly one place:
 *
 *     bash scripts/ui_smoke.sh https://host/support scripts/ui_smoke_customer.mjs
 *
 * ESM ``import`` ignores ``NODE_PATH``, so jsdom is loaded through
 * ``createRequire`` — same reason as the operator-console smoke.
 */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
if (!BASE) {
  console.error('用法: node scripts/ui_smoke_customer.mjs https://host/support/');
  process.exit(2);
}

const PAGE_URL = new URL('customer.html', BASE).href;

let passed = 0;
const failures = [];

function check(desc, ok, detail) {
  if (ok) {
    passed += 1;
    console.log(`  [PASS] ${desc}${detail ? ' — ' + detail : ''}`);
  } else {
    failures.push(desc);
    console.log(`  [FAIL] ${desc}${detail ? ' — ' + detail : ''}`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Poll until ``fn()`` is truthy. A throw inside ``fn`` counts as "not yet". */
async function waitFor(label, fn, ms = 8000) {
  const t0 = Date.now();
  for (;;) {
    let v = false;
    try { v = fn(); } catch { v = false; }
    if (v) return;
    if (Date.now() - t0 > ms) throw new Error(`超时等待：${label}`);
    await sleep(25);
  }
}

// ---------------------------------------------------------------------------
// Fetch the page over the real link. Loading it from disk would skip the edge
// nginx prefix rewrite, and the page's relative ``api/...`` URLs resolve against
// its own URL — a wrong prefix is exactly what would break in production.
// ---------------------------------------------------------------------------
const pageRes = await fetch(PAGE_URL);
const html = await pageRes.text();
// ``html.length`` is UTF-16 code units, not bytes; the page is mostly Chinese,
// so the two differ by ~2 per CJK character. Print both: the byte figure is
// the one docs/PRODUCT.md quotes (15247), and a log that shows only 14829
// "字节" invites the false conclusion that the served page is stale.
check('拉取到 C 端页面', pageRes.ok && html.length > 1000,
  `${html.length} 字符 / ${Buffer.byteLength(html, 'utf8')} 字节`);

// ---------------------------------------------------------------------------
// Scaffolding has to exist *before* the page script runs, which is why these are
// declared here and wired in ``beforeParse`` rather than after construction:
// attaching the error listener afterwards would miss anything thrown at parse
// time — the single most likely failure mode for a single-file page.
// ---------------------------------------------------------------------------
const scriptErrors = [];
const requests = [];

/**
 * Two channels, because they do not see the same things.
 *
 * Measured on jsdom 30.0.1: a window ``error`` listener *does* receive syntax
 * errors as well as runtime ones (``const a=1`` then ``const a=2`` reports
 * "Identifier 'a' has already been declared"). So that listener alone is not
 * vacuous. But ``window.onerror`` never sees ``console.error``, and jsdom's own
 * complaints (unsupported APIs, unhandled rejections inside its own machinery)
 * only surface on the virtual console. A page that logs an error and carries on
 * would pass with one channel and fail with two, so both are wired.
 */
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => {
  const m = String((e && e.message) || e);
  // jsdom does not implement navigation. This page links to the operator
  // console, and clicking it in a real browser is fine — flagging it here would
  // make the "no script errors" assertion permanently red, which is how such an
  // assertion ends up being switched off.
  if (m.indexOf('Not implemented: navigation') >= 0) return;
  scriptErrors.push(`jsdomError: ${m}`);
});
vc.on('error', (...a) => scriptErrors.push(`console.error: ${a.join(' ')}`));

/** What the fake backend answers next. Each section replaces this. */
let chatPlan = () => sseBody([{ event: 'final', answer: '（默认）', conversation_id: 'c-1' }]);

function sseBody(events) {
  const enc = new TextEncoder();
  const frames = events.map((e) => `data: ${JSON.stringify(e)}\n\n`);
  let i = 0;
  return {
    getReader: () => ({
      read: () => (i < frames.length
        ? Promise.resolve({ done: false, value: enc.encode(frames[i++]) })
        : Promise.resolve({ done: true })),
    }),
  };
}

const dom = new JSDOM(html, {
  url: PAGE_URL,
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(win) {
    win.addEventListener('error', (e) => scriptErrors.push(String(e.message)));
    // jsdom ships no TextDecoder/TextEncoder and no fetch; the page uses all
    // three. This is scaffolding, not a stub of the behaviour under test — the
    // SSE framing the page has to parse is real, byte for byte.
    win.TextEncoder = TextEncoder;
    win.TextDecoder = TextDecoder;
    win.fetch = async (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      requests.push({ url: String(url), body, headers: (opts && opts.headers) || {} });

      if (String(url).endsWith('api/auth/login')) {
        return {
          ok: true, status: 200,
          text: async () => JSON.stringify({ access_token: 'smoke-token', tenant_id: 'tenant-demo' }),
        };
      }
      if (String(url).endsWith('api/chat/stream')) {
        const plan = chatPlan(body);
        if (plan && plan.__status) {
          return { ok: false, status: plan.__status, text: async () => JSON.stringify(plan.__body || {}) };
        }
        return { ok: true, status: 200, body: plan };
      }
      return { ok: false, status: 404, text: async () => 'not found' };
    };
  },
});

const { window } = dom;
const doc = window.document;
const $ = (id) => doc.getElementById(id);

/**
 * Text a user can actually read.
 *
 * ``document.body.textContent`` is *not* that: the page is a single file, so its
 * inline ``<script>`` lives inside ``<body>`` and ``textContent`` happily returns
 * the JavaScript source. That is not hypothetical — it is how this file's first
 * version reported four phantom "leaks" of ``intent``, ``confidence``,
 * ``get_sku`` and ``get_order``: the page's own explanatory comment names all
 * four. A negative assertion that reads source comments is worse than no
 * assertion, because it will be "fixed" by deleting the comment.
 *
 * So walk the tree and skip SCRIPT/STYLE/NOSCRIPT.
 */
function visibleText(node) {
  let out = '';
  for (const child of (node || doc.body).childNodes) {
    if (child.nodeType === 3) {
      out += child.nodeValue;
    } else if (child.nodeType === 1) {
      const tag = child.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
      out += visibleText(child);
    }
  }
  return out;
}
const text = () => visibleText(doc.body);

const chatRequests = () => requests.filter((r) => r.url.endsWith('api/chat/stream'));

/**
 * Send one turn and wait for it to *settle*.
 *
 * Waiting only for the request to go out is not enough: ``send()`` guards on a
 * module-level ``streaming`` flag, so a second click while the first stream is
 * still open is silently dropped and the assertion then fails for a reason that
 * has nothing to do with the page. ``disabled`` is set synchronously inside the
 * click handler, so waiting for it to return to false is a reliable settle
 * signal — including on the error paths, whose final ``.then`` always runs.
 */
async function turn(message) {
  const before = chatRequests().length;
  $('msg').value = message;
  $('send').click();
  await waitFor('请求发出', () => chatRequests().length > before);
  await waitFor('本轮结束', () => $('send').disabled === false);
  return chatRequests().pop();
}

// ---------------------------------------------------------------------------
console.log('\n[1] 初始状态：只有登录框，没有任何内部信息');
// ---------------------------------------------------------------------------
check('登录框可见', !$('login').classList.contains('hidden'));
check('主界面隐藏', $('app').classList.contains('hidden'));
check('身份条为未登录', $('identity').textContent === '未登录', $('identity').textContent);
check('有回到运营台的入口', !!doc.querySelector('a[href="index.html"]'));
check('页面无脚本错误', scriptErrors.length === 0, scriptErrors.join(' | '));

// ---------------------------------------------------------------------------
console.log('\n[2] 登录');
// ---------------------------------------------------------------------------
$('l-submit').click();
await waitFor('登录后进入主界面', () => !$('app').classList.contains('hidden'));
check('登录后进入主界面', !$('app').classList.contains('hidden'), `l-err="${$('l-err').textContent}"`);
check('登录框已隐藏', $('login').classList.contains('hidden'));
check('身份条显示租户', $('identity').textContent === '已登录 · tenant-demo', $('identity').textContent);
check('令牌写入 localStorage', window.localStorage.getItem('support.token') === 'smoke-token');
check('登录请求带 tenant-demo',
  requests.some((r) => r.url.endsWith('api/auth/login') && r.body && r.body.tenant_id === 'tenant-demo'));
check('登录请求不带 Authorization 头',
  requests.filter((r) => r.url.endsWith('api/auth/login')).every((r) => !r.headers.Authorization));

// ---------------------------------------------------------------------------
console.log('\n[3] 对话：SSE 流式渲染');
// ---------------------------------------------------------------------------
chatPlan = () => sseBody([
  { event: 'start' },
  { event: 'delta', text: '这个订单' },
  { event: 'delta', text: '已经发货了。' },
  { event: 'final', answer: '这个订单已经发货了。', conversation_id: 'c-42' },
]);

await turn('我的订单到哪了？');
check('用户气泡已渲染', !!doc.querySelector('.bubble.user'),
  (doc.querySelector('.bubble.user') || {}).textContent);
check('助手气泡已渲染', !!doc.querySelector('.bubble.assistant'));
check('回答由多个 delta 累积而成', text().includes('这个订单已经发货了。'));
check('空态提示已移除', !doc.querySelector('.empty'));
check('回答完成后发送按钮恢复', $('send').disabled === false);
check('发送框已清空', $('msg').value === '');
check('请求带 Bearer 令牌',
  chatRequests().some((r) => r.headers.Authorization === 'Bearer smoke-token'));
check('请求带 X-Demo-Tenant',
  chatRequests().some((r) => r.headers['X-Demo-Tenant'] === 'tenant-demo'));

// The conversation id comes back in the ``final`` event and must be echoed on
// the next turn — otherwise every turn starts a new conversation server-side
// and the page silently loses its own history.
const second = await turn('那什么时候能到？');
check('第二轮带上 conversation_id', second.body.conversation_id === 'c-42',
  String(second.body.conversation_id));

// ---------------------------------------------------------------------------
console.log('\n[4] 业务事实渲染成卡片');
// ---------------------------------------------------------------------------
chatPlan = () => sseBody([
  { event: 'delta', text: '这款有货。' },
  {
    event: 'final', answer: '这款有货。', conversation_id: 'c-42',
    tool: {
      tool: 'get_sku', status: 'executed',
      result: {
        sku_id: 'SKU-1001', name: '纯棉基础款圆领T恤', spec: 'M / 白色',
        stock: 128, price: 99.0, status: 'on_sale', in_stock: true,
      },
    },
  },
]);
await turn('SKU-1001 还有货吗？');
await waitFor('商品卡渲染', () => !!doc.querySelector('.card .price'));
const skuCard = doc.querySelector('.card');
check('渲染出商品卡', !!skuCard);
check('卡片含商品名', text().includes('纯棉基础款圆领T恤'));
check('卡片含规格', text().includes('M / 白色'));
check('价格按元格式化', text().includes('¥99.00'),
  (skuCard && skuCard.querySelector('.price') || {}).textContent);
check('库存渲染成现货标签', text().includes('现货 128 件'));

// The order card is the other card type, and the one that once printed
// "金额 None" on the deterministic path.
chatPlan = () => sseBody([
  { event: 'delta', text: '查到了。' },
  {
    event: 'final', answer: '查到了。', conversation_id: 'c-42',
    tool: {
      tool: 'get_order', status: 'executed',
      result: { order_id: 'ORD-1001', status: 'shipped', amount: 199.0, refund_available: true },
    },
  },
]);
await turn('订单 ORD-1001 到哪了');
await waitFor('订单卡渲染', () => text().includes('订单 ORD-1001'));
check('渲染出订单卡', text().includes('订单 ORD-1001'));
check('订单金额按元格式化', text().includes('¥199.00'));
check('订单状态翻译成中文', text().includes('已发货'));
check('退款资格渲染成标签', text().includes('可申请退款'));
check('订单卡不出现 None', !text().includes('None'));

// ---------------------------------------------------------------------------
console.log('\n[5] 管线内部信息一律不出现在 C 端 DOM 里');
// ---------------------------------------------------------------------------
// Checked against the *rendered* DOM, after real turns, not against the source
// file. These are the exact fields the operator console shows and this page must
// not.
//
// First, guard the guard. ``localStorage`` appears in the page's inline script
// and nowhere in its visible text, so if it shows up here the extractor is
// reading source again and every assertion below is vacuous.
check('文本提取确实跳过了内联脚本（否则下面每条负向断言都是空的）',
  !text().includes('localStorage'));

for (const leaked of [
  '决策检查器', 'intent_source', 'intent', '置信度', 'confidence',
  'trace_id', 'request_id', 'degraded', 'handoff', 'rrf', 'bm25',
  'get_sku', 'get_order', 'check_aftersale_eligibility', 'executed',
  '工具', '来源', '检索',
]) {
  check(`C 端不渲染内部字段「${leaked}」`, !text().includes(leaked));
}

// ---------------------------------------------------------------------------
console.log('\n[6] 快捷按钮');
// ---------------------------------------------------------------------------
const quick = Array.from(doc.querySelectorAll('#quick button[data-say]'));
check('三个快捷按钮存在', quick.length === 3, `${quick.length} 个`);
check('快捷按钮文案正确', quick.map((b) => b.textContent).join('/') === '查物流/申请退货/查库存',
  quick.map((b) => b.textContent).join('/'));

const beforeQuick = chatRequests().length;
quick[2].click();
await waitFor('快捷按钮发出请求', () => chatRequests().length > beforeQuick);
await waitFor('本轮结束', () => $('send').disabled === false);
check('快捷按钮发送预设文案', chatRequests().pop().body.message === 'SKU-1001 还有货吗？');

// ---------------------------------------------------------------------------
console.log('\n[7] 流中断（没有 final 事件）要显式提示，而不是把半截回答当完整');
// ---------------------------------------------------------------------------
chatPlan = () => sseBody([{ event: 'delta', text: '这个订单' }]);
await turn('再说一次');
await waitFor('中断提示出现', () => !!doc.querySelector('.retry'));
check('渲染中断提示', text().includes('回答没有发完'),
  (doc.querySelector('.retry') || {}).textContent);
check('中断后发送按钮仍恢复', $('send').disabled === false);

// ---------------------------------------------------------------------------
console.log('\n[8] 令牌过期（401）要退回登录页');
// ---------------------------------------------------------------------------
chatPlan = () => ({
  __status: 401,
  __body: { detail: '令牌已过期', error: { code: 'unauthorized', message: '登录已过期，请重新登录' } },
});
await turn('还在吗');
await waitFor('退回登录页', () => !$('login').classList.contains('hidden'));
check('401 后退回登录页', !$('login').classList.contains('hidden'));
check('401 后清除令牌', window.localStorage.getItem('support.token') === null);
check('401 后提示可读（取 error.message，不是状态码）',
  $('l-err').textContent.includes('登录已过期'), $('l-err').textContent);

check('全程无脚本错误', scriptErrors.length === 0, scriptErrors.join(' | '));

// ---------------------------------------------------------------------------
console.log('\n[9] 守卫的守卫：错误捕获通道本身是活的');
// ---------------------------------------------------------------------------
// "No script errors" is the assertion most likely to rot into a no-op: if a
// future jsdom stops reporting through these channels, or someone drops the
// wiring, it stays green while checking nothing. So run one deliberately broken
// page through the *same* wiring and require that it is caught.
const canary = [];
const canaryVc = new VirtualConsole();
canaryVc.on('jsdomError', (e) => canary.push(String((e && e.message) || e)));
new JSDOM('<script>window.__canary = notDefinedAtAll;</script>', {
  runScripts: 'dangerously',
  virtualConsole: canaryVc,
  beforeParse(win) { win.addEventListener('error', (e) => canary.push(String(e.message))); },
});
check('故意写错的页面会被抓到（否则「无脚本错误」是空的）', canary.length > 0,
  canary.slice(0, 2).join(' | '));

// ---------------------------------------------------------------------------
console.log(`\n=== 结果: ${passed} 通过 / ${failures.length} 失败 ===`);
if (failures.length) {
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log('全部通过 ✅');
