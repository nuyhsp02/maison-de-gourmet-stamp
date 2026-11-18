'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Element } = require('./app-harness.cjs');

const code = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8');
const session = {csrf:'test-only-csrf', actor:'fixture', expires_at:Math.floor(Date.now()/1000) + 60};
const statistics = {cards:1, stamps:1, redeemed:0, distribution:[0,1,0,0,0,0,0,0,0,0,0]};
const customer = {name:'가상 손님', customer_id:'synthetic-card', redeemed_at:null,
  stamps:[{name:'시연 라떼', product_id:'example-latte', collected_at:500, revision:2}]};
const ok = body => ({ok:true, status:200, json:async () => body});
const fail = (status, error) => ({ok:false, status, json:async () => ({error})});
const tick = () => new Promise(resolve => setImmediate(resolve));

async function open(handler) {
  const nodes = new Map(), requests = [], events = {};
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  element('search-type').value = 'phone';
  const document = {getElementById:element, createElement:tag => Object.assign(new Element(tag), {style:{}}),
    addEventListener:(type, f) => { events[type] = f; }};
  const context = {document, window:{addEventListener:(type,f) => { events[type] = f; }},
    setTimeout:() => 1, clearTimeout:() => {}, fetch:async (url, opts) => {
      const body = opts.body && JSON.parse(opts.body);
      requests.push({url, opts, body});
      return handler(url.split('/').at(-1), body);
    }};
  vm.runInNewContext(code, context);
  await tick();
  return {element, requests, events, async find(value) {
    element('query').value = value;
    await element('search-form').fire('submit');
  }};
}
const normal = route => {
  if (route === 'session' || route === 'login') return ok(session);
  if (route === 'stats') return ok(statistics);
  if (route === 'search') return ok(customer);
  if (route === 'logout') return ok({ok:true});
  throw new Error('unexpected mock route');
};

test('staff login clears password, fetches protected stats only after authentication', async () => {
  const ui = await open(route => route === 'session' ? fail(401, '로그인 필요') : normal(route));
  assert.deepEqual(ui.requests.map(r=>r.url), ['/api/admin/session']);
  assert.equal(ui.element('workspace').hidden, true);
  ui.element('password').value = 'test value from form';
  await ui.element('login-form').fire('submit');
  assert.equal(ui.element('password').value, '');
  assert.equal(ui.element('workspace').hidden, false);
  assert.equal(ui.element('cards').textContent, '1장');
  assert.equal(ui.element('distribution').children.length, 11);
  assert.deepEqual(ui.requests[1].body, {password:'test value from form'});
  assert.ok(!ui.requests[1].url.includes('test'));
});

test('phone search uses POST and staff CSRF, then scrubs input, results and token on logout', async () => {
  const ui = await open(normal);
  await ui.find('010-0000-0001');
  const search = ui.requests.at(-1);
  assert.equal(search.url, '/api/admin/search');
  assert.equal(search.opts.method,'POST');
  assert.equal(search.opts.headers['X-Stamp-Client'],'1');
  assert.equal(search.opts.headers['X-Admin-CSRF'], session.csrf);
  assert.deepEqual(search.body, {phone:'010-0000-0001'});
  assert.equal(ui.element('query').value, '');
  assert.equal(ui.element('card-name').textContent, customer.name);
  await ui.element('logout').fire('click');
  assert.equal(ui.element('card-name').textContent, '');
  assert.equal(ui.element('product-rows').children.length, 0);
  assert.equal(ui.element('workspace').hidden, true);
});

test('each cancellation requires selecting, confirming and sending that exact stamp revision', async () => {
  let cancelled = false;
  const ui = await open(route => {
    if (route === 'cancel') { cancelled = true; return ok({ok:true}); }
    if (route === 'search' && cancelled) return ok({...customer,stamps:[]});
    return normal(route);
  });
  await ui.find('01000000001');
  const row = ui.element('product-rows').children[0];
  const button = row.children[2].children[0];
  await button.fire('click');
  assert.ok(ui.element('target').textContent.includes(customer.name));
  assert.ok(ui.element('target').textContent.includes('시연 라떼'));
  assert.equal(cancelled, false);
  await ui.element('keep').fire('click');
  assert.equal(ui.element('target').textContent, '');
  assert.equal(cancelled, false);
  await button.fire('click');
  await ui.element('cancel-stamp').fire('click');
  const [cancel] = ui.requests.filter(r => r.url.endsWith('/cancel'));
  assert.deepEqual(cancel.body,{customer_id:'synthetic-card',product_id:'example-latte',revision:2});
  assert.equal(cancel.opts.headers['X-Admin-CSRF'],session.csrf);
  assert.equal(ui.element('product-rows').children.length,0);
  assert.equal(ui.element('confirmation').open,false);
});

