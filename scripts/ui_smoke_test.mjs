/**
 * UI smoke test: drive the real demo_client page in jsdom against a live deployment.
 *
 * Why this exists on top of the API-level probe:
 *   scripts/acceptance_probe.py proves the *backend* contract. It says nothing
 *   about whether the page that a person actually clicks can reach it. A single
 *   typo in an element id, a wrong relative path, or a missing global would
 *   leave every API check green and the demo dead on arrival.
 *
 * It loads the HTML that the server actually serves — not the local file — so
 * the relative-path resolution under the `/support/` prefix is exercised for
 * real. `fetch` is bridged to Node's implementation, which means the SSE stream
 * is a genuine ReadableStream and the page's `res.body.getReader()` path runs
 * unmodified.
 *
 * Usage:
 *   NODE_TLS_REJECT_UNAUTHORIZED=0 node scripts/ui_smoke_test.mjs https://124.222.190.3/support/
 *
 * Requires `jsdom` (npm i jsdom). It is loaded through CommonJS resolution so
 * that a `NODE_PATH` install location is honoured — ESM `import` ignores
 * NODE_PATH entirely, which makes the package unreachable in a sandbox that
 * keeps its node_modules outside the project.
 *
 * Exit code 0 only if every check passes.
 */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let JSDOM;
let VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.error('jsdom is required for this test. Install it with:  npm i jsdom');
  console.error('(or set NODE_PATH to a directory that contains it)');
  console.error(String(e.message || e));
  process.exit(2);
}

const BASE = (process.argv[2] || 'https://124.222.190.3/support/').replace(/\/?$/, '/');
const TENANT = 'tenant-demo';
const EMAIL = 'customer@demo.local';
const PASSWORD = 'DemoCustomer123';

const pass = [];
const fail = [];

