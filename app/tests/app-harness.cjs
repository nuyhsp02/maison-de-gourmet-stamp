'use strict';
// Runs the actual app.js with a small DOM/HTTP test double, not a real browser.
// Pixel decoding is tested separately with real jsQR; no camera permissions used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const clone = value => JSON.parse(JSON.stringify(value));
const pause = () => new Promise(resolve => setImmediate(resolve));
const URL_A = 'http://127.0.0.1:8765/q/' + 'a'.repeat(32);

class Element {
  constructor(id) {
    this.id = id; this.hidden = false; this.disabled = false; this.checked = false;
    this.open = false; this.children = []; this.listeners = {}; this.dataset = {};
    this.attributes = {};
    this.classList = { toggle() {} };
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async fire(name) { for (const fn of this.listeners[name] || []) await fn({}); }
  append(...children) { this.children.push(...children); }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector() { return this.span ||= new Element('span'); }
  showModal() { this.open = true; }
  close() { this.open = false; return this.fire('close'); }
  async play() {}
  focus() {}
  select() {}
  get lastChild() { return { remove: () => this.children.pop() }; }
}

async function openApp(options = {}) {
  const products = options.products || Array.from({ length: 10 }, (_, i) => ({
    id: 'p' + i, name: '상품 ' + i, icon: 'cup', category: i < 4 ? 'season' : 'select'
  }));
  const server = options.server || { customer: options.customer === false ? null :
    { stamps: {}, count: 0, status: 'collecting', redeemed_at: null, recovery_saved: false },
    phase: 'demo', reward_open: true };
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  element('switch-confirm').hidden = true;
  const documentEvents = {}, windowEvents = {}, timers = new Map();
  const requests = [], drawCalls = []; let timerId = 0;
  const document = {
    body: { dataset: { autoCollect: options.eligible === false ? 'manual' : 'eligible' } },
    visibilityState: options.hidden ? 'hidden' : 'visible', hidden: !!options.hidden,
    prerendering: !!options.prerender,
    hasFocus: () => options.focused !== false,
    getElementById: element, createElementNS: (_ns, id) => new Element(id),
    createElement: name => {
      const el = new Element(name);
      if (name === 'canvas') el.getContext = () => ({
        drawImage: (...args) => drawCalls.push(args),
        getImageData: () => ({ data: new Uint8ClampedArray(el.width * el.height * 4) })
      });
      return el;
    },
    addEventListener: (name, fn) => (documentEvents[name] ||= []).push(fn)
  };
  const location = new URL(options.url || URL_A);
  const history = { state: options.history || null,
    replaceState(value, _title, url) { this.state = value; location.href = new URL(url, location.href).href; } };
  const video = element('camera-video');
  Object.assign(video, { readyState: 2, videoWidth: 1920, videoHeight: 1080,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 400 }) });
  element('viewfinder').getBoundingClientRect = () => ({ left: 72, top: 72, width: 256, height: 256 });
  let detectorCodes = null, detectorCalls = 0;
  class BarcodeDetector {
    static async getSupportedFormats() { return ['qr_code']; }
    async detect(canvas) {
      detectorCalls++;
      if (options.detectorHook) return options.detectorHook(canvas);
      return detectorCodes || [];
    }
  }
  const snapshot = pending => clone({ event: { draft: true, title: '테스트', cafe_name: '카페',
    description: '테스트', products, reward: '선물' }, ...server,
    csrf: server.customer ? 'test-csrf' : null, pending });
  const context = {
    console, URL, Uint8ClampedArray, setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); }, document, location, history,
    performance: { getEntriesByType: () => options.navigation === null ? [] : [{ type: options.navigation || 'navigate', activationStart: options.activationStart || 0 }] },
    isSecureContext: true, BarcodeDetector: options.native === false ? undefined : BarcodeDetector,
    jsQR: options.decoder,
    getComputedStyle: el => el === video ? { objectPosition: '50% 50%', objectFit: 'cover', transform: 'none' } :
      { borderLeftWidth: '2px', borderRightWidth: '2px', borderTopWidth: '2px', borderBottomWidth: '2px' },
    navigator: { userAgent: 'Test browser', vibrate() {}, mediaDevices: {
      getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } },
    addEventListener: (name, fn) => (windowEvents[name] ||= []).push(fn),
    fetch: async (url, init = {}) => {
      const parsed = new URL(url, location.origin), body = init.body ? JSON.parse(init.body) : null;
      requests.push({ path: parsed.pathname, method: init.method || 'GET', body, headers: init.headers });
      let value, status = 200;
      if (parsed.pathname === '/api/state') {
        if (options.stateHook) await options.stateHook();
        value = snapshot(parsed.searchParams.has('qr') ? 'p0' : null);
      }
      else if (parsed.pathname === '/api/session') {
        server.customer = { stamps: {}, count: 0, status: 'collecting', redeemed_at: null }; value = { ok: true };
      } else if (parsed.pathname === '/api/collect') {
        if (options.collect401) { server.customer = null; status = 401; value = { error: '세션 만료' }; }
        else {
          const c = server.customer, id = body.qr === 'b'.repeat(32) ? 'p1' : 'p0';
          const added = !c.stamps[id]; c.stamps[id] = 1; c.count = Object.keys(c.stamps).length;
          c.status = c.count === 10 ? 'complete' : 'collecting';
          value = { ...snapshot(null), product_id: id, added };
          if (options.loseCollectResponse) throw new Error('network lost after commit');
        }
      } else if (parsed.pathname === '/api/redeem') {
        const already = server.customer.status === 'redeemed';
        server.customer.status = 'redeemed'; server.customer.redeemed_at = 1;
        value = { ...snapshot(null), already_redeemed: already };
        if (options.loseRedeemResponse) throw new Error('network lost after redemption');
      } else throw new Error('Unexpected test route: ' + parsed.pathname);
      return { ok: status === 200, status, json: async () => clone(value) };
    }
  };
  context.window = context;
  vm.createContext(context);
  for (const file of ['collection-core.js', 'stamp-art.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', file), 'utf8'), context, { filename: file });
  }
  await pause();
  return {
    element, requests, server, location, history, document, drawCalls,
    posts: name => requests.filter(r => r.method === 'POST' && r.path === name),
    async click(id) { if (!element(id).disabled) await element(id).fire('click'); await pause(); },
    async windowEvent(name, event = {}) { for (const fn of windowEvents[name] || []) await fn(event); await pause(); },
    async visibility(hidden) {
      document.hidden = hidden; document.visibilityState = hidden ? 'hidden' : 'visible';
      for (const fn of documentEvents.visibilitychange || []) await fn(); await pause();
    },
    codes(value) { detectorCodes = value; },
    get detectorCalls() { return detectorCalls; },
    async frames(count) {
      for (let i = 0; i < count; i++) {
        const entry = [...timers].find(([, value]) => value.ms === 180);
        if (!entry) break;
        timers.delete(entry[0]); await entry[1].fn(); await pause();
      }
    }
  };
}
module.exports = { openApp, URL_A };
