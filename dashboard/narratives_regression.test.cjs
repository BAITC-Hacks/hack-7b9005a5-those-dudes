const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { init, structuredHtml, viewModel } = require('./dist/narratives.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const runId = 'a'.repeat(32);

function element() {
  const classes = new Set();
  return {
    dataset: {}, handlers: {}, textContent: '', checked: false, disabled: false, value: '',
    classList: { add: name => classes.add(name), contains: name => classes.has(name), toggle(name, on) { if (on ?? !classes.has(name)) classes.add(name); else classes.delete(name); } },
    addEventListener(name, fn) { this.handlers[name] = fn; }
  };
}

function harness(responses) {
  const elements = new Map();
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); };
  const calls = [], scheduled = new Map();
  const window = { handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; } };
  get('#csvExplanationScope').value = 'all';
  let timer = 0;
  const controller = init({
    window, document: { querySelector: get }, totalNodes: 2248,
    context: { apiUrl: endpoint => `${endpoint}?run=${runId}` },
    timers: { setTimeout(fn) { scheduled.set(++timer, fn); return timer; }, clearTimeout(id) { scheduled.delete(id); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      assert(response, `unexpected request ${url}`);
      return { ok: response.ok ?? true, status: response.code ?? 200, json: async () => response.payload };
    }
  });
  return { get: name => get(`#csvExplanation${name}`), calls, scheduled, controller, window };
}
const idle = { status: 'idle', ready: true, configured: true, total: 0, completed: 0, generated_count: 0, available_nodes: 2248 };
const running = { ...idle, status: 'running', total: 2248, completed: 3, generated_count: 3, pending: 2245, scope: 'all' };

test('page load only reads status and never posts a paid generation request', async () => {
  const h = harness([{ payload: idle }]);
  await tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, `/api/explanations/status?run=${runId}`);
  assert.equal(h.calls[0].options.method, 'GET');
  assert.equal(h.get('Start').disabled, true);
  assert.equal(h.scheduled.size, 0);
  await h.controller.start();
  assert.equal(h.calls.length, 1, 'programmatic click still requires visible checkbox consent');
});

test('explicit consent starts run-pinned all-node API generation and status polling', async () => {
  const h = harness([{ payload: idle }, { payload: running }, { payload: running }]);
  await tick();
  h.get('Consent').checked = true;
  h.get('Consent').handlers.change();
  assert.equal(h.get('Start').disabled, false);
  await h.controller.start();
  const request = h.calls[1];
  assert.equal(request.url, `/api/explanations/start?run=${runId}`);
  assert.equal(request.options.headers['X-HackAlem-Action'], 'explain-batch');
  assert.deepEqual(JSON.parse(request.options.body), { scope: 'all', consent: true });
  assert.equal(h.get('Start').disabled, true);
  assert.equal(h.get('Consent').checked, false);
  assert.equal(h.get('Cancel').disabled, false);
  assert.equal(h.get('Progress').value, 3);
  assert.equal(h.get('Progress').max, 2248);
  assert.equal(h.scheduled.size, 1);
});

test('top scope only requests top nodes; completion does not imply all nodes are explained', async () => {
  const complete = { ...idle, status: 'complete', scope: 'top', total: 50, completed: 50, generated_count: 50 };
  const h = harness([{ payload: idle }, { payload: complete }, { payload: complete }]);
  await tick();
  h.get('Consent').checked = true;
  h.get('Scope').value = 'top';
  await h.controller.start();
  assert.equal(JSON.parse(h.calls[1].options.body).scope, 'top');
  assert.match(h.get('Coverage').textContent, /50 из 2\D248/);
  assert.match(h.get('Status').textContent, /выбранных узлов/);
  assert.equal(h.scheduled.size, 0);
});

test('reopening an active run resumes only status polling without another API start', async () => {
  const h = harness([{ payload: running }]);
  await tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.scheduled.size, 1);
  assert.equal(h.get('Consent').disabled, true);
  assert.equal(h.get('Scope').value, 'all');
  h.window.handlers.pagehide();
  assert.equal(h.scheduled.size, 0);
});

test('missing key disables batch start while preserving already generated counts', async () => {
  const h = harness([{ payload: { ...idle, ready: false, configured: false, generated_count: 5 } }]);
  await tick();
  h.get('Consent').checked = true;
  h.get('Consent').handlers.change();
  assert.equal(h.get('Start').disabled, true);
  assert.match(h.get('Status').textContent, /OPENAI_API_KEY/);
  assert.match(h.get('Coverage').textContent, /5 из/);
  await h.controller.start();
  assert.equal(h.calls.length, 1);
});

