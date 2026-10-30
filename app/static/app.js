'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const Core = window.CollectionCore;
  const STAMP_ART = window.CollectionStampArt || {};
  // Presentation only: preserve event.json and all product/session/API settings.
  const BRAND = Object.freeze({ name: 'Maison de Gourmet', title: 'Winter Collection',
    description: '열 가지 겨울의 맛을 모아, 나만의 트리를 완성해 보세요.' });
  const icons = new Set(['cup', 'cream', 'tea', 'glass', 'bean', 'bread', 'cake', 'cookie', 'bag']);
  let state, pendingId = null, busy = false, toastTimer;
  let qrKey = location.pathname.startsWith('/q/') ? location.pathname.slice(3) : null;
  const isRestore = location.pathname === '/recover';
  let recoveryToken = isRestore ? location.hash.slice(1) : '';
  // Fragments never reach the server; remove from browser history before other work.
  if (isRestore) history.replaceState(null, '', '/recover');
  let cameraStream = null, cameraEpoch = 0, frameTimer = null;
  let scanBusy = false, scanGate = new Core.ScanGate();
  const navigation = performance.getEntriesByType?.('navigation')?.[0];
  const initiallyVisible = document.visibilityState === 'visible';
  const wasPrerendered = document.prerendering === true || (navigation?.activationStart || 0) > 0;
  let initialAutoConsidered = false, autoCollecting = false, navigationInterrupted = false;

  function icon(id, className) {
    const svg = document.createElementNS(NS, 'svg');
    const use = document.createElementNS(NS, 'use');
    svg.setAttribute('aria-hidden', 'true');
    if (className) svg.setAttribute('class', className);
    use.setAttribute('href', '#' + id);
    svg.append(use);
    return svg;
  }
  function text(tag, value, className) {
    const el = document.createElement(tag);
    el.textContent = value;
    if (className) el.className = className;
    return el;
  }
  function toast(message, error = false) {
    clearTimeout(toastTimer);
    $('toast').textContent = message;
    $('toast').classList.toggle('is-error', error);
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
  }
  const dateTime = seconds => new Date(seconds * 1000).toLocaleString('ko-KR');
  const eventDate = value => new Date(value).toLocaleDateString('ko-KR', {
    year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Asia/Seoul'
  });
  async function api(path, data) {
    const options = { credentials: 'same-origin', cache: 'no-store', redirect: 'error' };
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = { 'Content-Type': 'application/json', 'X-Stamp-Client': '1' };
      if (state?.csrf) options.headers['X-CSRF-Token'] = state.csrf;
      options.body = JSON.stringify(data);
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error('서버에 연결할 수 없어요. 연결을 확인한 뒤 다시 시도해 주세요.'); }
    let result;
    try { result = await response.json(); }
    catch (_) { throw new Error('서버 응답을 확인할 수 없어요. 잠시 후 다시 시도해 주세요.'); }
    if (!response.ok) {
      const error = new Error(result.error || '요청을 처리하지 못했어요.');
      error.code = result.code;
      error.status = response.status;
      throw error;
    }
    return result;
  }
  async function refresh() {
    const result = await api('/api/state' + (qrKey ? '?qr=' + encodeURIComponent(qrKey) : ''));
    state = result;
    pendingId = result.pending;
    render();
  }
  async function ensureSession() {
    if (state.customer) return;
    await api('/api/session', {});
    await refresh();
    if (!state.customer) throw new Error('쿠키가 차단되어 기록을 저장할 수 없어요. 쿠키를 허용하거나 다른 기본 브라우저에서 열어 주세요.');
  }
  function isActive() { return ['demo', 'running'].includes(state.phase); }
  function product(id) { return state.event.products.find(p => p.id === id); }
  function clearPending() {
    pendingId = null;
    qrKey = null;
    history.replaceState(null, '', '/');
    render();
  }
  function renderStampArt(p) {
    const frame = text('span', '', 'stamp-art');
    frame.setAttribute('aria-hidden', 'true');
    const symbol = STAMP_ART[p.id];
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 48 48');
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    const use = document.createElementNS(NS, 'use');
    use.setAttribute('color', 'inherit');
    use.setAttribute('href', typeof symbol === 'string' ?
      '/static/stamp-art.svg#' + symbol : '#' + (icons.has(p.icon) ? p.icon : 'cup'));
    svg.append(use);
    frame.append(svg);
    return frame;
  }
  function renderStamp(p, index, justAdded) {
    const collected = state.customer?.stamps[p.id];
    const item = text('li', '', 'stamp-item' + (collected ? ' collected' : '') + (justAdded === p.id ? ' just-added' : ''));
    item.setAttribute('aria-label', p.name + (collected ? ' · 수집 완료' : ' · 아직 수집 전'));
    item.setAttribute('data-product-id', p.id);
    item.setAttribute('data-category', p.category);
    item.setAttribute('value', String(index + 1));
    item.title = p.name;
    const seal = text('div', '', 'stamp-seal');
    seal.append(renderStampArt(p), text('span', String(index + 1).padStart(2, '0'), 'stamp-number'));
    if (collected) {
      const impression = text('span', '', 'stamp-impression');
      impression.setAttribute('aria-hidden', 'true');
      seal.append(impression);
      const check = text('span', '', 'stamp-check'); check.append(icon('check')); seal.append(check);
      item.title += ' · 적립: ' + dateTime(collected);
    }
    item.append(seal, text('span', p.name, 'stamp-name'), text('span', collected ? '수집 완료' : '수집 전', 'stamp-status'));
    return item;
  }
  function render(justAdded) {
    if (!state) return;
    const e = state.event, c = state.customer;
    const count = c?.count || 0;
    document.title = `${BRAND.title} · ${BRAND.name}`;
    $('cafe-name').textContent = BRAND.name;
    $('event-title').textContent = BRAND.title;
    $('event-description').textContent = BRAND.description;
    $('demo-banner').hidden = !e.draft;
    $('event-period').textContent = e.draft ? '겨울 시즌 한정 · 일정 확정 예정' : `${eventDate(e.starts_at)} — ${eventDate(e.ends_at)} (종료 시각 전까지)`;
    if (!e.draft) $('event-period').title = `${e.starts_at} ~ ${e.ends_at} (종료 시각 미포함)`;
    $('browser-warning').hidden = !/KAKAOTALK|Instagram|FBAN|FBAV|Line\/|NAVER|; wv\)/i.test(navigator.userAgent);
    $('period-banner').hidden = !['upcoming', 'ended'].includes(state.phase);
    $('period-banner').textContent = state.phase === 'upcoming' ? '아직 시작 전이에요. 이벤트 시작 후 스탬프를 모을 수 있어요.' : '스탬프 적립이 종료되었어요. 모은 기록은 계속 볼 수 있어요.';
    $('board-title').textContent = c?.status === 'redeemed' ? '선물까지 받은 겨울' : count === 10 ? '겨울 트리가 완성됐어요!' : '나의 겨울 트리';
    $('stamp-count').textContent = count;
    $('progress').value = count;
    $('progress-caption').textContent = count === 10 ? '열 가지 겨울이 모여, 트리가 완성됐어요.' : `선물까지 남은 겨울의 맛, ${10 - count}가지.`;
    $('stamp-tree').replaceChildren(...e.products.map((p, i) => renderStamp(p, i, justAdded)));
    $('pending-panel').hidden = !pendingId;
    if (pendingId) {
      const already = Boolean(c?.stamps[pendingId]);
      $('pending-title').textContent = product(pendingId)?.name || '상품 확인';
      $('pending-help').textContent = already ? '이미 모은 상품이에요. 한 상품은 한 번만 적립돼요.' :
        autoCollecting ? '기존 기록을 확인했어요. 이 상품을 적립하고 있어요.' :
        c ? '구매한 상품이 맞나요? 아래 버튼으로 적립해 주세요.' :
        '처음 방문하셨나요? 구매한 상품을 확인하고 버튼을 누르면 새 스탬프판을 만들어요.';
      $('collect-button').disabled = busy || already || !isActive() || c?.status === 'redeemed';
      $('collect-button').querySelector('span').textContent = already ? '이미 수집한 스탬프' : !isActive() ? '지금은 적립할 수 없어요' : busy ? '처리 중…' : c ? '이 상품 스탬프 받기' : '새 스탬프판 만들고 적립하기';
    }
    $('reward-panel').hidden = count !== 10;
    if (count === 10) {
      const redeemed = c.status === 'redeemed';
      $('reward-title').textContent = redeemed ? '겨울 선물을 받았어요' : '트리 완성 · 선물 지급 대기';
      $('reward-description').textContent = redeemed ? '함께한 겨울을 기억할게요. 완성한 트리는 그대로 남아요.' : e.reward;
      $('reward-date').textContent = redeemed ? '지급 완료 · ' + dateTime(c.redeemed_at) : e.draft ? '직원에게 이 화면을 보여주세요.' : '직원에게 이 화면을 보여주세요. 지급 마감: ' + new Date(e.reward_until).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' });
      $('redeem-button').hidden = redeemed || !state.reward_open;
      $('redeem-button').disabled = busy || redeemed || !state.reward_open;
      if (redeemed) $('redeem-error').textContent = '';
      $('reward-warning').hidden = redeemed;
      if (!redeemed && !state.reward_open) $('reward-date').textContent = '보상 지급 기간이 아닙니다. 직원에게 문의해 주세요.';
    }
    $('scan-button').hidden = count === 10;
    $('scan-caption').hidden = count === 10;
    $('scan-button').disabled = busy || !isActive();
    $('recovery-button').disabled = busy;
    $('recovery-button').textContent = c?.recovery_saved ? '새 링크 만들기 ↗' : '복구 링크 저장 ↗';
    $('recovery-status').textContent = c?.recovery_saved ? '발급됨 · 나만 볼 수 있는 곳에 보관하세요.' : '선택 사항 · 개인 복구 링크 저장';
    $('generate-link').disabled = busy;
    $('restore-button').disabled = busy || (!$('switch-confirm').hidden && !$('switch-checkbox').checked);
  }
  async function action(fn, errorElement) {
    if (busy) return;
    busy = true; render();
    try { await fn(); }
    catch (error) {
      if (errorElement) $(errorElement).textContent = error.message;
      else toast(error.message, true);
      // A committed write can lose its response. Read current truth; never roll back locally.
      try { await refresh(); } catch (_) { /* Keep the last known board if offline. */ }
    }
    finally { busy = false; render(); }
  }

  async function collectPending(automatic = false) {
    if (automatic) {
      // Automatic flow must NEVER create a customer or recover a stale session.
      if (!state.customer || !state.csrf || navigationInterrupted || document.visibilityState !== 'visible' || !document.hasFocus()) return;
    } else await ensureSession();
    const result = await api('/api/collect', { qr: qrKey });
    state = result;
    const id = result.product_id;
    clearPending(); render(id);
    toast(result.added ? `${product(id).name} 스탬프를 모았어요.` : '이미 수집한 상품이에요.');
  }
  $('collect-button').addEventListener('click', () => action(() => collectPending()));
  $('dismiss-pending').addEventListener('click', clearPending);

  async function maybeAutoCollect() {
    if (!pendingId || !qrKey) return;
    if (state.customer?.stamps[pendingId]) { toast('이미 수집한 상품이에요.'); return; }
    if (!Core.canAutoCollect({
      serverEligible: document.body.dataset.autoCollect === 'eligible',
      navigationType: navigation?.type, initiallyVisible,
      visible: document.visibilityState === 'visible', focused: document.hasFocus(),
      prerendered: wasPrerendered || document.prerendering === true,
      attempted: navigationInterrupted || history.state?.cafeCollectionAutoAttempted === true,
      hasSession: Boolean(state.customer && state.csrf), active: isActive(),
      collected: Boolean(state.customer?.stamps[pendingId]), redeemed: state.customer?.status === 'redeemed'
    })) return;
    // Before the POST, mark this history entry. Lost responses, reloads, back,
    // visibility changes and retry-load do not trigger a second automatic POST.
    try {
      history.replaceState({ ...(history.state || {}), cafeCollectionAutoAttempted: true }, '', location.href);
    } catch (_) { return; }
    autoCollecting = true;
    try { await action(() => collectPending(true)); }
    finally { autoCollecting = false; render(); }
  }

  // Camera frames are decoded locally; images are never uploaded.
  function stopCamera() {
    cameraEpoch++;
    clearTimeout(frameTimer);
    cameraStream?.getTracks().forEach(track => track.stop());
    cameraStream = null;
    $('camera-video').srcObject = null;
    scanBusy = false;
  }
  function scannerError(message) {
    stopCamera();
    $('camera-view').hidden = true;
    $('camera-fallback').hidden = false;
    $('scanner-status').textContent = message;
  }
  function parseQR(raw) {
    const url = new URL(raw);
    if (url.origin !== location.origin || url.search || url.hash || !/^\/q\/[A-Za-z0-9_-]{32}$/.test(url.pathname)) {
      throw new Error('이 매장의 컬렉션 QR만 스캔할 수 있어요.');
    }
    return url.pathname.slice(3);
  }
  async function acceptScan(raw, epoch) {
    if (scanBusy || epoch !== cameraEpoch) return;
    const gate = scanGate;
    scanBusy = true;
    try {
      const key = parseQR(raw);
      const result = await api('/api/collect', { qr: key });
      gate.settle(raw, true);
      if (epoch !== cameraEpoch) { await refresh(); return; }
      state = result; render(result.product_id);
      const name = product(result.product_id).name;
      $('scanner-status').textContent = result.added ? `${name} 적립 완료! 다음 QR을 비춰주세요.` : `${name}은 이미 모았어요. 다른 QR을 비춰주세요.`;
      const row = text('li', name); row.append(text('span', result.added ? '적립 완료 ✓' : '이미 수집함'));
      $('scan-results').prepend(row);
      if ($('scan-results').children.length > 20) $('scan-results').lastChild.remove();
      if (result.added && navigator.vibrate) navigator.vibrate(50);
      if (state.customer.count === 10) {
        stopCamera();
        $('scanner-dialog').close();
        toast('열 가지 취향을 모두 모았어요! 직원에게 화면을 보여주세요.');
      }
    } catch (error) {
      gate.settle(raw, false);
      $('scanner-status').textContent = error.message;
      // Stop on transport/auth errors so the camera cannot flood a failing server.
      if (!error.status || [401, 403, 409, 429, 503].includes(error.status)) scannerError(error.message);
    } finally { if (epoch === cameraEpoch) scanBusy = false; }
  }
  async function startCamera() {
    stopCamera();
    const epoch = cameraEpoch;
    $('scan-results').replaceChildren();
    $('camera-view').hidden = false; $('camera-fallback').hidden = true;
    $('scanner-status').textContent = '카메라 연결 중…';
    $('scanner-dialog').showModal();
    scanGate = new Core.ScanGate();
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      scannerError('이 브라우저에서는 카메라를 사용할 수 없어요. HTTPS 주소와 브라우저를 확인해 주세요.'); return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } } });
      if (epoch !== cameraEpoch || !$('scanner-dialog').open) { stream.getTracks().forEach(t => t.stop()); return; }
      cameraStream = stream;
      const video = $('camera-video'); video.srcObject = stream; await video.play();
      if (epoch !== cameraEpoch) return;
      let detector = null;
      try {
        if (window.BarcodeDetector && (await window.BarcodeDetector.getSupportedFormats()).includes('qr_code')) {
          detector = new window.BarcodeDetector({ formats: ['qr_code'] });
        }
      } catch (_) { /* jsQR is the self-hosted compatibility decoder. */ }
      if (!detector && typeof window.jsQR !== 'function') { scannerError('QR 인식 기능을 불러오지 못했어요. 기본 카메라로 스캔해 주세요.'); return; }
      const canvas = document.createElement('canvas');
      const context = canvas.getContext('2d', { willReadFrequently: true });
      function geometry() {
        const style = getComputedStyle(video), frameStyle = getComputedStyle($('viewfinder'));
        if (style.objectPosition !== '50% 50%' || style.transform !== 'none') throw new Error('unsupported_video_transform');
        const v = video.getBoundingClientRect(), f = $('viewfinder').getBoundingClientRect();
        const borderLeft = parseFloat(frameStyle.borderLeftWidth) || 0;
        const borderRight = parseFloat(frameStyle.borderRightWidth) || 0;
        const borderTop = parseFloat(frameStyle.borderTopWidth) || 0;
        const borderBottom = parseFloat(frameStyle.borderBottomWidth) || 0;
        return Core.sourceRect(video.videoWidth, video.videoHeight, v, {
          left: f.left + borderLeft, top: f.top + borderTop,
          width: f.width - borderLeft - borderRight, height: f.height - borderTop - borderBottom
        }, style.objectFit);
      }
      $('scanner-status').textContent = 'QR 하나를 중앙 십자에 맞추고 잠시 멈춰주세요.';
      async function frame() {
        if (epoch !== cameraEpoch || !$('scanner-dialog').open) return;
        if (video.readyState >= 2 && !scanBusy) {
          try {
            const crop = geometry();
            const geometryKey = JSON.stringify([video.videoWidth, video.videoHeight, crop]);
            const scale = Math.min(1, 720 / Math.max(crop.width, crop.height));
            canvas.width = Math.round(crop.width * scale); canvas.height = Math.round(crop.height * scale);
            context.drawImage(video, crop.x, crop.y, crop.width, crop.height, 0, 0, canvas.width, canvas.height);
            const decoded = await Core.decodeCrop(canvas, context, detector, window.jsQR);
            if (epoch !== cameraEpoch || document.hidden) return;
            const currentGeometry = JSON.stringify([video.videoWidth, video.videoHeight, geometry()]);
            if (geometryKey !== currentGeometry) { scanGate.reset(); }
            else {
              if (decoded.status === 'ambiguous') $('scanner-status').textContent = '여러 QR이 보여요. 네모 안에 하나만 남겨주세요.';
              else if (['edge', 'off_center'].includes(decoded.status)) $('scanner-status').textContent = 'QR 전체를 네모 안에 넣고 중심을 십자에 맞춰주세요.';
              if (scanGate.claim(decoded.raw, Date.now(), geometryKey)) await acceptScan(decoded.raw, epoch);
            }
          } catch (error) {
            scanGate.reset();
            if (['invalid_scan_geometry', 'finder_outside_video', 'scan_region_too_small'].includes(error.message)) {
              $('scanner-status').textContent = '카메라 방향을 맞추고 QR 하나를 중앙에 비춰주세요.';
            } else if (detector) detector = null;
            else { scannerError('카메라 영상을 읽지 못했어요. 기본 카메라로 스캔해 주세요.'); return; }
          }
        }
        if (epoch === cameraEpoch) frameTimer = setTimeout(frame, 180);
      }
      frame();
    } catch (error) {
      if (epoch !== cameraEpoch) return;
      const message = error.name === 'NotAllowedError' ? '카메라 권한이 허용되지 않았어요.' : error.name === 'NotFoundError' ? '사용할 수 있는 카메라가 없어요.' : '카메라를 열지 못했어요. 다른 앱에서 사용 중인지 확인해 주세요.';
      scannerError(message);
    }
  }
  $('scan-button').addEventListener('click', () => action(async () => { await ensureSession(); await startCamera(); }));
  for (const id of ['close-scanner', 'scanner-done']) $(id).addEventListener('click', () => $('scanner-dialog').close());
  $('scanner-dialog').addEventListener('close', stopCamera);
  $('scanner-dialog').addEventListener('cancel', stopCamera);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      navigationInterrupted = true;
      if ($('scanner-dialog').open) $('scanner-dialog').close();
      stopCamera();
    } else if (state && !busy) { refresh().catch(() => {}); }
  });
  window.addEventListener('pagehide', () => { navigationInterrupted = true; stopCamera(); });
  window.addEventListener('pageshow', event => { if (event.persisted && state) refresh().catch(() => {}); });

  $('recovery-button').addEventListener('click', () => action(async () => {
    await ensureSession();
    $('generated-link').hidden = true; $('generate-link').hidden = false;
    $('recovery-error').textContent = ''; $('recovery-url').value = '';
    $('recovery-dialog').showModal();
  }));
  $('generate-link').addEventListener('click', () => action(async () => {
    const result = await api('/api/recovery-link', { confirm_rotation: true });
    $('recovery-url').value = result.url;
    $('recovery-expiry').textContent = '유효 기간: ' + dateTime(result.expires_at);
    $('generated-link').hidden = false; $('generate-link').hidden = true;
    await refresh();
  }, 'recovery-error'));
  $('copy-link').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('recovery-url').value); toast('복구 링크를 복사했어요. 나만 볼 수 있는 곳에 저장해 주세요.'); }
    catch (_) { $('recovery-url').focus(); $('recovery-url').select(); toast('링크를 선택했어요. 길게 눌러 복사해 주세요.'); }
  });
  $('close-recovery').addEventListener('click', () => $('recovery-dialog').close());
  $('recovery-dialog').addEventListener('close', () => { $('recovery-url').value = ''; $('generated-link').hidden = true; });
  $('restore-button').addEventListener('click', () => action(async () => {
    $('restore-error').textContent = '';
    try {
      await api('/api/recover', { token: recoveryToken, confirm_switch: $('switch-checkbox').checked });
      await refresh();
      if (!state.customer) throw new Error('쿠키가 차단되어 복구를 유지할 수 없어요. 쿠키 허용 후 저장한 링크를 다시 열어 주세요.');
      recoveryToken = ''; history.replaceState(null, '', '/'); $('restore-dialog').close();
      toast('기존 컬렉션을 이 브라우저에서 이어갈 수 있어요.');
    } catch (error) {
      if (error.code === 'confirm_switch') $('switch-confirm').hidden = false;
      throw error;
    }
  }, 'restore-error'));
  $('switch-checkbox').addEventListener('change', () => render());
  $('cancel-restore').addEventListener('click', () => $('restore-dialog').close());
  $('restore-dialog').addEventListener('close', () => { recoveryToken = ''; history.replaceState(null, '', '/'); });
  $('redeem-button').addEventListener('click', () => action(async () => {
    $('redeem-error').textContent = '';
    const result = await api('/api/redeem', { confirm_received: true });
    state = result; render();
    toast(result.already_redeemed ? '이미 지급 완료된 컬렉션이에요.' : '보상 지급 완료로 기록했어요.');
  }, 'redeem-error'));

  async function init() {
    const firstLoad = !initialAutoConsidered;
    initialAutoConsidered = true;
    $('loading').hidden = false; $('load-error').hidden = true;
    try {
      await refresh(); $('app').hidden = false;
      if (isRestore && recoveryToken) $('restore-dialog').showModal();
      else if (isRestore) toast('저장한 개인 복구 링크 전체를 다시 열어 주세요.', true);
      else if (firstLoad) await maybeAutoCollect();
    } catch (error) { $('load-error-text').textContent = error.message; $('load-error').hidden = false; }
    finally { $('loading').hidden = true; }
  }
  $('retry-load').addEventListener('click', init);
  init();
})();