function check(name, ok, detail = '') {
  (ok ? pass : fail).push(name);
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' — ' + detail : ''}`);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** Poll until `fn()` is truthy or the deadline passes. */
async function waitFor(fn, timeoutMs = 20000, label = 'condition') {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if (await fn()) return true;
    } catch {
      /* keep polling */
    }
    await sleep(120);
  }
  console.log(`  (timeout waiting for ${label})`);
  return false;
}

async function main() {
  console.log(`\n=== 前端 UI 冒烟测试: ${BASE} ===\n`);

  const html = await fetch(BASE, { redirect: 'follow' }).then((r) => r.text());
  // Report *both* units. ``html.length`` counts UTF-16 code units, not bytes —
  // this page is mostly Chinese, so the two numbers differ by ~2 per CJK
  // character. Printing only the character count next to a word like "字节"
  // makes it look like the deployed page is smaller than the file on disk
  // (15247 bytes vs 14829), which reads as staleness when it is nothing of the
  // kind. The byte count is what docs/PRODUCT.md quotes, so print it too.
  check('拉取到页面', html.includes('<html') && html.includes('id="login"'),
    `${html.length} 字符 / ${Buffer.byteLength(html, 'utf8')} 字节`);

  const virtualConsole = new VirtualConsole();
  const pageErrors = [];
  virtualConsole.on('jsdomError', (e) => pageErrors.push(String(e.message || e)));
  virtualConsole.on('error', (...a) => pageErrors.push(a.join(' ')));

  const dom = new JSDOM(html, {
    url: BASE,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;

  // jsdom ships no fetch and no TextDecoder. Both are needed by the page: the
  // first for every API call, the second to decode the SSE stream.
  window.fetch = (input, init) =>
    fetch(new URL(String(input), window.location.href).toString(), init);
  window.TextDecoder = TextDecoder;
  window.TextEncoder = TextEncoder;

  await sleep(400);

  const $ = (id) => window.document.getElementById(id);
  check('脚本已执行（元素可查）', !!$('l-submit') && !!$('send'));

  // -- 1. login ------------------------------------------------------------ //
  console.log('\n[1] 登录');
  $('l-tenant').value = TENANT;
  $('l-email').value = EMAIL;
  $('l-password').value = PASSWORD;
  $('l-submit').dispatchEvent(new window.Event('click', { bubbles: true }));

  const loggedIn = await waitFor(() => $('login').classList.contains('hidden'), 20000, '登录完成');
  check('登录后进入主界面', loggedIn, `l-err="${$('l-err').textContent}"`);
  check('令牌已写入 localStorage', !!window.localStorage.getItem('support.token'));
  check('身份条显示租户', ($('identity').textContent || '').includes(TENANT),
    $('identity').textContent);

  // -- 2. chat over SSE ---------------------------------------------------- //
  console.log('\n[2] 对话（SSE 流式）');
  $('msg').value = '退款一般多久到账？';
  $('send').dispatchEvent(new window.Event('click', { bubbles: true }));

  const replied = await waitFor(() => {
    const bubbles = window.document.querySelectorAll('#thread .bubble.assistant');
    if (!bubbles.length) return false;
    const text = bubbles[bubbles.length - 1].textContent || '';
    return text && text !== '…';
  }, 30000, '助手回答');

  const assistant = window.document.querySelectorAll('#thread .bubble.assistant');
  const answer = assistant.length ? assistant[assistant.length - 1].textContent : '';
  check('收到助手回答', replied, `${answer.slice(0, 60)}…`);
  check('回答与退款到账相关', /退款|到账|工作日/.test(answer), answer.slice(0, 80));
  check('用户气泡已渲染',
    (window.document.querySelectorAll('#thread .bubble.user').length || 0) >= 1);

  // The button is re-enabled at the end of the promise chain, *after* a
  // loadConversations() round trip. Checking it the instant the text stops
  // changing races that request and fails spuriously.
  const reEnabled = await waitFor(() => $('send').disabled === false, 15000, '发送按钮复位');
  check('回答完成后发送按钮恢复', reEnabled);

  // -- 2b. decision inspector: the plan and its gate ledger ---------------- //
  // The refund-confirm turn is the only turn that builds a three-step plan, so
  // it is the only one with cross-step edges and a type dependency to show.
  // Every assertion below is about the plan's *shape*, which the planner derives
  // from the message alone — so they hold whether or not ORD-1001 is still
  // refundable, which matters because the demo order is stateful (a previous
  // acceptance run may already have refunded it).
  console.log('\n[2b] 决策检视：plan 校验台账');
  $('msg').value = '确认退款 ORD-1001 现在就退';
  $('send').dispatchEvent(new window.Event('click', { bubbles: true }));

  const planRendered = await waitFor(
    () => window.document.querySelector('#inspect .plan') !== null, 30000, 'plan 面板');
  const plan = window.document.querySelector('#inspect .plan');
  const planText = plan ? (plan.textContent || '') : '';
  check('plan 面板已渲染', planRendered,
    planText.trim().replace(/\s+/g, ' ').slice(0, 90));

  const steps = plan ? plan.querySelectorAll('.step') : [];
  check('plan 显示三步', steps.length === 3, `${steps.length} 步`);
  check('plan 含资格检查步骤', planText.includes('check_aftersale_eligibility'));
  check('plan 含创建申请步骤', planText.includes('create_refund_proposal'));
  check('plan 含执行退款步骤', planText.includes('execute_refund'));
  check('plan 显示依赖边', /依赖\s*→/.test(planText));
  // The edge that makes the money path auditable: execute_refund's proposal_id
  // is shown as coming from the proposal step, not from the caller.
  check('plan 显示 proposal_id 来自前序步骤',
    /参数\s+proposal_id\s*←\s*step-2\.proposal_id/.test(planText),
    (planText.match(/参数\s+proposal_id\s*←\s*\S+/) || ['未找到'])[0]);

  const gates = plan ? Array.from(plan.querySelectorAll('.gate')) : [];
  check('八个 gate 全部列出', gates.length === 8, `${gates.length} 个`);
  check('无 gate 失败', plan ? plan.querySelectorAll('.gate.no').length === 0 : false,
    plan ? (plan.querySelector('.gate.no') || {}).textContent || '' : '无 plan');
  const badge = plan ? plan.querySelector('.badge') : null;
  check('校验结论为通过', badge ? badge.textContent === '校验通过' : false,
    badge ? badge.textContent : '无 badge');

  // -- 3. tabs ------------------------------------------------------------- //
  console.log('\n[3] 审批与指标面板');
  const tabs = window.document.querySelectorAll('.tab');
  check('三个 Tab 存在', tabs.length === 3, `${tabs.length} 个`);

  const approvalsTab = Array.from(tabs).find((t) => t.dataset.view === 'approvals');
  approvalsTab.dispatchEvent(new window.Event('click', { bubbles: true }));
  const approvalsRendered = await waitFor(
    () => !$('view-approvals').classList.contains('hidden')
      && !($('approvals').textContent || '').includes('加载中'),
    20000, '审批列表');
  check('审批面板渲染完成', approvalsRendered,
    ($('approvals').textContent || '').trim().slice(0, 60));

  const metricsTab = Array.from(tabs).find((t) => t.dataset.view === 'metrics');
  metricsTab.dispatchEvent(new window.Event('click', { bubbles: true }));
  const metricsRendered = await waitFor(
    () => !$('view-metrics').classList.contains('hidden')
      && !($('metrics').textContent || '').includes('加载中'),
    25000, '指标面板');
  check('指标面板渲染完成', metricsRendered,
    ($('metrics').textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80));

  // -- 4. reviewer sees the approval queue --------------------------------- //
  console.log('\n[4] 以 reviewer 身份登录');
  $('logout').dispatchEvent(new window.Event('click', { bubbles: true }));
  await sleep(200);
  check('退出后回到登录页', !$('login').classList.contains('hidden'));
  check('退出后令牌被清除', !window.localStorage.getItem('support.token'));

  $('l-tenant').value = TENANT;
  $('l-email').value = 'reviewer@demo.local';
  $('l-password').value = 'DemoReviewer123';
  $('l-submit').dispatchEvent(new window.Event('click', { bubbles: true }));
  check('reviewer 登录成功',
    await waitFor(() => $('login').classList.contains('hidden'), 20000, 'reviewer 登录'),
    `l-err="${$('l-err').textContent}"`);

  const reviewerTab = Array.from(window.document.querySelectorAll('.tab'))
    .find((t) => t.dataset.view === 'approvals');
  reviewerTab.dispatchEvent(new window.Event('click', { bubbles: true }));
  // `role_required` renders as "one of [...] required"; nothing else does.
  // Waiting on that string rather than on "the box is non-empty" matters — the
  // panel still held the previous session's 403 text, so a naive wait matched
  // instantly and reported the old failure as the new result.
  const queueOk = await waitFor(() => {
    const text = ($('approvals').textContent || '').trim();
    return text && !text.includes('加载中') && !text.includes('required');
  }, 20000, 'reviewer 审批队列');
  const queueText = ($('approvals').textContent || '').trim().replace(/\s+/g, ' ');
  check('reviewer 能看到审批队列（未被 403）', queueOk, queueText.slice(0, 90));

  check('页面无脚本错误', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));

  console.log(`\n=== 结果: ${pass.length} 通过 / ${fail.length} 失败 ===`);
  if (fail.length) {
    console.log('失败项:');
    for (const name of fail) console.log(`  - ${name}`);
    process.exitCode = 1;
    return;
  }
  console.log('全部通过 ✅');
}

main().catch((e) => {
  console.error('harness error:', e);
  process.exitCode = 2;
});