test('API authorization failure remains visible after the following status refresh', async () => {
  const h = harness([{ payload: idle }, { ok: false, code: 401, payload: { message: 'OpenAI: ключ не принят.' } }, { payload: idle }]);
  await tick();
  h.get('Consent').checked = true;
  await h.controller.start();
  assert.equal(h.get('Status').textContent, 'OpenAI: ключ не принят.');
  assert.equal(h.get('Badge').textContent, 'Запуск не подтверждён');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('lost start response is recovered by a read without duplicating the paid start', async () => {
  const h = harness([{ payload: idle }, new Error('Соединение прервано'), { payload: running }]);
  await tick();
  h.get('Consent').checked = true;
  await h.controller.start();
  assert.equal(h.get('Badge').textContent, 'Генерация…');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.get('Start').disabled, true);
  assert.equal(h.scheduled.size, 1);
});

test('cancellation keeps generated counts and displays saved partial results', async () => {
  const cancelled = { ...running, status: 'cancelled', can_resume: true };
  const h = harness([{ payload: running }, { payload: cancelled }]);
  await tick();
  await h.controller.cancel();
  assert.equal(h.calls[1].url, `/api/explanations/cancel?run=${runId}`);
  assert.equal(h.get('Start').textContent, 'Продолжить через API');
  assert.equal(h.get('Progress').value, 3);
  assert.equal(h.scheduled.size, 0);
  assert.match(h.get('Status').textContent, /сохранены/);
});

test('temporary status connection failure never starts a duplicate paid task', async () => {
  const h = harness([{ payload: running }, new Error('Нет соединения')]);
  await tick();
  await h.controller.refresh();
  assert.match(h.get('Status').textContent, /повторно не отправляется/);
  assert(h.calls.every(call => call.options.method === 'GET'));
  assert.equal(h.scheduled.size, 1);
});

test('node explanations render evidence, why, limitations and JSON as escaped plain text', () => {
  const html = structuredHtml({
    explanation: '<img src=x onerror=alert(1)>',
    evidence: ['in_deg=6', { observed: 0.94, interpretation: '<script>bad()</script>' }],
    priority_explanation: 'Нужна проверка связи двух потоков.',
    alternative_explanation: 'Альтернативная роль слабее по профилю <script>bad()</script>.',
    limitations: ['Нет внешних входов', 'depth=4'],
    analyst_next_step: 'Запросить следующее окно.',
    evidence_json: '[{"metric":"fifo_1d","value":0.94,"note":"</pre><script>bad()</script>"}]'
  });
  assert(!html.includes('<img'));
  assert(!html.includes('<script>'));
  for (const title of ['Почему назначена эта роль', 'Числовые основания', 'Почему этот приоритет', 'Сравнение с альтернативной ролью', 'Ограничения гипотезы', 'Следующий шаг аналитика', 'Структурированные основания']) assert(html.includes(title));
  assert(html.includes('&lt;script&gt;'));
  assert(html.includes('0.94'));
  assert.equal(structuredHtml(null), '');
});

test('partial failures are not rendered as complete and malformed counts are clamped', () => {
  const view = viewModel({ status: 'partial', completed: 8, failed: 2, total: 10, available_nodes: 2248, generated_count: 8, can_resume: true }, 0);
  assert.equal(view.badge, 'Частично готово');
  assert.equal(view.warning, true);
  assert.equal(view.button, 'Продолжить через API');
  assert.equal(view.pending, 0);
  assert.equal(viewModel({ total: -1, completed: 'bad' }, 5).total, 0);
});

test('completed sample or top-only jobs never imply full-dataset API coverage', () => {
  for (const count of [2, 3, 50]) {
    const view = viewModel({ status: 'complete', total: count, completed: count, available_nodes: 2248, generated_count: count }, 0);
    assert.equal(view.badge, 'Выбранные узлы готовы');
    assert.match(view.coverage, new RegExp(`^API: ${count} из 2\\D248 узлов$`));
  }
  assert.equal(viewModel({ status: 'complete', total: 2248, completed: 2248, available_nodes: 2248, generated_count: 2248 }, 0).badge, 'Завершено');
});

test('dashboard ships the explanation panel and initializes it after the dataset is ready', () => {
  const html = fs.readFileSync(path.join(__dirname, 'dist/index.html'), 'utf8');
  const app = fs.readFileSync(path.join(__dirname, 'dist/app.js'), 'utf8');
  assert(html.includes('id="csvExplanationPanel"'));
  assert(html.includes('id="csvExplanationConsent" type="checkbox"'));
  assert(!html.includes('id="csvExplanationConsent" type="checkbox" checked'));
  assert(html.indexOf('/narratives.js') < html.indexOf('/bootstrap.js'));
  assert(app.includes('window.HackAlemNarratives?.init()'));
  assert(!app.includes('Без ключа — локальное объяснение'));
  assert(app.includes('новые ответы оплачиваются по тарифу API'));
});
