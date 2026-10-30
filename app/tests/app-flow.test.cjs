'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { openApp, URL_A } = require('./app-harness.cjs');

test('new visitor is never created automatically; explicit button creates then collects', async () => {
  const app = await openApp({ customer: false });
  assert.equal(app.posts('/api/session').length, 0);
  assert.equal(app.posts('/api/collect').length, 0);
  await app.click('collect-button');
  assert.equal(app.posts('/api/session').length, 1);
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.server.customer.count, 1);
});
test('valid returning visitor auto POSTs exactly once and sends CSRF', async () => {
  const app = await openApp();
  assert.equal(app.posts('/api/session').length, 0);
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.posts('/api/collect')[0].headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(app.location.pathname, '/');
  await app.visibility(true); await app.visibility(false);
  await app.windowEvent('pageshow', { persisted: true });
  await app.click('retry-load');
  assert.equal(app.posts('/api/collect').length, 1);
});
for (const [name, options] of Object.entries({ reload: { navigation: 'reload' }, back: { navigation: 'back_forward' },
  prefetch: { eligible: false }, hidden: { hidden: true }, unfocused: { focused: false },
  prerender: { prerender: true }, activatedPrerender: { activationStart: 1 },
  missingNavigation: { navigation: null }, attempted: { history: { cafeCollectionAutoAttempted: true } } })) {
  test('automatic collection blocked for ' + name + ', manual fallback works', async () => {
    const app = await openApp(options);
    assert.equal(app.posts('/api/collect').length, 0);
    await app.visibility(false);
    assert.equal(app.posts('/api/collect').length, 0);
    await app.click('collect-button');
    assert.equal(app.posts('/api/collect').length, 1);
    assert.equal(app.posts('/api/session').length, 0);
  });
}
test('already-collected product shows status without another POST', async () => {
  const app = await openApp({ server: { customer: { stamps: { p0: 1 }, count: 1, status: 'collecting' }, phase: 'demo', reward_open: true } });
  assert.equal(app.posts('/api/collect').length, 0);
  assert.equal(app.element('collect-button').disabled, true);
  assert.match(app.element('pending-help').textContent, /이미/);
});
test('lost automatic response is read back; retry and reload do not auto POST again', async () => {
  const app = await openApp({ loseCollectResponse: true });
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.server.customer.count, 1);
  assert.equal(app.history.state.cafeCollectionAutoAttempted, true);
  await app.click('retry-load'); await app.visibility(false);
  assert.equal(app.posts('/api/collect').length, 1);
  const reloaded = await openApp({ server: app.server, navigation: 'reload', history: app.history.state });
  assert.equal(reloaded.posts('/api/collect').length, 0);
});
test('expired session during auto POST never falls through to customer creation', async () => {
  const app = await openApp({ collect401: true });
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.posts('/api/session').length, 0);
  assert.equal(app.server.customer, null);
  assert.match(app.element('collect-button').querySelector('span').textContent, /새 스탬프판/);
});
test('one reward button directly posts, no checkbox/dialog; state survives reread', async () => {
  const stamps = Object.fromEntries(Array.from({ length: 10 }, (_, i) => ['p' + i, 1]));
  const server = { customer: { stamps, count: 10, status: 'complete' }, phase: 'demo', reward_open: true };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', server });
  assert.equal(app.element('redeem-button').hidden, false);
  await Promise.all(Array.from({ length: 8 }, () => app.click('redeem-button')));
  assert.equal(app.posts('/api/redeem').length, 1);
  assert.equal(app.posts('/api/redeem')[0].body.confirm_received, true);
  assert.equal(app.element('redeem-button').hidden, true);
  assert.equal(app.element('redeem-button').disabled, true);
  assert.equal(server.customer.status, 'redeemed');
  const reopened = await openApp({ url: 'http://127.0.0.1:8765/', server });
  assert.equal(reopened.element('redeem-button').hidden, true);
  assert.equal(reopened.element('redeem-button').disabled, true);
});
for (const event of ['pagehide', 'visibility']) {
  test('navigation interrupted before session response cannot auto collect: ' + event, async () => {
    let release, first = true;
    const app = await openApp({ stateHook: () => {
      if (!first) return;
      first = false; return new Promise(resolve => { release = resolve; });
    } });
    if (event === 'pagehide') await app.windowEvent('pagehide');
    else { await app.visibility(true); await app.visibility(false); }
    release(); await app.windowEvent('pageshow', { persisted: true });
    assert.equal(app.posts('/api/collect').length, 0);
    assert.equal(app.posts('/api/session').length, 0);
  });
}
test('actual scanner loop crops native input, ignores outside/multiple codes, posts once', async () => {
  const app = await openApp({ url: 'http://127.0.0.1:8765/' });
  await app.click('scan-button');
  const qr = points => ({ rawValue: URL_A, cornerPoints: points });
  const center = [{ x: 200, y: 200 }, { x: 490, y: 200 }, { x: 490, y: 490 }, { x: 200, y: 490 }];
  app.codes([qr(center.map(p => ({ x: p.x - 500, y: p.y })))]);
  await app.frames(5);
  assert.equal(app.posts('/api/collect').length, 0);
  app.codes([qr(center), qr(center)]); await app.frames(5);
  assert.equal(app.posts('/api/collect').length, 0);
  app.codes([qr(center)]); await app.frames(8);
  assert.equal(app.posts('/api/collect').length, 1);
  assert.ok(app.drawCalls.length > 0);
  for (const call of app.drawCalls) {
    assert.equal(call.length, 9);
    assert.deepEqual(Array.from(call.slice(1, 5)), [620, 200, 680, 680]);
  }
});
test('lost reward response is reconciled to completed state without an active retry button', async () => {
  const stamps = Object.fromEntries(Array.from({ length: 10 }, (_, i) => ['p' + i, 1]));
  const server = { customer: { stamps, count: 10, status: 'complete' }, phase: 'demo', reward_open: true };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', server, loseRedeemResponse: true });
  await app.click('redeem-button');
  assert.equal(app.posts('/api/redeem').length, 1);
  assert.equal(app.element('redeem-button').hidden, true);
  assert.equal(app.element('redeem-button').disabled, true);
  assert.equal(app.element('redeem-error').textContent, '');
  assert.equal(server.customer.status, 'redeemed');
});
test('scanner closed while native decode is pending cannot collect', async () => {
  let release;
  const app = await openApp({ url: 'http://127.0.0.1:8765/', detectorHook: () =>
    new Promise(resolve => { release = resolve; }) });
  await app.click('scan-button');
  await app.click('close-scanner');
  release([{ rawValue: URL_A, cornerPoints: [
    { x: 200, y: 200 }, { x: 490, y: 200 }, { x: 490, y: 490 }, { x: 200, y: 490 }] }]);
  await app.frames(4);
  assert.equal(app.posts('/api/collect').length, 0);
});
test('actual jsQR fallback loop uses the same crop as native loop', async () => {
  let calls = 0;
  const decoder = (_pixels, width, height) => {
    assert.equal(width, 680); assert.equal(height, 680);
    if (++calls % 2 === 0) return null;
    return { data: URL_A, location: { topLeftCorner: { x: 200, y: 200 },
      topRightCorner: { x: 490, y: 200 }, bottomRightCorner: { x: 490, y: 490 }, bottomLeftCorner: { x: 200, y: 490 } } };
  };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', native: false, decoder });
  await app.click('scan-button'); await app.frames(8);
  assert.equal(app.posts('/api/collect').length, 1);
  for (const call of app.drawCalls) assert.deepEqual(Array.from(call.slice(1, 5)), [620, 200, 680, 680]);
});

