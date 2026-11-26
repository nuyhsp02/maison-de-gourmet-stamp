'use strict';
(() => {
  const error = '국내 휴대전화번호의 자릿수와 구분 기호를 확인해 주세요.';
  const separator = '(?: *- *| +)';
  function digits(raw) {
    if (typeof raw !== 'string' || raw.length > 64 || /[^0-9()+ -]/.test(raw)) return null;
    let value = raw.replace(/^ +| +$/g, '');
    const international = value.startsWith('+82');
    if (international) value = value.replace(/^\+82(?: *- *| *)/, '');
    const prefix = international ? '1[016789]' : '01[016789]';
    const groups = value.match(new RegExp('^(?:(' + prefix + ')|\\((' + prefix + ')\\))' +
      separator + '?([0-9]{3,4})' + separator + '?([0-9]{4})$'));
    if (!groups) return null;
    const n = (international ? '0' : '') + (groups[1] || groups[2]) + groups[3] + groups[4];
    return n.startsWith('010') && n.length !== 11 ? null : n;
  }
  function format(n) {
    const middle = n.startsWith('010') || n.length === 11 ? 4 : 3;
    return [n.slice(0, 3), n.slice(3, 3 + middle), n.slice(3 + middle)].filter(Boolean).join('-');
  }
  function bind(input) {
    let previous = null;
    input.addEventListener('beforeinput', ev => {
      previous = null;
      if (input.type !== 'tel' || ev.isComposing) return;
      const n = input.value.replace(/-/g, '');
      if (!/^[0-9]{0,11}$/.test(n) || input.value !== format(n)) return;
      previous = input.value;
      const pos = input.selectionStart;
      if (pos !== input.selectionEnd) return;
      if (ev.inputType === 'deleteContentBackward' && input.value[pos - 1] === '-')
        input.setSelectionRange(pos - 2, pos);
      if (ev.inputType === 'deleteContentForward' && input.value[pos] === '-')
        input.setSelectionRange(pos, pos + 2);
    });
    input.addEventListener('input', ev => {
      const hadFormatting = previous !== null;
      previous = null;
      if (input.type !== 'tel' || ev.isComposing) return;
      const raw = input.value;
      let n = digits(raw);
      if (n === null) {
        const numberEdit = /^[0-9]*$/.test(raw) || (hadFormatting &&
          (/^delete/.test(ev.inputType || '') || /^[0-9]+$/.test(ev.data || '')) && /^[0-9-]*$/.test(raw));
        if (!numberEdit) return;
        n = raw.replace(/-/g, '');
        if (n.length > 11) return;
      }
      const value = format(n);
      if (raw === value) return;
      let offset = raw.slice(0, input.selectionStart).replace(/[^0-9]/g, '').length;
      if (raw.trimStart().startsWith('+82')) offset = Math.max(0, offset - 1);
      input.value = value;
      let cursor = 0;
      while (cursor < value.length && offset) {
        if (value[cursor++] !== '-') offset--;
      }
      input.setSelectionRange(cursor, cursor);
    });
  }
  window.MaisonPhone = Object.freeze({digits, format, bind, error});
})();
