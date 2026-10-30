'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Core = require('../static/collection-core.js');
const jsQR = require('../static/vendor/jsQR.js');
const fixtures = require('./fixtures/qr-matrices.json');

const box = { left: 0, top: 0, width: 400, height: 400 };
const finder = { left: 72, top: 72, width: 256, height: 256 };
const corners = (x = 130, y = 130, size = 140) => [
  { x, y }, { x: x + size, y }, { x: x + size, y: y + size }, { x, y: y + size }];

test('cover maps landscape source into the visible central square', () => {
  assert.deepEqual(Core.sourceRect(1920, 1080, box, finder), { x: 615, y: 195, width: 690, height: 690 });
});
test('portrait/rotated metadata recalculates source coordinates', () => {
  assert.deepEqual(Core.sourceRect(1080, 1920, box, finder), { x: 195, y: 615, width: 690, height: 690 });
});
test('page offsets, aspect ratio and finder inner border are represented', () => {
  const v = { left: 31, top: 245, width: 320, height: 240 };
  const f = { left: 111, top: 305, width: 160, height: 120 };
  assert.deepEqual(Core.sourceRect(640, 480, v, f), { x: 160, y: 120, width: 320, height: 240 });
  const inner = { left: 113, top: 307, width: 156, height: 116 };
  assert.deepEqual(Core.sourceRect(640, 480, v, inner), { x: 164, y: 124, width: 312, height: 232 });
});
test('contain handles centered letterbox; finder in bars is rejected', () => {
  const crop = Core.sourceRect(1920, 1080, box, { left: 100, top: 100, width: 200, height: 200 }, 'contain');
  assert.equal(crop.x, 480); assert.ok(Math.abs(crop.y - 60) <= 1);
  assert.ok(crop.y + crop.height <= 1080);
  assert.throws(() => Core.sourceRect(1920, 1080, box, finder, 'contain'), /outside_video/);
});
test('invalid or unavailable video geometry never falls back to full frame', () => {
  assert.throws(() => Core.sourceRect(0, 0, box, finder));
  assert.throws(() => Core.sourceRect(1920, 1080, box, finder, 'fill'));
  assert.throws(() => Core.sourceRect(1920, 1080, box, { ...finder, left: -1 }));
});
test('only a fully contained single QR covering the target is selected', () => {
  const code = { raw: 'selected', corners: corners() };
  assert.equal(Core.selectCandidate([code], 400, 400).raw, 'selected');
  assert.equal(Core.selectCandidate([{ ...code, corners: corners(-1, 130) }], 400, 400).raw, null);
  assert.equal(Core.selectCandidate([{ ...code, corners: corners(10, 10, 90) }], 400, 400).status, 'off_center');
  assert.equal(Core.selectCandidate([{ raw: 'no geometry' }], 400, 400).raw, null);
  assert.equal(Core.selectCandidate([], 400, 400).raw, null);
  assert.equal(Core.selectCandidate([code, code], 400, 400).status, 'ambiguous');
});
test('native detector receives cropped canvas and rejects multiple results', async () => {
  const canvas = { width: 400, height: 400 };
  const detector = { detect: async input => {
    assert.equal(input, canvas);
    return [{ rawValue: 'a', cornerPoints: corners() }, { rawValue: 'b', cornerPoints: corners(10, 10, 80) }];
  } };
  assert.equal((await Core.decodeCrop(canvas, {}, detector, jsQR)).raw, null);
  detector.detect = async () => [{ rawValue: 'a', cornerPoints: corners() }];
  assert.equal((await Core.decodeCrop(canvas, {}, detector, jsQR)).raw, 'a');
});

