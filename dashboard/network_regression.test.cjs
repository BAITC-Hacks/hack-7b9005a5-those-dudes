// Exercise the real renderer and exported data with a minimal canvas/DOM adapter.
// No browser, dependencies, or changes to production exports are required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const bundle = fs.readFileSync(path.join(__dirname, 'dist/dashboard_data.js'), 'utf8');
const D = JSON.parse(bundle.slice('window.HACKALEM_DATA='.length).trim().replace(/;$/, ''));
const source = fs.readFileSync(path.join(__dirname, 'dist/app.js'), 'utf8');
const renderer = source.slice(source.indexOf('  const network = {'), source.indexOf('  function sparklineForNode'));

function harness(width = 900, height = 500) {
  let size = { width, height };
  let observer;
  const drawn = [];
  const ctx = new Proxy({
    clearRect: () => { drawn.length = 0; },
    arc: (x, y, radius) => drawn.push({ x, y, radius }),
  }, { get: (object, key) => object[key] ?? (() => {}) });
  const elements = new Map();
  const opened = [];
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      style: {}, classList: { add() {}, remove() {} },
      handlers: {}, addEventListener(name, callback) { this.handlers[name] = callback; },
      parentElement: { getBoundingClientRect: () => size },
      getBoundingClientRect: () => ({ ...size, left: 0, top: 0 }),
      getContext: () => ctx,
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    D, $, $$: () => [], window: { devicePixelRatio: 2 },
    ResizeObserver: class { constructor(callback) { observer = callback; } observe() {} },
    clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
    fmtInt: String, fmtMoney: String, escapeHtml: String, shortGid: String,
    COLORS: { depth: ['a', 'b', 'c', 'd', 'e'], cluster: ['a', 'b'], role: {} },
    nodeById: new Map(D.nodes.map(node => [node.gid, node])),
    openNode: gid => opened.push(gid),
  });
  vm.runInContext(`${renderer}\nthis.renderer = network;`, context);
  const network = context.renderer;
  network.setup();
  return { network, drawn, $, opened, resize(w, h) { size = { width: w, height: h }; observer(); } };
}

function bounds(network) {
  const points = network.visibleNodes.map(node => network.screenPosition(node.gid));
  assert(points.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)));
  return {
    minX: Math.min(...points.map(p => p.x)), maxX: Math.max(...points.map(p => p.x)),
    minY: Math.min(...points.map(p => p.y)), maxY: Math.max(...points.map(p => p.y)),
  };
}

function assertFitted(network) {
  const b = bounds(network);
  assert(b.minX >= 40 && b.maxX <= network.width - 40, JSON.stringify(b));
  assert(b.minY >= 45 && b.maxY <= network.height - 45, JSON.stringify(b));
  return b;
}

test('unknown gid has persistent visible feedback, closes previous card and recovers', () => {
  const h = harness();
  const search = h.$('#nodeSearch');
  search.setCustomValidity = value => { search.validationMessage = value; };
  h.network.selected = D.nodes[0];
  search.value = '999999999999999999';
  search.handlers.keydown({ key: 'Enter' });
  assert.match(h.$('#nodeSearchStatus').textContent, /не найден/);
  assert.equal(h.network.selected, null);
  assert.equal(h.opened.length, 0);
  search.value = D.nodes[0].gid;
  search.handlers.input();
  search.handlers.keydown({ key: 'Enter' });
  assert.equal(h.$('#nodeSearchStatus').textContent, '');
  assert.equal(search.validationMessage, '');
  assert.deepEqual(h.opened, [D.nodes[0].gid]);
});

test('initial render spreads all nodes across the measured canvas, not a single pixel', () => {
  const { network, drawn } = harness();
  assert.equal(network.visibleNodes.length, D.nodes.length);
  assert.equal(drawn.length, D.nodes.length);
  const b = assertFitted(network);
  assert(b.maxX - b.minX > 700);
  assert(b.maxY - b.minY > 300);
});

test('resizing reprojects nodes; hiding and reopening a tab preserves valid geometry', () => {
  const h = harness();
  h.resize(0, 0);
  assert.equal(h.network.width, 900);
  h.resize(440, 650);
  const b = assertFitted(h.network);
  assert(b.maxY - b.minY > 350);
  assert.equal(h.$('#networkCanvas').width, 880);
});

test('initially hidden view recovers once its canvas becomes visible', () => {
  const h = harness(0, 0);
  h.resize(900, 500);
  const b = assertFitted(h.network);
  assert(b.maxX - b.minX > 700);
});

test('each filtered cluster is centered, fills the viewport, and survives aspect changes', () => {
  const h = harness();
  h.network.filters.layout = 'cluster';
  for (const cluster of D.clusters.filter(row => row.n_nodes > 1)) {
    h.network.filters.cluster = String(cluster.cluster_id);
    h.network.applyFilters();
    const b = assertFitted(h.network);
    assert(Math.abs((b.minX + b.maxX) / 2 - h.network.width / 2) < 1e-6);
    assert(Math.abs((b.minY + b.maxY) / 2 - h.network.height / 2) < 1e-6);
    assert(Math.max((b.maxX - b.minX) / h.network.width, (b.maxY - b.minY) / h.network.height) > .35);
  }
  h.resize(420, 650);
  assertFitted(h.network);
});

test('zoom buttons and fit restore a visible graph; cursor zoom keeps its anchor', () => {
  const h = harness();
  const gid = h.network.visibleNodes[10].gid;
  const before = h.network.screenPosition(gid);
  h.network.zoomAt(1.5, before.x, before.y);
  const after = h.network.screenPosition(gid);
  assert(Math.hypot(after.x - before.x, after.y - before.y) < 1e-6);
  h.$('#zoomIn').handlers.click();
  assert(Number.parseInt(h.$('#networkZoom').textContent) > 100);
  h.$('#fitNetwork').handlers.click();
  assert.equal(h.$('#networkZoom').textContent, '100%');
  assertFitted(h.network);
});

test('filtered depth columns spread vertically instead of retaining their tiny global slice', () => {
  const h = harness();
  h.network.filters.component = String(D.components.find(row => row.n_nodes > 10 && row.n_nodes < 30).component_id);
  h.network.applyFilters();
  const b = assertFitted(h.network);
  assert(b.maxY - b.minY > 250);
  const node = h.network.visibleNodes.find(row => row.is_seed);
  h.network.focusNode(node);
  const neighborIds = new Set([node.gid]);
  h.network.visibleEdges.forEach(edge => {
    if (edge.src === node.gid || edge.dst === node.gid) { neighborIds.add(edge.src); neighborIds.add(edge.dst); }
  });
  neighborIds.forEach(gid => {
    const p = h.network.screenPosition(gid);
    assert(p.x >= 40 && p.x <= h.network.width - 40);
    assert(p.y >= 45 && p.y <= h.network.height - 45);
  });
});

test('single-node and empty filters produce finite, recoverable camera state', () => {
  const h = harness();
  h.network.filters.cluster = String(D.clusters.find(row => row.n_nodes === 1).cluster_id);
  h.network.applyFilters();
  assert.equal(h.network.visibleNodes.length, 1);
  const b = assertFitted(h.network);
  assert.equal(b.minX, h.network.width / 2);
  assert.equal(b.minY, h.network.height / 2);
  h.network.filters.depths.clear();
  h.network.applyFilters();
  assert.equal(h.network.visibleNodes.length, 0);
  assert(Number.isFinite(h.network.scale));
});