test('V1.2 renders ten ornaments in original product order with season/select metadata', async () => {
  const app = await openApp({ url: 'http://127.0.0.1:8765/' });
  const ornaments = app.element('stamp-tree').children;
  assert.equal(ornaments.length, 10);
  assert.deepEqual(ornaments.map(item => item.attributes['data-product-id']), Array.from({ length: 10 }, (_, i) => 'p' + i));
  assert.deepEqual(ornaments.map(item => item.attributes.value), Array.from({ length: 10 }, (_, i) => String(i + 1)));
  assert.equal(ornaments.filter(item => item.attributes['data-category'] === 'season').length, 4);
  assert.equal(ornaments.filter(item => item.attributes['data-category'] === 'select').length, 6);
  assert.equal(app.posts('/api/collect').length, 0);
});

test('V1.2 branded tree displays restored collection without changing customer data', async () => {
  const customer = { stamps: { p0: 1, p4: 1, p9: 1 }, count: 3, status: 'collecting', recovery_saved: true };
  const server = { customer, phase: 'demo', reward_open: true };
  const before = JSON.stringify(server);
  const app = await openApp({ url: 'http://127.0.0.1:8765/', server });
  assert.equal(app.element('cafe-name').textContent, 'Maison de Gourmet');
  assert.equal(app.element('event-title').textContent, 'Winter Collection');
  assert.equal(app.element('stamp-count').textContent, 3);
  assert.equal(app.element('stamp-tree').children.filter(item => item.className.includes('collected')).length, 3);
  assert.equal(app.element('recovery-button').disabled, false);
  assert.equal(JSON.stringify(server), before);
});

