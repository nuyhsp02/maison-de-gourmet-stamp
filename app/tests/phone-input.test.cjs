'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const cases = require('./fixtures/phone-formats.json');
global.window = {};
require('../static/phone-input.js');
const phone = window.MaisonPhone;
const sample = s => s.replaceAll('D', '5');

test('customer and staff use the same supported domestic and international grammar', () => {
  for (const [raw, spec] of cases.accepted)
    assert.ok(phone.digits(sample(raw)) === spec.slice(0, 3) + '5'.repeat(Number(spec[3])));
  for (const raw of cases.rejected) assert.equal(phone.digits(sample(raw)), null);
});

function field() {
  const events = {};
  const input = {type: 'tel', value: '', selectionStart: 0, selectionEnd: 0,
    addEventListener(type, fn) { events[type] = fn; },
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }};
  phone.bind(input);
  function edit(type, data = null) {
    const ev = {inputType: type, data};
    events.beforeinput(ev);
    let a = input.selectionStart, b = input.selectionEnd;
    if (a === b && type === 'deleteContentBackward') a = Math.max(0, a - 1);
    if (a === b && type === 'deleteContentForward') b++;
    input.value = input.value.slice(0, a) + (data ?? '') + input.value.slice(b);
    input.setSelectionRange(a + (data?.length || 0), a + (data?.length || 0));
    events.input(ev);
  }
  return {input, edit};
}

test('typing, replacing a selection, deleting beside a divider and re-inserting preserve caret', () => {
  const {input, edit} = field();
  for (const ch of sample('010DDDDDDDD')) edit('insertText', ch);
  assert.ok(input.value === sample('010-DDDD-DDDD'));
  input.setSelectionRange(4, 5);
  edit('insertReplacementText', '6');
  assert.equal(input.selectionStart, 5);
  input.setSelectionRange(8, 8);
  edit('deleteContentForward');
  assert.equal(input.value.replaceAll('-', '').length, 10);
  input.setSelectionRange(4, 4);
  edit('deleteContentBackward');
  assert.equal(input.selectionStart, 2);
  edit('insertText', '0');
  assert.ok(input.value.startsWith('010-6'));
});

test('valid paste normalizes while malformed paste and overflow remain editable', () => {
  const {input, edit} = field();
  edit('insertFromPaste', sample('+82 10 DDDD DDDD'));
  assert.ok(input.value === sample('010-DDDD-DDDD'));
  edit('insertText', '6');
  assert.equal(phone.digits(input.value), null);
  assert.equal(input.value.replaceAll('-', '').length, 12);
  input.setSelectionRange(0, input.value.length);
  const invalid = sample('010--DDDD-DDDD');
  edit('insertFromPaste', invalid);
  assert.ok(input.value === invalid && phone.digits(input.value) === null);
});
