'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { openApp, URL_A } = require('./app-harness.cjs');

const profile = { name: '시연 이름', phone: '010-0000-0001' };
function fillProfile(app) {
  app.element('identity-name').value = profile.name;
  app.element('identity-phone').value = profile.phone;
}
test('new QR visitor registers explicitly; the pending product still requires a collection POST', async () => {
  const app = await openApp({ customer: false });
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  const productTitle = app.element('pending-title').textContent;
  await app.click('collect-button');
  assert.equal(app.element('identity-panel').hidden, false);
  assert.equal(app.element('notice-acknowledged').checked, false);
  assert.equal(app.element('collection-panel').hidden, true);
  assert.equal(app.element('identity-qr').hidden, false);
  fillProfile(app);
  await app.submitIdentity();
  assert.equal(app.posts('/api/register').length, 0);
  app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  assert.equal(app.element('review-name').textContent, profile.name);
  assert.equal(app.element('review-phone').textContent, profile.phone);
  assert.equal(app.posts('/api/register').length, 0);
  await Promise.all([app.click('confirm-registration'), app.click('confirm-registration')]);
  assert.deepEqual(app.posts('/api/register').map(r => r.body), [{ ...profile, notice_acknowledged: true }]);
  assert.equal(app.element('identity-panel').hidden, true);
  assert.equal(app.element('identity-phone').value, '');
  assert.equal(app.element('identity-name').value, '');
  assert.equal(app.element('pending-panel').hidden, false);
  assert.equal(app.element('pending-title').textContent, productTitle);
  assert.equal(app.element('review-name').textContent, '');
  assert.equal(app.element('review-phone').textContent, '');
  assert.equal(app.posts('/api/collect').length, 0);
  await app.click('collect-button');
  assert.equal(app.posts('/api/session').length, 0);
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.server.customer.count, 1);
});

test('editing the registration review retains consent and registers only the reviewed correction', async () => {
  const app = await openApp({ customer: false, url: 'http://127.0.0.1:8765/' });
  fillProfile(app);
  app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  await app.submitIdentity();  // another Enter/submit cannot confirm registration
  assert.equal(app.element('registration-review').hidden, false);
  assert.equal(app.element('identity-form').hidden, true);
  await app.click('edit-registration');
  assert.equal(app.element('identity-name').value, profile.name);
  assert.equal(app.element('identity-phone').value, profile.phone);
  assert.equal(app.element('notice-acknowledged').checked, true);
  assert.equal(app.element('review-name').textContent, '');
  await app.click('confirm-registration');
  app.element('identity-name').value = '수정 <시연>';
  await app.submitIdentity();
  assert.equal(app.element('review-name').textContent, '수정 <시연>');
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  await app.click('confirm-registration');
  assert.deepEqual(app.posts('/api/register').map(r => r.body), [
    { ...profile, name: '수정 <시연>', notice_acknowledged: true }
  ]);
  assert.equal(app.posts('/api/collect').length, 0);
});

test('changed fields and withdrawn consent invalidate the review before sending', async () => {
  const app = await openApp({ customer: false, url: 'http://127.0.0.1:8765/' });
  fillProfile(app);
  app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  app.element('identity-phone').value = '010-0000-0099';
  await app.click('confirm-registration');
  assert.equal(app.element('registration-review').hidden, true);
  assert.equal(app.element('identity-form').hidden, false);
  await app.submitIdentity();
  assert.equal(app.element('review-phone').textContent, '010-0000-0099');
  app.element('notice-acknowledged').checked = false;
  await app.click('confirm-registration');
  assert.equal(app.element('review-phone').textContent, '');
  assert.equal(app.posts('/api/register').length, 0);
});

test('leaving the review clears inputs and summary before returning from browser history', async () => {
  const app = await openApp({ customer: false, url: 'http://127.0.0.1:8765/' });
  fillProfile(app);
  app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  await app.windowEvent('pagehide');
  assert.equal(app.element('review-name').textContent, '');
  assert.equal(app.element('review-phone').textContent, '');
  await app.windowEvent('pageshow', { persisted: true });
  assert.equal(app.element('registration-review').hidden, true);
  assert.equal(app.element('identity-name').value, '');
  assert.equal(app.element('identity-phone').value, '');
  assert.equal(app.element('notice-acknowledged').checked, false);
  assert.equal(app.posts('/api/register').length, 0);
});

