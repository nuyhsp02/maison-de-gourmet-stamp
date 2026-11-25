'use strict';
(() => {
  const el = id => document.getElementById(id);
  const number = value => value.toLocaleString('ko-KR');
  const formatter = new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  });
  const date = seconds => formatter.format(new Date(seconds * 1000));
  const notice = message => { el('message').textContent = message; };
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('maison-admin') : null;
  let csrf = '', generation = 0, expiry, pending = false, card = null, selected = null;
  const stale = Symbol('discarded response');

  function clearSelection() {
    if (el('confirmation').open) el('confirmation').close();
    el('target').textContent = '';
    selected = null;
  }
  function clearCard() {
    clearSelection();
    el('detail').hidden = true;
    for (const id of ['card-name', 'card-phone', 'card-id', 'card-state']) el(id).textContent = '';
    el('product-rows').replaceChildren();
    card = null;
  }
  function clearStats() {
    for (const id of ['cards', 'stamps', 'redeemed']) el(id).textContent = '—';
    el('distribution').replaceChildren();
  }
  function controls(busy) {
    pending = busy;
    for (const id of ['login', 'refresh', 'search', 'search-type', 'query', 'dismiss', 'cancel-stamp']) {
      el(id).disabled = busy;
    }
  }
  function lock(message = '') {
    generation++;
    clearTimeout(expiry);
    csrf = '';
    clearCard();
    clearStats();
    el('password').value = el('query').value = '';
    el('workspace').hidden = el('logout').hidden = true;
    el('login-panel').hidden = false;
    el('logout').disabled = false;
    controls(false);
    notice(message);
  }

  async function request(route, data) {
    const current = generation;
    const options = {credentials: 'same-origin', redirect: 'error', cache: 'no-store'};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json', 'X-Stamp-Client': '1'};
      if (csrf) options.headers['X-Admin-CSRF'] = csrf;
      options.body = JSON.stringify(data);
    }
    let response, body;
    try {
      response = await fetch('/api/admin/' + route, options);
      body = await response.json();
    } catch {
      if (generation !== current) throw stale;
      throw new Error('서버 응답을 확인할 수 없습니다. 연결 상태를 확인해 주세요.');
    }
    if (generation !== current) throw stale;
    if (!response.ok) {
      if (csrf && (response.status === 401 || body.code === 'admin_csrf_failed')) {
        lock('직원 세션을 다시 확인해 주세요.');
        throw stale;
      }
      const error = new Error(body.error || '요청을 처리하지 못했습니다.');
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function element(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }
  async function stats() {
    clearStats();
    const data = await request('stats');
    el('cards').textContent = number(data.cards) + '장';
    el('stamps').textContent = number(data.stamps) + '개';
    el('redeemed').textContent = number(data.redeemed) + '장';
    const max = Math.max(1, ...data.distribution);
    data.distribution.forEach((count, i) => {
      const item = element('li', '');
      item.setAttribute('aria-label', '스탬프 ' + i + '개 보유 카드 ' + number(count) + '장');
      const track = element('span', ''), fill = element('span', '');
      track.className = 'bar-track';
      fill.className = 'bar-value';
      fill.style.height = (100 * count / max) + '%';
      track.setAttribute('aria-hidden', 'true');
      track.append(fill);
      item.append(element('strong', number(count)), track, element('span', String(i)));
      el('distribution').append(item);
    });
  }
  function openConfirmation(stamp) {
    if (pending || !card || card.redeemed_at !== null) return;
    selected = {customer_id: card.customer_id, product_id: stamp.product_id, revision: stamp.revision};
    el('target').textContent = (card.name || '미등록 이름') + ' · ' + card.customer_id + '\n' +
      stamp.name + ' · ' + date(stamp.collected_at) + ' 적립';
    el('confirmation').showModal();
    el('keep').focus();
  }
  async function lookup(body) {
    clearCard();
    const result = await request('search', body);
    card = result;
    el('card-phone').textContent = result.phone_masked || '연락처 미보관';
    el('card-name').textContent = result.name || '기존 익명 카드';
    el('card-id').textContent = '카드 ID · ' + result.customer_id;
    const count = result.stamps.filter(s => s.collected_at !== null).length;
    const redeemed = result.redeemed_at !== null;
    el('card-state').textContent = '적립 ' + count + '/10 · ' + (redeemed ?
      '보상 수령 완료 (' + date(result.redeemed_at) + ') · 취소 불가' : '보상 미수령');
    for (const stamp of result.stamps) {
      const row = element('tr', ''), action = element('td', '—');
      row.append(element('td', stamp.name),
        element('td', stamp.collected_at === null ? '미적립' : date(stamp.collected_at)), action);
      if (stamp.collected_at !== null && !redeemed) {
        const button = element('button', '취소');
        button.type = 'button';
        button.className = 'row-cancel';
        button.setAttribute('aria-label', stamp.name + ' 스탬프 취소');
        button.addEventListener('click', () => openConfirmation(stamp));
        action.replaceChildren(button);
      }
      el('product-rows').append(row);
    }
    el('detail').hidden = false;
  }
  async function showSession(session) {
    csrf = session.csrf;
    clearTimeout(expiry);
    expiry = setTimeout(() => lock('직원 세션이 만료되었습니다.'), Math.max(0, session.expires_at * 1000 - Date.now()));
    el('login-panel').hidden = true;
    el('workspace').hidden = el('logout').hidden = false;
    await stats();
  }
  async function run(work) {
    if (pending) return;
    const current = generation;
    controls(true);
    notice('');
    try { await work(); }
    catch (error) { if (error !== stale && current === generation) notice(error.message); }
    finally { if (current === generation) controls(false); }
  }
  async function restore() {
    await run(async () => {
      try { await showSession(await request('session')); }
      catch (error) {
        if (error.status === 401) lock();
        else throw error;
      }
    });
  }
  el('login-form').addEventListener('submit', event => {
    event.preventDefault();
    return run(async () => {
      const password = el('password').value;
      el('password').value = '';
      await showSession(await request('login', {password}));
    });
  });
  el('logout').addEventListener('click', async () => {
    generation++;
    const current = generation;
    clearCard();
    clearStats();
    el('workspace').hidden = true;
    el('query').value = '';
    el('logout').disabled = true;
    try {
      await request('logout', {});
      lock('로그아웃했습니다.');
      channel?.postMessage('logout');
    } catch (error) {
      if (error !== stale && current === generation) {
        notice('로그아웃을 확인하지 못했습니다. 다시 눌러 주세요.');
        el('logout').disabled = false;
      }
    }
  });
  el('refresh').addEventListener('click', () => run(stats));
  window.MaisonPhone.bind(el('query'));
  el('search-type').addEventListener('change', () => {
    const phone = el('search-type').value === 'phone';
    el('query-label').textContent = phone ? '휴대전화번호 전체' : '카드 ID 전체';
    el('query').type = phone ? 'tel' : 'text';
    el('query').inputMode = phone ? 'numeric' : 'text';
    el('query-help').hidden = !phone;
    el('query').value = '';
    clearCard();
    notice('');
    el('query').focus();
  });
  el('search-form').addEventListener('submit', event => {
    event.preventDefault();
    return run(async () => {
      const key = el('search-type').value;
      let value = el('query').value;
      if (key === 'phone') {
        const number = window.MaisonPhone.digits(value);
        if (!number) { clearCard(); notice(window.MaisonPhone.error); el('query').focus(); return; }
        value = window.MaisonPhone.format(number);
      } else value = value.trim();
      el('query').value = '';
      await lookup({[key]: value});
    });
  });
  el('dismiss').addEventListener('click', () => { clearCard(); notice(''); el('query').focus(); });
  el('keep').addEventListener('click', clearSelection);
  el('confirmation').addEventListener('close', () => { selected = null; el('target').textContent = ''; });
  el('cancel-stamp').addEventListener('click', () => run(async () => {
    const target = selected;
    if (!target) return;
    clearCard();
    clearStats();
    const current = generation;
    let outcome = '스탬프 1개를 취소했습니다.';
    try {
      await request('cancel', target);
    } catch (error) {
      if (error === stale) throw error;
      if (error.status !== 409) {
        notice('취소 결과를 확인하지 못했습니다. 새로고침 후 카드를 다시 조회해 주세요.');
        return;
      }
      outcome = '카드 상태가 바뀌어 취소하지 않았습니다.';
    }
    const reads = await Promise.allSettled([stats(), lookup({customer_id: target.customer_id})]);
    if (generation !== current) return;
    if (reads.some(read => read.status === 'rejected')) {
      outcome += ' 최신 현황을 모두 불러오지 못했습니다. 현황 새로고침과 카드 재조회를 이용해 주세요.';
    }
    notice(outcome);
  }));

  channel?.addEventListener('message', event => { if (event.data === 'logout') lock('로그아웃했습니다.'); });
  window.addEventListener('pagehide', () => lock());
  window.addEventListener('pageshow', event => { if (event.persisted) restore(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) lock(); else restore(); });
  lock();
  restore();
})();
