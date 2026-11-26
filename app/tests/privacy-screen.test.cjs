'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/privacy-screen.js'), 'utf8');
function screen(budget) {
  const events = {}, docEvents = {};
  let monotonic = 10, wall = 1000, timer, redirect = '', reload = false;
  const dialog = {open:true, close() { this.open = false; }};
  const body = {hidden:true, contents:'synthetic card', replaceChildren() { this.contents = ''; }};
  const html = {hidden:false};
  vm.runInNewContext(code, {
    document:{body, documentElement:html, currentScript:{dataset:{remainingMs:budget}}, querySelectorAll:()=>[dialog],
      addEventListener:(n,f)=>{ docEvents[n]=f; }},
    window:{addEventListener:(n,f)=>{ events[n]=f; }}, location:{replace:u=>{ redirect=u; }, reload:()=>{ reload=true; }},
    performance:{now:()=>monotonic}, Date:{now:()=>wall},
    setTimeout:(f,ms)=>{ timer={f,ms};return 1; }, clearTimeout:()=>{ timer=null; }
  });
  return {body, html, dialog, events, docEvents, get timer(){return timer;},
    get redirect(){return redirect;},get reload(){return reload;}, advance(m,w){monotonic+=m;wall+=w;}};
}
test('open PROD screen clears personal data at the server deadline while offline', () => {
  const s=screen('1010');
  assert.equal(s.body.hidden,false);assert.equal(s.timer.ms,1000);
  s.advance(999,-4000);s.events.focus();assert.equal(s.body.hidden,false);
  s.advance(1,0);s.timer.f();assert.equal(s.body.hidden,true);assert.equal(s.body.contents,'');
  assert.equal(s.dialog.open,false);assert.equal(s.redirect,'/');
});
test('waking from sleep and BFCache cannot reveal the previous card', () => {
  const s=screen('1010');s.advance(0,1001);s.docEvents.visibilitychange();assert.equal(s.body.contents,'');
  const t=screen('5000000000');assert.ok(t.timer.ms<2**31);
  t.events.pagehide();assert.equal(t.html.hidden,true);t.events.pageshow({persisted:true});assert.equal(t.reload,true);
});
test('missing or elapsed server screen deadline fails closed', () => {
  for(const budget of [undefined,'','invalid','0']) assert.equal(screen(budget).body.contents,'');
});
