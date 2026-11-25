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
  let csrf = '', role = '', generation = 0, expiry, pending = false, card = null, selected = null;
  let correctionStamp = null, reviewed = null;
  const stale = Symbol('discarded response');
  const pollMs = 30_000;
  let period = '', cardId = '', policyVersion = 0, pollTimer, boundaryTimer, checkQueued = false;
  let resumeCard = null, permissionsUntil = 0;

  function resetCorrection() {
    correctionStamp = reviewed = null;
    for (const id of ['changes', 'correction-source', 'correction-error']) el(id).textContent = '';
    el('reason').value = '';
    el('destination').replaceChildren();
  }
  function clearSelection() {
    if (el('correction').open) el('correction').close();
    resetCorrection();
    if (el('confirmation').open) el('confirmation').close();
    el('target').textContent = '';
    selected = null;
  }
  function clearCard(keepId = false) {
    clearSelection();
    if (!keepId) cardId = '';
    el('detail').hidden = true;
    for (const id of ['card-name', 'card-phone', 'card-id', 'card-state', 'period-notice']) el(id).textContent = '';
    el('product-rows').replaceChildren();
    card = null;
  }
  function clearStats() {
    for (const id of ['cards', 'stamps', 'redeemed']) el(id).textContent = '—';
    el('distribution').replaceChildren();
  }
  function controls(busy, preserveInput = false) {
    pending = busy;
    for (const id of ['login', 'login-kind', 'review', 'apply-correction', 'refresh', 'search', 'search-type', 'dismiss', 'cancel-stamp']) {
      el(id).disabled = busy;
    }
    el('query').disabled = busy && !preserveInput;
  }
  function invalidatePolicy() {
    policyVersion++;
    period = '';
    clearCard(true);
  }
  function stopTimers() {
    clearTimeout(expiry);
    clearTimeout(pollTimer);
    clearTimeout(boundaryTimer);
    checkQueued = false;
  }
  function lock(message = '') {
    generation++;
    stopTimers();
    resumeCard = null;
    csrf = role = period = '';
    el('login-kind').value = 'staff';
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
    // Allow writes to wait for the server's SQLite lock. An ambiguous response
    // is followed by a read, never an automatic retry of the mutation.
    const controller = new AbortController();
    const reading = ['session', 'search', 'stats'].includes(route);
    const timeout = setTimeout(() => controller.abort(), reading ? 10_000 : 45_000);
    options.signal = controller.signal;
    let response, body;
    try {
      response = await fetch('/api/admin/' + route, options);
      body = await response.json();
    } catch {
      if (generation !== current) throw stale;
      throw new Error('서버 응답을 확인할 수 없습니다. 연결 상태를 확인해 주세요.');
    } finally {
      clearTimeout(timeout);
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
  function canCancel(result) {
    return performance.now() < permissionsUntil && period === 'collection' && result?.permissions?.period === period && result.permissions.can_cancel === true &&
      result.redeemed_at === null;
  }
  function canCorrect(result) {
    return performance.now() < permissionsUntil && role === 'admin' && period === 'correction' &&
      result?.permissions?.period === period &&
      result.permissions.can_correct === true && result.redeemed_at === null &&
      Number.isSafeInteger(result.correction_revision);
  }
  function openConfirmation(stamp) {
    if (pending || !canCancel(card) || !card.stamps.includes(stamp)) return;
    selected = {customer_id: card.customer_id, product_id: stamp.product_id, revision: stamp.revision};
    el('target').textContent = (card.name || '미등록 이름') + ' · ' + card.customer_id + '\n' +
      stamp.name + ' · ' + date(stamp.collected_at) + ' 적립';
    el('confirmation').showModal();
    el('keep').focus();
  }
  function openCorrection(stamp) {
    if (pending || !canCorrect(card) || !card.stamps.includes(stamp)) return;
    clearSelection();
    correctionStamp = stamp;
    el('correction-source').textContent = '정정할 스탬프 · ' + stamp.name + ' · ' + date(stamp.collected_at);
    el('correction-type').value = 'replace';
    for (const item of card.stamps.filter(item => item.collected_at === null)) {
      const option = element('option', item.name);
      option.value = item.product_id;
      el('destination').append(option);
    }
    el('destination-field').hidden = el('correction-form').hidden = false;
    el('correction-review').hidden = true;
    el('correction').showModal();
    el('correction-type').focus();
  }
  function reviewCorrection(event) {
    event.preventDefault();
    if (pending || !canCorrect(card) || !correctionStamp || el('correction-form').hidden) return;
    const type = el('correction-type').value;
    const replacement = card.stamps.find(item => item.product_id === el('destination').value && item.collected_at === null);
    const detail = el('reason').value.trim();
    const length = Array.from(detail).length;
    if (length < 10 || length > 300) {
      el('correction-error').textContent = '정정 사유는 10~300자로 입력해 주세요.';
      el('reason').focus();
      return;
    }
    const normalized = detail.normalize('NFKC').toLowerCase();
    const name = card.name?.normalize('NFKC').toLowerCase();
    if ((name && normalized.includes(name)) || /(?:\+?82|0)[\s().-]*[1-9](?:[\s().-]*\d){7,9}/.test(normalized) ||
        /\d{6}[- ]?[1-8]\d{6}|[^\s@]+@[^\s@]+\.[^\s@]+/.test(normalized)) {
      el('correction-error').textContent = '정정 사유에서 이름, 전화번호 등 개인정보를 제외해 주세요.';
      el('reason').focus();
      return;
    }
    if (!['replace', 'remove'].includes(type) || (type === 'replace' && !replacement)) {
      el('correction-error').textContent = '변경할 미적립 상품을 선택해 주세요.';
      return;
    }
    reviewed = {
      request_id: crypto.randomUUID().replace(/-/g, ''), customer_id: card.customer_id,
      product_id: correctionStamp.product_id, revision: card.correction_revision,
      type, new_product_id: type === 'replace' ? replacement.product_id : null,
      reason_code: type === 'replace' ? 'wrong_product' : 'erroneous_stamp', reason_detail: detail
    };
    const count = card.stamps.filter(item => item.collected_at !== null).length;
    el('changes').textContent = [
      '정정 전 · ' + correctionStamp.name + ' 적립 · ' + count + '/10',
      '정정 후 · ' + (type === 'replace' ? replacement.name + ' 적립 · ' + count : '스탬프 제거 · ' + (count - 1)) + '/10',
      '보상 자격 · 정정 전후 모두 미충족',
      '정정 사유 · ' + detail
    ].join('\n');
    el('correction-form').hidden = true;
    el('correction-review').hidden = false;
    el('revise').focus();
  }
  async function lookup(body, revalidating = false) {
    const version = policyVersion;
    clearCard();
    cardId = body.customer_id || '';
    const result = await request('search', body);
    cardId = result.customer_id;
    // A delayed read can straddle the deadline. Keep only the ID for a fresh lookup.
    if (version !== policyVersion || !period) return;
    if (result.permissions && result.permissions.period !== period) {
      invalidatePolicy();
      if (revalidating) throw new Error('카드의 행사 상태를 다시 확인해 주세요.');
      checkQueued = true;
      return;
    }
    card = result;
    el('card-phone').textContent = result.phone_masked || '연락처 미보관';
    el('card-name').textContent = result.name || '기존 익명 카드';
    el('card-id').textContent = '카드 ID · ' + result.customer_id;
    const count = result.stamps.filter(s => s.collected_at !== null).length;
    const redeemed = result.redeemed_at !== null;
    el('card-state').textContent = '적립 ' + count + '/10 · ' + (redeemed ?
      '보상 수령 완료 (' + date(result.redeemed_at) + ') · 취소 불가' : '보상 미수령');
    if (result.permissions?.period === 'closed') {
      el('period-notice').textContent = '보상 수령 기간이 종료되어 스탬프를 변경할 수 없습니다.';
    } else if (result.permissions?.period === 'correction') {
      if (role !== 'admin') {
        el('period-notice').textContent = '스탬프 적립 기간이 종료되어 일반 직원의 취소가 제한됩니다. 정정이 필요한 경우 관리자에게 요청해 주세요.';
      } else if (count === 10 && !redeemed) {
        el('period-notice').textContent = '완성 카드의 보상 자격은 유지됩니다. 오류가 있으면 운영자에게 검토를 요청해 주세요.';
      } else if (!canCorrect(result)) {
        el('period-notice').textContent = '이 카드는 현재 정정할 수 없습니다.';
      }
    }
    for (const stamp of result.stamps) {
      const row = element('tr', ''), action = element('td', '—');
      row.append(element('td', stamp.name),
        element('td', stamp.collected_at === null ? '미적립' : date(stamp.collected_at)), action);
      if (stamp.collected_at !== null && (canCancel(result) || canCorrect(result))) {
        const cancel = canCancel(result);
        const label = cancel ? '취소' : '정정';
        const button = element('button', label);
        button.type = 'button';
        button.className = 'row-cancel';
        button.setAttribute('aria-label', stamp.name + ' 스탬프 ' + label);
        button.addEventListener('click', () => cancel ? openConfirmation(stamp) : openCorrection(stamp));
        action.replaceChildren(button);
      }
      el('product-rows').append(row);
    }
    el('detail').hidden = false;
  }
  function applySession(session, started) {
    const now = session.server_now, next = session.next_transition_at;
    if (!['staff', 'admin'].includes(session.role) || !session.csrf ||
        !['collection', 'correction', 'closed'].includes(session.period) || !Number.isFinite(now) ||
        !Number.isFinite(session.expires_at) || session.expires_at <= now ||
        !(next === null || (Number.isFinite(next) && next > now))) {
      throw new Error('서버의 행사 상태를 확인할 수 없습니다.');
    }
    // This monotonic deadline can only withhold a stale action, even if the
    // browser delivers its timer late. Fresh permission always comes from the API.
    const deadline = timestamp => started + (timestamp - now) * 1000;
    permissionsUntil = deadline(Math.min(next ?? Infinity, session.expires_at));
    const until = timestamp => Math.max(0, deadline(timestamp) - performance.now());
    csrf = session.csrf;
    role = session.role;
    period = session.period;
    clearTimeout(expiry);
    clearTimeout(boundaryTimer);
    clearTimeout(pollTimer);
    expiry = setTimeout(() => lock('직원 세션이 만료되었습니다.'), until(session.expires_at));
    pollTimer = setTimeout(recheck, pollMs);
    // Avoid overflowing the browser's 32-bit timer for events many weeks away.
    if (next !== null && until(next) < 2_147_483_647) {
      boundaryTimer = setTimeout(() => {
        invalidatePolicy();
        notice('행사 상태를 다시 확인하고 있습니다.');
        return recheck();
      }, Math.max(50, until(next)));
    }
  }
  async function showSession(session, started) {
    applySession(session, started);
    el('management-title').textContent = role === 'admin' ? '관리자 스탬프 관리' : '직원 스탬프 관리';
    el('login-panel').hidden = true;
    el('workspace').hidden = el('logout').hidden = false;
    await stats();
  }
  async function run(work, quiet = false) {
    if (pending) return;
    const current = generation;
    controls(true, quiet);
    if (!quiet) notice('');
    try { await work(); }
    catch (error) { if (error !== stale && current === generation) notice(error.message); }
    finally {
      if (current === generation) {
        controls(false);
        if (checkQueued) {
          checkQueued = false;
          void recheck();
        }
      }
    }
  }
  async function verifyPolicy(reload = false) {
    const version = policyVersion, previous = period, started = performance.now();
    try {
      const session = await request('session');
      if (version !== policyVersion) return; // The boundary queued another check.
      if (csrf !== session.csrf || role !== session.role) {
        lock('직원 세션이 변경됐습니다. 다시 로그인해 주세요.');
        return;
      }
      if (previous !== session.period) invalidatePolicy();
      applySession(session, started);
      if (cardId && (reload || previous !== period || !card)) await lookup({customer_id:cardId}, true);
      if (!previous && period) notice('현재 행사 상태를 확인했습니다.');
    } catch (error) {
      if (error === stale) throw error;
      invalidatePolicy();
      clearTimeout(boundaryTimer);
      clearTimeout(pollTimer);
      pollTimer = setTimeout(recheck, 5000);
      throw new Error('행사 상태를 확인할 수 없어 변경 기능을 잠갔습니다. 연결 후 현황을 새로고침해 주세요.');
    }
  }
  function recheck() {
    if (!csrf || document.hidden || el('workspace').hidden) return;
    if (pending) { checkQueued = true; return; }
    return run(() => verifyPolicy(), true);
  }
  async function restore() {
    if (document.hidden) return;
    await run(async () => {
      const started = performance.now();
      try {
        const session = await request('session');
        const id = resumeCard && resumeCard.csrf === session.csrf ? resumeCard.id : '';
        resumeCard = null;
        await showSession(session, started);
        if (id) await lookup({customer_id:id});
      } catch (error) {
        if (error === stale) throw error;
        lock(error.status === 401 ? '' : '직원 세션을 확인할 수 없습니다. 연결 후 다시 로그인해 주세요.');
      }
    });
  }
  el('login-form').addEventListener('submit', event => {
    event.preventDefault();
    return run(async () => {
      const password = el('password').value;
      el('password').value = '';
      const route = el('login-kind').value === 'manager' ? 'manager-login' : 'login';
      const started = performance.now();
      await showSession(await request(route, {password}), started);
    });
  });
  el('logout').addEventListener('click', async () => {
    generation++;
    const current = generation;
    stopTimers();
    period = '';
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
  el('refresh').addEventListener('click', () => run(async () => {
    await verifyPolicy(true);
    if (csrf) await stats();
  }));
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
  el('confirmation').addEventListener('close', () => {
    if (!el('confirmation').open) { selected = null; el('target').textContent = ''; }
  });
  el('correction').addEventListener('close', () => { if (!el('correction').open) resetCorrection(); });
  el('discard').addEventListener('click', clearSelection);
  el('correction-type').addEventListener('change', () => {
    el('destination-field').hidden = el('correction-type').value !== 'replace';
  });
  el('correction-form').addEventListener('submit', reviewCorrection);
  el('revise').addEventListener('click', () => {
    reviewed = null;
    el('changes').textContent = '';
    el('correction-review').hidden = true;
    el('correction-form').hidden = false;
    el('reason').focus();
  });
  el('apply-correction').addEventListener('click', () => run(async () => {
    const target = reviewed;
    if (!canCorrect(card) || !el('correction').open || !target) return;
    clearSelection();
    const current = generation;
    let outcome = '스탬프 정정을 완료했습니다.';
    try {
      await request('correct', target);
    } catch (error) {
      if (error === stale) throw error;
      outcome = error.status >= 400 && error.status < 500 ? error.message :
        '정정 결과를 확인하지 못했습니다. 다시 실행하기 전에 카드의 적립 상태를 확인해 주세요.';
    }
    await refreshAfterChange(target.customer_id, current, outcome);
  }));
  el('cancel-stamp').addEventListener('click', () => run(async () => {
    const target = selected;
    if (!target || !canCancel(card) || !el('confirmation').open) return;
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
    await refreshAfterChange(target.customer_id, current, outcome);
  }));
  async function refreshAfterChange(customerId, current, outcome) {
    const reads = await Promise.allSettled([stats(), lookup({customer_id: customerId})]);
    if (generation !== current) return;
    if (reads.some(read => read.status === 'rejected')) {
      outcome += ' 최신 현황을 모두 불러오지 못했습니다. 현황 새로고침과 카드 재조회를 이용해 주세요.';
    }
    notice(outcome);
  }

  channel?.addEventListener('message', event => { if (event.data === 'logout') lock('로그아웃했습니다.'); });
  window.addEventListener('pagehide', () => lock());
  window.addEventListener('pageshow', event => { if (event.persisted) restore(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      const previous = csrf && cardId ? {csrf, id:cardId} : null;
      lock();
      resumeCard = previous;
    } else if (csrf) recheck();
    else restore();
  });
  window.addEventListener('focus', () => { if (csrf) recheck(); });
  lock();
  restore();
})();
