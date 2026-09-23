const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, 'dist/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'dist/index.html'), 'utf8');
const functionSource = source.slice(source.indexOf('  function setupAi()'), source.indexOf('  setupNavigation();'));

function harness() {
  const get = () => ({ value: '', handlers: {}, dataset: {}, focus() {}, addEventListener(name, fn) { this.handlers[name] = fn; } });
  const elements = new Map();
  const $ = key => { if (!elements.has(key)) elements.set(key, get()); return elements.get(key); };
  const buttons = [...html.matchAll(/data-action='([^']+)' data-prompt="([^"]+)"/g)].map(match => Object.assign(get(), { dataset: { action: match[1], prompt: match[2] } }));
  const calls = [];
  const context = vm.createContext({ $, $$: () => buttons, location: { protocol: 'http:' },
    addAiMessage() {}, apiUrl: value => value + '?run=example',
    AbortController, setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ message: 'ok' }) }; },
  });
  vm.runInContext(functionSource + '\nsetupAi();', context);
  return { $, buttons, calls };
}

test('every quick prompt submits its structured action with explicit filters', async () => {
  const h = harness();
  assert.equal(h.buttons.length, 4);
  for (const button of h.buttons) {
    button.handlers.click();
    await h.$('#aiForm').handlers.submit({ preventDefault() {} });
    assert.deepEqual(h.calls.at(-1), { url: '/api/action?run=example', body: JSON.parse(button.dataset.action) });
  }
  assert.equal(h.calls[1].body.min_fifo_1d, .8);
});

test('editing a quick prompt discards its old structured action', async () => {
  const h = harness();
  h.buttons[1].handlers.click();
  h.$('#aiQuestion').value = 'Мой другой вопрос';
  h.$('#aiQuestion').handlers.input();
  await h.$('#aiForm').handlers.submit({ preventDefault() {} });
  assert.deepEqual(h.calls[0], { url: '/api/query?run=example', body: { question: 'Мой другой вопрос' } });
});
