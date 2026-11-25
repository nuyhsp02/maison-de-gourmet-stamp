'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { Element } = require('./app-harness.cjs');

const code = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8');
const february = Date.parse('2027-02-01T00:00:00+09:00') / 1000;
const march = Date.parse('2027-03-01T00:00:00+09:00') / 1000;
const session = {role:'staff', csrf:'test-only-csrf', actor:'fixture', server_now:february - 3600,
  expires_at:february + 3600, period:'collection', next_transition_at:february};
const statistics = {cards:1, stamps:1, redeemed:0, distribution:[0,1,0,0,0,0,0,0,0,0,0]};
const customer = {name:'가상 손님', customer_id:'synthetic-card', redeemed_at:null,
  permissions:{period:'collection',can_cancel:true,can_correct:false}, correction_revision:3,
  stamps:[{name:'시연 라떼', product_id:'example-latte', collected_at:500, revision:2}]};
const ok = body => ({ok:true, status:200, json:async () => body});
const fail = (status, error) => ({ok:false, status, json:async () => ({error})});
const tick = () => new Promise(resolve => setImmediate(resolve));

async function open(handler) {
  let elapsed = 0, wallTime = Date.now(), timerId = 0;
  const timers = new Map();
  const nodes = new Map(), requests = [], events = {};
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  element('search-type').value = 'phone';
  const document = {hidden:false, getElementById:element, createElement:tag => Object.assign(new Element(tag), {style:{}}),
    addEventListener:(type, f) => { events[type] = f; }};
  const location = { pathname: '/admin', replace(url) { this.pathname = url; } };
  const context = {document, location, crypto:webcrypto, window:{addEventListener:(type,f) => { events[type] = f; }},
    Date:class extends Date { static now() { return wallTime + elapsed; } },
    performance:{now:() => elapsed}, AbortController,
    setTimeout(fn, ms) { timers.set(++timerId, {fn, at:elapsed + ms}); return timerId; },
    clearTimeout:id => timers.delete(id), fetch:async (url, opts) => {
      const body = opts.body && JSON.parse(opts.body);
      requests.push({url, opts, body});
      return handler(url.split('/').at(-1), body, elapsed, opts.signal);
    }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/phone-input.js'), 'utf8'), context);
  vm.runInNewContext(code, context);
  await tick();
  return {element, requests, events, location,
    setWallTime(ms) { wallTime = ms - elapsed; },
    delayTimers(ms) { elapsed += ms; },
    async visibility(hidden) { document.hidden = hidden; events.visibilitychange(); await tick(); },
    async advance(ms) {
      const until = elapsed + ms;
      for (;;) {
        const first = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
        if (!first || first[1].at > until) break;
        elapsed = Math.max(elapsed, first[1].at);
        timers.delete(first[0]);
        first[1].fn();
        await tick();
      }
      elapsed = until;
    },
    async find(value) {
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
  const nextPhone = '010-0000-0002', nextName = '다음 가상 고객';
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

const manager = {...session, role:'admin', period:'correction', server_now:february, next_transition_at:march};
const absent = {name:'시연 과자', product_id:'example-cookie', collected_at:null, revision:0};
const source = customer.stamps[0];
const correctionCard = {...customer, stamps:[source, absent],
  permissions:{period:'correction',can_cancel:false,can_correct:true}};
const staffNotice = '스탬프 적립 기간이 종료되어 일반 직원의 취소가 제한됩니다. 정정이 필요한 경우 관리자에게 요청해 주세요.';
const reason = '상품 바코드를 잘못 선택했음을 확인했습니다.';
const action = (ui, index = 0) => ui.element('product-rows').children[index].children[2].children[0];
const corrections = ui => ui.requests.filter(r => r.url.endsWith('/correct'));
const cardRoutes = (account, result) => route => {
  if (route === 'session') {
    const period = result.permissions?.period || account.period;
    return ok({...account, period, next_transition_at:period === 'closed' ? null : march});
  }
  if (route === 'search') return ok(result);
  return normal(route);
};

test('manager login uses separate credential route and never sends a role', async () => {
  const ui = await open(route => {
    if (route === 'session') return fail(401, '로그인 필요');
    if (route === 'manager-login') return ok(manager);
    return normal(route);
  });
  ui.element('login-kind').value = 'manager';
  ui.element('password').value = 'synthetic credential';
  await ui.element('login-form').fire('submit');
  const login = ui.requests[1];
  assert.equal(login.url, '/api/admin/manager-login');
  assert.deepEqual(login.body, {password:'synthetic credential'});
  assert.equal(ui.element('password').value, '');
  assert.equal(ui.element('workspace').hidden, false);
});

test('after collection staff see exact closure notice and neither mutation button', async () => {
  const ui = await open(cardRoutes(session, correctionCard));
  await ui.find('01000000001');
  assert.equal(action(ui), undefined);
  assert.equal(ui.element('period-notice').textContent, staffNotice);
  await ui.element('apply-correction').fire('click');
  await ui.element('cancel-stamp').fire('click');
  assert.equal(corrections(ui).length, 0);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/cancel')).length, 0);
});

test('closed period, unavailable permissions and protected cards show no actions', async () => {
  for (const [account, card, message] of [
    [manager, {...correctionCard, permissions:{...correctionCard.permissions,period:'closed'}}, '종료'],
    [session, {...customer, permissions:{...customer.permissions,period:'closed'}}, '종료'],
    [manager, {...correctionCard, permissions:{...correctionCard.permissions,can_correct:false}}, '정정할 수 없습니다'],
    [manager, {...correctionCard, redeemed_at:550}, '정정할 수 없습니다'],
    [manager, {...customer,permissions:undefined}, '']
  ]) {
    const ui = await open(cardRoutes(account, card));
    await ui.find('01000000001');
    assert.equal(action(ui), undefined);
    assert.ok(ui.element('period-notice').textContent.includes(message));
  }
});

test('manager must review a valid reason and uncollected destination before submitting', async () => {
  const ui = await open(cardRoutes(manager, correctionCard));
  await ui.find('01000000001');
  assert.equal(action(ui).textContent, '정정');
  assert.equal(action(ui, 1), undefined);
  await action(ui).fire('click');
  assert.deepEqual(ui.element('destination').children.map(o => o.value), [absent.product_id]);
  ui.element('destination').value = absent.product_id;
  for (const invalid of ['', '짧은 사유', '가'.repeat(301)]) {
    ui.element('reason').value = invalid;
    await ui.element('correction-form').fire('submit');
    await ui.element('apply-correction').fire('click');
    assert.equal(ui.element('correction-review').hidden, true);
    assert.ok(ui.element('correction-error').textContent.includes('10~300'));
  }
  for (const personal of [customer.name + '님의 스탬프를 확인했습니다', '연락처 010-0000-0001 확인', 'help@example.invalid 주소를 확인']) {
    ui.element('reason').value = personal;
    await ui.element('correction-form').fire('submit');
    assert.equal(ui.element('correction-review').hidden, true);
    assert.ok(ui.element('correction-error').textContent.includes('개인정보'));
    assert.ok(ui.requests.every(request => !JSON.stringify(request).includes(personal)));
  }
  ui.element('reason').value = reason;
  ui.element('destination').value = source.product_id;
  await ui.element('correction-form').fire('submit');
  assert.equal(ui.element('correction-review').hidden, true);
  ui.element('destination').value = absent.product_id;
  await ui.element('correction-form').fire('submit');
  const review = ui.element('changes').textContent;
  for (const expected of [source.name, absent.name, reason, '1/10', '보상 자격']) assert.ok(review.includes(expected));
  await ui.element('revise').fire('click');
  assert.equal(ui.element('reason').value, reason);
  await ui.element('apply-correction').fire('click');
  await ui.element('discard').fire('click');
  assert.equal(ui.element('reason').value, '');
  assert.equal(corrections(ui).length, 0);
});

test('confirmed replacement posts card revision once, then renders the freshly queried card', async () => {
  let committed = false, finish;
  const after = {...correctionCard, correction_revision:4, stamps:[
    {...source,collected_at:null}, {...absent,collected_at:source.collected_at}
  ]};
  const ui = await open(route => {
    if (route === 'correct') return new Promise(resolve => { finish = () => { committed = true; resolve(ok({ok:true})); }; });
    return cardRoutes(manager, committed ? after : correctionCard)(route);
  });
  await ui.find('01000000001');
  await action(ui).fire('click');
  ui.element('reason').value = reason;
  ui.element('destination').value = absent.product_id;
  await ui.element('correction-form').fire('submit');
  assert.equal(corrections(ui).length, 0);
  const submitting = ui.element('apply-correction').fire('click');
  await ui.element('apply-correction').fire('click');
  finish();
  await submitting;
  assert.equal(corrections(ui).length, 1);
  const [sent] = corrections(ui);
  assert.ok(/^[a-f0-9]{32}$/.test(sent.body.request_id));
  assert.deepEqual(sent.body, {request_id:sent.body.request_id, customer_id:customer.customer_id,
    revision:correctionCard.correction_revision, product_id:source.product_id, type:'replace',
    new_product_id:absent.product_id, reason_code:'wrong_product', reason_detail:reason});
  assert.equal(sent.opts.headers['X-Admin-CSRF'], manager.csrf);
  assert.deepEqual(ui.requests.filter(r => r.url.endsWith('/search')).at(-1).body, {customer_id:customer.customer_id});
  assert.equal(action(ui), undefined);
  assert.equal(action(ui, 1).textContent, '정정');
  assert.equal(ui.element('reason').value, '');
});

test('remove review reflects the count change and rejection re-reads the intact card', async () => {
  const ui = await open(route => route === 'correct' ? fail(409,'카드 상태를 다시 확인해 주세요.') :
    cardRoutes(manager, correctionCard)(route));
  await ui.find('01000000001');
  const before = ui.element('card-state').textContent;
  await action(ui).fire('click');
  ui.element('correction-type').value = 'remove';
  await ui.element('correction-type').fire('change');
  assert.equal(ui.element('destination-field').hidden,true);
  ui.element('reason').value = reason;
  await ui.element('correction-form').fire('submit');
  assert.ok(ui.element('changes').textContent.includes('스탬프 제거 · 0/10'));
  await ui.element('apply-correction').fire('click');
  const [sent] = corrections(ui);
  assert.equal(sent.body.new_product_id,null);
  assert.equal(sent.body.type,'remove');
  assert.equal(sent.body.reason_code,'erroneous_stamp');
  assert.equal(ui.element('card-state').textContent,before);
  assert.equal(action(ui).textContent,'정정');
  assert.ok(ui.element('message').textContent.includes('다시 확인'));
});

test('dismissal on pagehide clears correction details and stale confirmation cannot write', async () => {
  const ui = await open(cardRoutes(manager, correctionCard));
  await ui.find('01000000001');
  await action(ui).fire('click');
  ui.element('reason').value = reason;
  ui.element('destination').value = absent.product_id;
  await ui.element('correction-form').fire('submit');
  ui.events.pagehide();
  assert.equal(ui.element('workspace').hidden, true);
  assert.equal(ui.element('changes').textContent, '');
  assert.equal(ui.element('reason').value, '');
  await ui.element('apply-correction').fire('click');
  assert.equal(corrections(ui).length, 0);
});

// The fake server uses its own epoch. Advancing the tab's monotonic timers does
// not change its wall clock offset or grant it any new permissions.
function eventServer(role, start) {
  return (route, body, elapsed) => {
    const now = start + elapsed / 1000;
    const period = now < february ? 'collection' : now < march ? 'correction' : 'closed';
    if (route === 'session') return ok({...session, role, period, server_now:now, expires_at:start + 28800,
      next_transition_at:period === 'collection' ? february : period === 'correction' ? march : null});
    if (route === 'search') return ok({...customer, correction_revision:period === 'collection' ? 1 : 7,
      stamps:[{...source}, {...absent}], permissions:{period, can_cancel:period === 'collection',
        can_correct:role === 'admin' && period === 'correction'}});
    if (route === 'correct' || route === 'cancel') return ok({ok:true});
    return normal(route);
  };
}

test('the February boundary closes a staff confirmation and refreshes the card by ID', async () => {
  const ui = await open(eventServer('staff', february - 5));
  await ui.find('01000000001');
  const oldButton = action(ui);
  assert.equal(oldButton.textContent, '취소');
  await oldButton.fire('click');
  assert.equal(ui.element('confirmation').open, true);
  await ui.advance(4999);
  assert.equal(ui.element('confirmation').open, true);
  await ui.advance(1);
  assert.equal(ui.element('confirmation').open, false);
  assert.equal(action(ui), undefined);
  assert.equal(ui.element('period-notice').textContent, staffNotice);
  assert.deepEqual(ui.requests.at(-1).body, {customer_id:customer.customer_id});
  await oldButton.fire('click');
  await ui.element('cancel-stamp').fire('click');
  assert.equal(ui.requests.filter(r => r.url.endsWith('/cancel')).length, 0);
});

test('February manager transition requires a fresh selection and sends only the new card revision', async () => {
  const ui = await open(eventServer('admin', february - 1));
  await ui.find('01000000001');
  const oldButton = action(ui);
  await oldButton.fire('click');
  await ui.advance(1000);
  assert.equal(ui.element('confirmation').open, false);
  assert.equal(action(ui).textContent, '정정');
  await oldButton.fire('click');
  assert.equal(ui.element('confirmation').open, false);
  await action(ui).fire('click');
  ui.element('correction-type').value = 'remove';
  ui.element('reason').value = reason;
  await ui.element('correction-form').fire('submit');
  await ui.element('apply-correction').fire('click');
  assert.equal(corrections(ui).length, 1);
  assert.equal(corrections(ui)[0].body.revision, 7);
});

test('March boundary scrubs the reviewed reason and cannot send the previous confirmation', async () => {
  const ui = await open(eventServer('admin', march - 2));
  await ui.find('01000000001');
  assert.equal(action(ui).textContent, '정정');
  await action(ui).fire('click');
  ui.element('correction-type').value = 'remove';
  ui.element('reason').value = reason;
  await ui.element('correction-form').fire('submit');
  assert.equal(ui.element('correction-review').hidden, false);
  await ui.advance(2000);
  assert.equal(ui.element('correction').open, false);
  assert.equal(ui.element('reason').value, '');
  assert.equal(ui.element('changes').textContent, '');
  assert.match(ui.element('period-notice').textContent, /종료/);
  assert.equal(action(ui), undefined);
  await ui.element('apply-correction').fire('click');
  assert.equal(corrections(ui).length, 0);
});

test('visibility resume after sleeping through March re-reads the active card without its old revision', async () => {
  const ui = await open(eventServer('admin', march - 1));
  await ui.find('01000000001');
  await action(ui).fire('click');
  ui.element('reason').value = reason;
  await ui.visibility(true);
  assert.equal(ui.element('correction').open, false);
  assert.equal(ui.element('card-name').textContent, '');
  assert.equal(ui.element('reason').value, '');
  const before = ui.requests.length;
  await ui.advance(8000);
  assert.equal(ui.requests.length, before);
  await ui.visibility(false);
  assert.equal(ui.element('card-name').textContent, customer.name);
  assert.equal(action(ui), undefined);
  assert.match(ui.element('period-notice').textContent, /종료/);
  assert.deepEqual(ui.requests.at(-1).body, {customer_id:customer.customer_id});
});

test('resuming under a different authenticated session cannot restore the preceding card', async () => {
  const handler = eventServer('staff', february - 45);
  let changed = false;
  const ui = await open(async (...args) => {
    const reply = handler(...args);
    if (changed && args[0] === 'session') return ok({...await reply.json(), csrf:'another-synthetic-session'});
    return reply;
  });
  await ui.find('01000000001');
  await ui.visibility(true);
  changed = true;
  const before = ui.requests.filter(r => r.url.endsWith('/search')).length;
  await ui.visibility(false);
  assert.equal(ui.element('detail').hidden, true);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/search')).length, before);
});

test('polling preserves same-period work and a wall clock jump neither grants rights nor expires the session', async () => {
  const ui = await open(eventServer('admin', february + 1));
  await ui.find('01000000001');
  await action(ui).fire('click');
  ui.element('reason').value = reason;
  ui.setWallTime(Date.parse('2031-01-01T00:00:00Z'));
  await ui.advance(30_000);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/session')).length, 2);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/search')).length, 1);
  assert.equal(ui.element('reason').value, reason);
  assert.equal(ui.element('correction').open, true);
  assert.equal(action(ui).textContent, '정정');
  const before = ui.requests.length;
  ui.setWallTime(Date.parse('2020-01-01T00:00:00Z'));
  await ui.events.focus();
  await tick();
  assert.equal(ui.requests.length, before + 1);
  assert.equal(ui.element('correction').open, true);
});

test('unreachable status on focus closes dialogs, blocks writes and recovers with a new card read', async () => {
  let hang = false;
  const handler = eventServer('admin', february + 1);
  const ui = await open((route, body, elapsed, signal) => {
    if (hang && route === 'session') return new Promise((_, reject) =>
      signal.addEventListener('abort', () => reject(new Error('read timed out'))));
    return handler(route, body, elapsed);
  });
  await ui.find('01000000001');
  await action(ui).fire('click');
  hang = true;
  ui.events.focus();
  assert.equal(ui.element('apply-correction').disabled, true);
  await ui.advance(10_000);
  assert.equal(ui.element('correction').open, false);
  assert.equal(ui.element('detail').hidden, true);
  assert.match(ui.element('message').textContent, /변경 기능을 잠갔습니다/);
  await ui.element('apply-correction').fire('click');
  assert.equal(corrections(ui).length, 0);
  hang = false;
  await ui.advance(5000);
  assert.equal(action(ui).textContent, '정정');
  assert.deepEqual(ui.requests.at(-1).body, {customer_id:customer.customer_id});
});

test('when an older session poll is in flight the deadline closes the dialog immediately', async () => {
  const handler = eventServer('admin', march - 31);
  let finish, hold = false;
  const ui = await open((route, body, elapsed) => {
    const reply = handler(route, body, elapsed);
    if (hold && route === 'session') {
      hold = false;
      return new Promise(resolve => { finish = () => resolve(reply); });
    }
    return reply;
  });
  await ui.find('01000000001');
  await action(ui).fire('click');
  hold = true;
  await ui.advance(30_000);
  assert.equal(typeof finish, 'function');
  await ui.advance(1000);
  assert.equal(ui.element('correction').open, false);
  assert.equal(ui.element('detail').hidden, true);
  finish();
  await tick();
  assert.equal(action(ui), undefined);
  assert.match(ui.element('period-notice').textContent, /종료/);
});

test('a delayed card search across the deadline supplies only its ID for the next server read', async () => {
  const handler = eventServer('admin', february - 1);
  let finish;
  const ui = await open((route, body, elapsed) => {
    const reply = handler(route, body, elapsed);
    if (route === 'search' && body.phone) return new Promise(resolve => { finish = () => resolve(reply); });
    return reply;
  });
  const pendingSearch = ui.find('01000000001');
  await tick();
  await ui.advance(1000);
  finish();
  await pendingSearch;
  await tick();
  assert.equal(action(ui).textContent, '정정');
  assert.deepEqual(ui.requests.filter(r => r.url.endsWith('/search')).map(r => r.body), [
    {phone:'010-0000-0001'}, {customer_id:customer.customer_id}
  ]);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/cancel')).length, 0);
});

test('a suspended timer cannot submit the old confirmation before the queued boundary callback', async () => {
  const ui = await open(eventServer('staff', february - 0.5));
  await ui.find('01000000001');
  await action(ui).fire('click');
  ui.delayTimers(501);
  assert.equal(ui.element('confirmation').open, true);
  await ui.element('cancel-stamp').fire('click');
  assert.equal(ui.requests.filter(r => r.url.endsWith('/cancel')).length, 0);
  await ui.advance(0);
  assert.equal(ui.element('confirmation').open, false);
  assert.equal(action(ui), undefined);
});

test('staff card and pending controls are cleared on a server privacy closure', async () => {
  let closed = false;
  const ui = await open(route => closed ? {ok:false, status:410, json:async () => ({code:'privacy_closed'})} : normal(route));
  await ui.find('01000000001');
  assert.equal(ui.element('workspace').hidden, false);
  closed = true;
  await ui.find('01000000001');
  assert.equal(ui.element('workspace').hidden, true);
  assert.equal(ui.element('product-rows').children.length, 0);
  assert.equal(ui.location.pathname, '/');
});