function image(width, height) {
  const data = new Uint8ClampedArray(width * height * 4); data.fill(255);
  return { width, height, data };
}
function place(target, fixture, left, top, scale = 3, rotate = false) {
  const n = fixture.rows.length;
  for (let y = 0; y < n * scale; y++) for (let x = 0; x < n * scale; x++) {
    const row = Math.floor(y / scale), col = Math.floor(x / scale);
    const value = rotate ? fixture.rows[n - 1 - col][row] : fixture.rows[row][col];
    const i = ((top + y) * target.width + left + x) * 4;
    target.data[i] = target.data[i + 1] = target.data[i + 2] = value === '1' ? 0 : 255;
  }
}
function crop(source, rect) {
  const out = image(rect.width, rect.height);
  for (let y = 0; y < rect.height; y++) {
    const start = ((rect.y + y) * source.width + rect.x) * 4;
    out.data.set(source.data.subarray(start, start + rect.width * 4), y * rect.width * 4);
  }
  return out;
}
function decode(pixels) {
  return Core.decodeCrop(pixels, { getImageData: () => pixels }, null, jsQR);
}
test('real jsQR pixels: center code selected, outside neighbors excluded', async () => {
  const pixels = image(1000, 600), size = fixtures[0].rows.length * 3;
  place(pixels, fixtures[0], Math.floor(500 - size / 2), Math.floor(300 - size / 2));
  place(pixels, fixtures[1], 70, 200);
  const roi = Core.sourceRect(1000, 600, { left: 0, top: 0, width: 1000, height: 600 },
    { left: 350, top: 150, width: 300, height: 300 });
  assert.equal((await decode(crop(pixels, roi))).raw, fixtures[0].url);
});
test('real jsQR pixels: no center code means outside QR never selected', async () => {
  const pixels = image(1000, 600); place(pixels, fixtures[1], 70, 200);
  assert.equal((await decode(crop(pixels, { x: 350, y: 150, width: 300, height: 300 }))).raw, null);
});
test('real jsQR pixels: two QR codes within ROI are ambiguous', async () => {
  const pixels = image(600, 400), size = fixtures[0].rows.length * 3;
  place(pixels, fixtures[0], Math.floor(300 - size / 2), Math.floor(200 - size / 2));
  place(pixels, fixtures[1], 15, 15);
  const result = await decode(pixels);
  assert.equal(result.raw, null);
  // jsQR can also refuse mixed finder patterns before returning its first QR.
  assert.ok(['ambiguous', 'none'].includes(result.status));
});
test('jsQR second pass masks the first quadrilateral and rejects a second code', async () => {
  const pixels = image(400, 400); pixels.data.fill(0);
  let calls = 0;
  const decoder = data => {
    const points = corners();
    if (++calls === 2) assert.equal(data[(200 * 400 + 200) * 4], 255);
    return { data: calls === 1 ? 'a' : 'b', location: {
      topLeftCorner: points[0], topRightCorner: points[1], bottomRightCorner: points[2], bottomLeftCorner: points[3]
    } };
  };
  assert.equal((await Core.decodeCrop(pixels, { getImageData: () => pixels }, null, decoder)).status, 'ambiguous');
  assert.equal(calls, 2);
});
test('real jsQR pixels: rotated center QR decodes without extra rotation', async () => {
  const pixels = image(400, 400), size = fixtures[0].rows.length * 3;
  place(pixels, fixtures[0], Math.floor(200 - size / 2), Math.floor(200 - size / 2), 3, true);
  assert.equal((await decode(pixels)).raw, fixtures[0].url);
});
test('three stable frames, in-flight lock, per-session dedupe and interruption reset', () => {
  const gate = new Core.ScanGate();
  assert.equal(gate.claim('a', 0, 'portrait'), false);
  assert.equal(gate.claim('a', 180, 'portrait'), false);
  assert.equal(gate.claim('a', 360, 'portrait'), true);
  assert.equal(gate.claim('a', 540, 'portrait'), false);
  assert.equal(gate.claim('b', 720, 'portrait'), false);
  gate.settle('a', true);
  for (let i = 0; i < 10; i++) assert.equal(gate.claim('a', 1000 + i * 180, 'portrait'), false);
  assert.equal(gate.claim('b', 4000, 'portrait'), false);
  assert.equal(gate.claim(null, 4180, 'portrait'), false);
  assert.equal(gate.claim('b', 4360, 'portrait'), false);
  assert.equal(gate.claim('b', 4540, 'landscape'), false);
  assert.equal(gate.claim('b', 4720, 'landscape'), false);
  assert.equal(gate.claim('b', 4900, 'landscape'), true);
});
const eligible = { serverEligible: true, navigationType: 'navigate', initiallyVisible: true,
  visible: true, focused: true, prerendered: false, attempted: false,
  hasSession: true, active: true, collected: false, redeemed: false };
test('automatic collection requires all explicit safety signals', () => {
  assert.equal(Core.canAutoCollect(eligible), true);
  for (const [key, value] of Object.entries({ serverEligible: false, navigationType: 'reload',
    initiallyVisible: false, visible: false, focused: false, prerendered: true,
    attempted: true, hasSession: false, active: false, collected: true, redeemed: true })) {
    assert.equal(Core.canAutoCollect({ ...eligible, [key]: value }), false, key);
  }
  assert.equal(Core.canAutoCollect({ ...eligible, navigationType: 'back_forward' }), false);
  assert.equal(Core.canAutoCollect({ ...eligible, navigationType: undefined }), false);
});