test('V1.2 ten ornaments remain collected after one-button reward and reopening', async () => {
  const stamps = Object.fromEntries(Array.from({ length: 10 }, (_, i) => ['p' + i, 1]));
  const server = { customer: { stamps, count: 10, status: 'complete' }, phase: 'demo', reward_open: true };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', server });
  assert.equal(app.element('stamp-tree').children.length, 10);
  await app.click('redeem-button');
  const reopened = await openApp({ url: 'http://127.0.0.1:8765/', server });
  assert.equal(reopened.element('stamp-tree').children.filter(item => item.className.includes('collected')).length, 10);
  assert.equal(reopened.element('stamp-count').textContent, 10);
  assert.equal(reopened.element('redeem-button').disabled, true);
  assert.equal(reopened.element('redeem-button').hidden, true);
});

test('V1.3 every registered menu has a fixed decorative artwork slot and matching SVG symbol', async () => {
  const products = require('../event.json').products;
  const app = await openApp({ url: 'http://127.0.0.1:8765/', products });
  const items = app.element('stamp-tree').children;
  for (let i = 0; i < 10; i++) {
    const seal = items[i].children[0];
    const art = seal.children.find(child => child.className === 'stamp-art');
    assert.ok(art);
    assert.equal(art.attributes['aria-hidden'], 'true');
    const svg = art.children[0];
    assert.equal(svg.attributes.viewBox, '0 0 48 48');
    assert.equal(svg.attributes.preserveAspectRatio, 'xMidYMid meet');
    assert.equal(svg.children[0].attributes.href, '/static/stamp-art.svg#menu-' + products[i].id);
    assert.equal(seal.children.filter(child => child.className === 'stamp-impression').length, 0);
    assert.equal(seal.children.filter(child => child.className === 'stamp-check').length, 0);
  }
  assert.equal(app.posts('/api/collect').length, 0);
});

test('V1.3 collected items alone receive ink ring and check; all items retain artwork geometry', async () => {
  const products = require('../event.json').products;
  const collectedId = products[0].id;
  const server = { customer: { stamps: { [collectedId]: 1 }, count: 1, status: 'collecting' }, phase: 'demo', reward_open: true };
  const before = JSON.stringify(server);
  const app = await openApp({ url: 'http://127.0.0.1:8765/', products, server });
  const items = app.element('stamp-tree').children;
  for (let i = 0; i < 10; i++) {
    const seal = items[i].children[0];
    assert.equal(seal.children.filter(child => child.className === 'stamp-impression').length, i === 0 ? 1 : 0);
    assert.equal(seal.children.filter(child => child.className === 'stamp-check').length, i === 0 ? 1 : 0);
    assert.equal(seal.children.find(child => child.className === 'stamp-art').children[0].attributes.viewBox, '0 0 48 48');
    assert.match(items[i].attributes['aria-label'], i === 0 ? /수집 완료/ : /수집 전/);
  }
  assert.equal(JSON.stringify(server), before);
});

test('V1.3 unregistered product ID falls back to existing icon without changing event data', async () => {
  const products = require('../event.json').products.map(p => ({ ...p }));
  products[0].id = 'other-menu';
  const before = JSON.stringify(products);
  const app = await openApp({ url: 'http://127.0.0.1:8765/', products });
  const seal = app.element('stamp-tree').children[0].children[0];
  const art = seal.children.find(child => child.className === 'stamp-art');
  assert.equal(art.children[0].children[0].attributes.href, '#' + products[0].icon);
  assert.equal(JSON.stringify(products), before);
});