test('409 refreshes paid-out card and removes all cancellation actions', async () => {
  let paid = false;
  const ui = await open(route => {
    if (route === 'cancel') { paid = true; return fail(409, '이미 지급'); }
    if (route === 'search' && paid) return ok({...customer,redeemed_at:550});
    return normal(route);
  });
  await ui.find('01000000001');
  await ui.element('product-rows').children[0].children[2].children[0].fire('click');
  await ui.element('cancel-stamp').fire('click');
  assert.ok(ui.element('card-state').textContent.includes('수령 완료'));
  assert.equal(ui.element('product-rows').children[0].children[2].children.length,0);
});

test('session expiry clears detail and metrics; late protected search cannot restore UI after logout', async () => {
  let resolveSearch;
  const ui = await open(route => route === 'search' ? new Promise(resolve => {resolveSearch = resolve;}) : normal(route));
  const pending = ui.find('01000000001');
  await tick();
  await ui.element('logout').fire('click');
  resolveSearch(ok(customer));
  await pending;
  assert.equal(ui.element('detail').hidden,true);
  assert.equal(ui.element('card-name').textContent,'');
  assert.equal(ui.element('distribution').children.length,0);
  const expired = await open(route => route === 'search' ? fail(401, '세션 만료') : normal(route));
  await expired.find('01000000001');
  assert.equal(expired.element('workspace').hidden,true);
  assert.equal(expired.element('login-panel').hidden,false);
});

test('rotated admin CSRF clears results, statistics and credentials before another staff login', async () => {
  let first = true;
  const ui = await open(route => {
    if (route === 'search' && !first) return {ok:false,status:403,
      json:async () => ({error:'직원 세션 재확인',code:'admin_csrf_failed'})};
    return normal(route);
  });
  await ui.find('01000000001');
  assert.equal(ui.element('card-name').textContent, customer.name);
  first = false;
  await ui.find('01000000002');
  assert.equal(ui.element('login-panel').hidden, false);
  assert.equal(ui.element('detail').hidden, true);
  assert.equal(ui.element('card-name').textContent, '');
  assert.equal(ui.element('query').value, '');
  assert.equal(ui.element('distribution').children.length, 0);
});

test('a committed cancellation with failed stats holds searches until the older card read settles', async () => {
  let committed = false, finishEarlierCard;
  const nextPhone = '01000000002', nextName = '다음 가상 고객';
  const ui = await open((route, body) => {
    if (route === 'cancel') { committed = true; return ok({ok:true}); }
    if (route === 'stats' && committed) return fail(503, '집계 지연');
    if (route === 'search' && body.customer_id) {
      return new Promise(resolve => { finishEarlierCard = resolve; });
    }
    if (route === 'search' && body.phone === nextPhone) return ok({...customer, name:nextName});
    return normal(route);
  });
  await ui.find('01000000001');
  await ui.element('product-rows').children[0].children[2].children[0].fire('click');
  const pending = ui.element('cancel-stamp').fire('click');
  await tick();
  assert.equal(ui.element('search').disabled, true);
  await ui.find(nextPhone);
  assert.ok(!ui.requests.some(r => r.body?.phone === nextPhone));
  finishEarlierCard(ok({...customer,stamps:[]}));
  await pending;
  assert.ok(ui.element('message').textContent.includes('스탬프 1개를 취소했습니다.'));
  assert.ok(ui.element('message').textContent.includes('재조회'));
  await ui.find(nextPhone);
  assert.equal(ui.element('card-name').textContent, nextName);
});