test('offline history restore returns to a usable empty registration form after the state read fails', async () => {
  let offline = false, failedReads = 0;
  const app = await openApp({ customer: false, url: new URL('/', URL_A).href, stateHook() {
    if (offline) { failedReads++; throw new Error('offline'); }
  } });
  fillProfile(app); app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  assert.equal(app.element('registration-review').hidden, false);
  await app.windowEvent('pagehide');
  offline = true;
  await app.windowEvent('pageshow', { persisted: true });
  assert.equal(failedReads, 1);
  assert.equal(app.element('registration-review').hidden, true);
  assert.equal(app.element('identity-form').hidden, false);
  assert.equal(app.element('identity-name').value, '');
  assert.equal(app.element('identity-phone').value, '');
  assert.equal(app.element('notice-acknowledged').checked, false);
  fillProfile(app); app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  assert.equal(app.element('registration-review').hidden, false);
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
});

test('duplicate registration opens find only on request, keeping inputs out of URLs and history', async () => {
  const app = await openApp({ customer: false, registerFailure: { status: 409, code: 'phone_registered',
    error: '이미 등록된 스탬프가 있어요. 기존 스탬프 찾기로 이동해 주세요.' },
    foundCard: { stamps: { p2: 1 }, count: 1, status: 'collecting', registered: true } });
  const productTitle = app.element('pending-title').textContent;
  await app.click('collect-button');
  fillProfile(app);
  app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  await app.click('confirm-registration');
  assert.match(app.element('identity-error').textContent, /기존 스탬프 찾기/);
  assert.equal(app.element('identity-submit').hidden, true);
  await app.submitIdentity();
  assert.equal(app.posts('/api/register').length, 1);
  assert.equal(app.posts('/api/find').length, 0);
  await app.click('identity-switch');
  assert.equal(app.element('identity-submit').hidden, false);
  assert.equal(app.posts('/api/find').length, 0);
  assert.equal(app.element('registration-notice').hidden, true);
  assert.equal(app.element('identity-title').textContent, '기존 스탬프 찾기');
  assert.equal(app.element('identity-name').value, profile.name);
  assert.equal(app.element('identity-phone').value, profile.phone);
  await app.submitIdentity();
  assert.deepEqual(app.posts('/api/find').map(r => r.body), [profile]);
  assert.equal(app.element('stamp-count').textContent, 1);
  assert.equal(app.posts('/api/collect').length, 0);
  assert.equal(app.element('pending-panel').hidden, false);
  assert.equal(app.element('pending-title').textContent, productTitle);
  assert.equal(app.location.href, URL_A);
  assert.equal(app.history.state, null);
  for (const request of app.requests) {
    assert.ok(!decodeURIComponent(request.url).includes(profile.name));
    assert.ok(!request.url.includes(profile.phone));
  }
  await app.click('collect-button');
  assert.equal(app.posts('/api/collect').length, 1);
  assert.deepEqual(Object.keys(app.server.customer.stamps).sort(), ['p0', 'p2']);
});

test('failed find keeps the form and correction guidance, leaving the visitor unsigned', async () => {
  const app = await openApp({ customer: false, findFailure: { status: 404, code: 'profile_not_found',
    error: '입력 정보를 확인해 주세요. 정정은 매장에 문의해 주세요.' } });
  await app.click('collect-button');
  await app.click('identity-switch');
  fillProfile(app);
  await app.submitIdentity();
  assert.equal(app.server.customer, null);
  assert.equal(app.posts('/api/register').length, 0);
  assert.match(app.element('identity-error').textContent, /정정/);
  assert.equal(app.element('identity-panel').hidden, false);
  await app.windowEvent('pagehide');
  await app.windowEvent('pageshow', { persisted: true });
  assert.equal(app.element('identity-name').value, '');
  assert.equal(app.element('identity-phone').value, '');
});

test('blocked session cookie keeps registration screen and prevents collection', async () => {
  const app = await openApp({ customer: false, blockCookie: true });
  await app.click('collect-button');
  fillProfile(app); app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  await app.click('confirm-registration');
  assert.equal(app.element('identity-panel').hidden, false);
  assert.match(app.element('identity-error').textContent, /쿠키/);
  assert.equal(app.element('identity-submit').hidden, true);
  assert.equal(app.posts('/api/collect').length, 0);
});

test('lost signup response refreshes the existing card and waits for product confirmation', async () => {
  const app = await openApp({ customer: false, loseRegisterResponse: true });
  await app.click('collect-button');
  fillProfile(app); app.element('notice-acknowledged').checked = true;
  await app.submitIdentity();
  await app.click('confirm-registration');
  assert.equal(app.posts('/api/register').length, 1);
  assert.equal(app.server.customer.count, 0);
  assert.equal(app.element('review-phone').textContent, '');
  assert.equal(app.element('scanner-dialog').open, true);
  assert.equal(app.posts('/api/collect').length, 0);
  await app.visibility(true); await app.visibility(false);
  assert.equal(app.posts('/api/register').length, 1);
  assert.equal(app.posts('/api/collect').length, 0);
});

test('legacy recovery link restores the anonymous card without requesting registration', async () => {
  const app = await openApp({ customer: false, url: 'http://127.0.0.1:8765/recover#test-token',
    linkCard: { stamps: { p1: 1 }, count: 1, status: 'collecting', registered: false } });
  assert.equal(app.location.hash, '');
  assert.equal(app.element('restore-dialog').open, true);
  await app.click('restore-button');
  assert.equal(app.posts('/api/recover').length, 1);
  assert.equal(app.posts('/api/register').length, 0);
  assert.equal(app.element('identity-panel').hidden, true);
  assert.match(app.element('cookie-notice').textContent, /개인 복구 링크/);
});
test('formerly eligible returning visitor sends zero POSTs until confirmation, then one with CSRF', async () => {
  const app = await openApp();
  assert.equal(app.element('scanner-dialog').open, true);
  assert.equal(app.element('pending-panel').hidden, false);
  assert.match(app.element('pending-title').textContent, /상품 0/);
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(app.server.customer.count, 0);
  await Promise.all(Array.from({ length: 6 }, () => app.click('collect-button')));
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.posts('/api/collect')[0].body.qr, new URL(URL_A).pathname.slice(3));
  assert.equal(app.posts('/api/collect')[0].headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(app.server.customer.count, 1);
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
  test('product link requires an explicit confirmation after ' + name, async () => {
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
for (const [name, phase, status] of [['after event close', 'ended', 'collecting'], ['after reward', 'demo', 'redeemed']]) {
  test('confirmation cannot collect ' + name, async () => {
    const app = await openApp({ server: { customer: { stamps: {}, count: 0, status }, phase, reward_open: false } });
    assert.equal(app.element('collect-button').disabled, true);
    await app.click('collect-button'); await app.click('dismiss-pending');
    assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  });
}
test('Escape cancels external selection even when a previous product read finishes afterward', async () => {
  let release, stall = false;
  const app = await openApp({ stateHook: () => {
    if (stall) return new Promise(resolve => { release = resolve; });
  } });
  stall = true;
  await app.visibility(false);
  assert.equal(typeof release, 'function');
  await app.escapeDialog('scanner-dialog');
  release(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.element('scanner-dialog').open, false);
  assert.equal(app.location.pathname, '/');
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
});
for (const customer of [true, false]) {
  test('cancel product link without writes or reappearing after reload, customer=' + customer, async () => {
    const app = await openApp({ customer });
    const saved = JSON.stringify(app.server);
    const requests = app.requests.length;
    await app.click('dismiss-pending');
    assert.equal(app.element('scanner-dialog').open, false);
    assert.equal(app.requests.length, requests);
    assert.equal(app.location.pathname, '/');
    await app.visibility(true); await app.visibility(false);
    await app.windowEvent('pageshow', { persisted: true });
    assert.equal(app.element('scanner-dialog').open, false);
    const reopened = await openApp({ url: app.location.href, server: app.server, navigation: 'reload' });
    assert.equal(reopened.element('scanner-dialog').open, false);
    assert.equal([...app.requests, ...reopened.requests].filter(r => r.method === 'POST').length, 0);
    assert.equal(JSON.stringify(app.server), saved);
  });
}
test('old personal link restored in another tab preserves product pending on return', async () => {
  const productTab = await openApp({ customer: false });
  await productTab.click('collect-button');
  await productTab.visibility(true);
  const linkTab = await openApp({ url: 'http://127.0.0.1:8765/recover#synthetic', server: productTab.server,
    linkCard: { stamps: { p1: 1 }, count: 1, status: 'collecting', registered: false } });
  await linkTab.click('restore-button');
  await productTab.visibility(false);
  assert.equal(productTab.posts('/api/collect').length, 0);
  assert.equal(productTab.element('scanner-dialog').open, true);
  assert.match(productTab.element('pending-title').textContent, /상품 0/);
  await productTab.click('collect-button');
  assert.deepEqual(productTab.server.customer.stamps, { p1: 1, p0: 1 });
});
test('lost confirmed response is read back; retry and reload do not POST again', async () => {
  const app = await openApp({ loseCollectResponse: true });
  assert.equal(app.posts('/api/collect').length, 0);
  await app.click('collect-button');
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.server.customer.count, 1);
  await app.click('retry-load'); await app.visibility(false);
  assert.equal(app.posts('/api/collect').length, 1);
  const reloaded = await openApp({ server: app.server, navigation: 'reload', history: app.history.state });
  assert.equal(reloaded.posts('/api/collect').length, 0);
});
test('expired session at product confirmation never falls through to customer creation', async () => {
  const app = await openApp({ collect401: true });
  assert.equal(app.posts('/api/collect').length, 0);
  await app.click('collect-button');
  assert.equal(app.posts('/api/collect').length, 1);
  assert.equal(app.posts('/api/register').length, 0);
  assert.equal(app.posts('/api/session').length, 0);
  assert.equal(app.server.customer, null);
  assert.equal(app.element('identity-panel').hidden, false);
  assert.equal(app.element('pending-panel').hidden, false);
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
const center = [{ x: 200, y: 200 }, { x: 490, y: 200 }, { x: 490, y: 490 }, { x: 200, y: 490 }];
const URL_B = new URL('/q/' + 'b'.repeat(32), URL_A).href;
const cameraCode = (rawValue, cornerPoints = center) => ({ rawValue, cornerPoints });

test('native scanner ignores ambiguous frames and locks the displayed product until one confirmation', async () => {
  const app = await openApp({ url: 'http://127.0.0.1:8765/' });
  assert.equal(app.element('scanner-dialog').open, false);
  await app.click('scan-button');
  app.codes([cameraCode(URL_A, center.map(p => ({ x: p.x - 500, y: p.y })))]);
  await app.frames(5);
  app.codes([cameraCode(URL_A), cameraCode(URL_B)]); await app.frames(5);
  assert.equal(app.requests.filter(r => new URL(r.url).searchParams.has('qr')).length, 0);
  app.codes([cameraCode(URL_B)]); await app.frames(8);
  assert.equal(app.element('pending-panel').hidden, false);
  assert.match(app.element('pending-title').textContent, /상품 1/);
  app.codes([cameraCode(URL_A)]); await app.frames(8);
  assert.match(app.element('pending-title').textContent, /상품 1/);
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  await Promise.all(Array.from({ length: 5 }, () => app.click('collect-button')));
  assert.deepEqual(app.posts('/api/collect').map(r => r.body.qr), ['b'.repeat(32)]);
  assert.deepEqual(app.server.customer.stamps, { p1: 1 });
  assert.equal(app.element('scan-results').children.length, 1);
  assert.equal(app.element('scanner-dialog').open, true);
  app.codes([cameraCode(URL_B)]); await app.frames(7);
  assert.equal(app.element('pending-panel').hidden, true);
  assert.equal(app.posts('/api/collect').length, 1);
  assert.ok(app.drawCalls.length > 0);
  for (const call of app.drawCalls) {
    assert.equal(call.length, 9);
    assert.deepEqual(Array.from(call.slice(1, 5)), [620, 200, 680, 680]);
  }
});
test('cancelled camera selection does not re-open until QR leaves the frame and can be confirmed later', async () => {
  const app = await openApp({ url: 'http://127.0.0.1:8765/' });
  const before = JSON.stringify(app.server);
  await app.click('scan-button');
  app.codes([cameraCode(URL_A)]); await app.frames(6);
  assert.equal(app.element('pending-panel').hidden, false);
  await app.click('dismiss-pending');
  app.codes([cameraCode(URL_A)]); await app.frames(8);
  // Ambiguity or an occasional missed frame does not establish that the customer removed the code.
  app.codes([cameraCode(URL_A), cameraCode(URL_B)]); await app.frames(4);
  app.codes([]); await app.frames(1);
  app.codes([cameraCode(URL_A)]); await app.frames(6);
  assert.equal(app.element('pending-panel').hidden, true);
  assert.equal(app.element('scanner-dialog').open, true);
  assert.equal(app.element('scan-results').children.length, 0);
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(JSON.stringify(app.server), before);
  app.codes([]); await app.frames(4);
  app.codes([cameraCode(URL_A)]); await app.frames(6);
  assert.equal(app.element('pending-panel').hidden, false);
  await app.click('collect-button');
  assert.deepEqual(app.server.customer.stamps, { p0: 1 });
});
test('late camera product lookup cannot reopen a closed scanner', async () => {
  let release, started;
  const lookup = new Promise(resolve => { started = resolve; });
  const app = await openApp({ url: 'http://127.0.0.1:8765/', stateHook: url => {
    if (url.searchParams.has('qr')) return new Promise(resolve => { release = resolve; started(); });
  } });
  await app.click('scan-button');
  app.codes([cameraCode(URL_A)]); const frames = app.frames(5);
  await Promise.race([lookup, frames.then(() => { if (!release) assert.fail('No product lookup started'); })]);
  await app.click('close-scanner');
  release(); await frames;
  assert.equal(app.element('scanner-dialog').open, false);
  assert.equal(app.posts('/api/collect').length, 0);
  assert.deepEqual(app.server.customer.stamps, {});
});
test('unknown product never opens confirmation or writes, and a known product remains scannable', async () => {
  const app = await openApp({ url: 'http://127.0.0.1:8765/' });
  await app.click('scan-button');
  app.codes([cameraCode(new URL('/q/' + 'z'.repeat(32), URL_A).href)]); await app.frames(6);
  assert.equal(app.element('pending-panel').hidden, true);
  assert.match(app.element('scanner-status').textContent, /QR/);
  app.codes([cameraCode(URL_B)]); await app.frames(6);
  assert.match(app.element('pending-title').textContent, /상품 1/);
  await app.click('dismiss-pending');
  assert.equal(app.requests.filter(r => r.method === 'POST').length, 0);
  assert.deepEqual(app.server.customer.stamps, {});
});
test('reopened scanner can reconfirm a staff-cancelled last stamp and enable one-time reward', async () => {
  const customer = { stamps: Object.fromEntries(Array.from({ length: 9 }, (_, i) => ['p' + i, 1])),
    count: 9, status: 'collecting' };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', server: { customer, phase: 'demo', reward_open: true } });
  const lastProduct = new URL('/q/' + 'j'.repeat(32), URL_A).href;
  for (let round = 0; round < 2; round++) {
    await app.click('scan-button');
    app.codes([cameraCode(lastProduct)]); await app.frames(6);
    assert.match(app.element('pending-title').textContent, /상품 9/);
    assert.equal(app.posts('/api/collect').length, round);
    await app.click('collect-button');
    assert.equal(app.element('scanner-dialog').open, false);
    assert.equal(customer.count, 10);
    assert.equal(app.element('redeem-button').hidden, false);
    if (round === 0) {
      // Reflect a pre-reward cancellation made on the separate staff device.
      delete customer.stamps.p9; customer.count = 9; customer.status = 'collecting';
      await app.visibility(true); await app.visibility(false);
      assert.equal(app.element('reward-panel').hidden, true);
    }
  }
  await app.click('redeem-button'); await app.click('redeem-button');
  assert.equal(app.posts('/api/collect').length, 2);
  assert.equal(app.posts('/api/redeem').length, 1);
  assert.equal(customer.status, 'redeemed');
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
test('jsQR fallback uses the same crop and confirmation controls as the native loop', async () => {
  let calls = 0;
  const decoder = (_pixels, width, height) => {
    assert.equal(width, 680); assert.equal(height, 680);
    if (++calls % 2 === 0) return null;
    return { data: URL_A, location: { topLeftCorner: { x: 200, y: 200 },
      topRightCorner: { x: 490, y: 200 }, bottomRightCorner: { x: 490, y: 490 }, bottomLeftCorner: { x: 200, y: 490 } } };
  };
  const app = await openApp({ url: 'http://127.0.0.1:8765/', native: false, decoder });
  await app.click('scan-button'); await app.frames(8);
  assert.equal(app.element('pending-panel').hidden, false);
  assert.match(app.element('pending-title').textContent, /상품 0/);
  assert.equal(app.posts('/api/collect').length, 0);
  await app.click('collect-button');
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
