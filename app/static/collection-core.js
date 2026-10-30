'use strict';
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CollectionCore = api;
})(typeof window === 'object' ? window : this, function () {
  // Map the displayed INNER viewfinder to the browser-oriented video frame.
  // videoWidth/videoHeight already describe the frame used by drawImage; applying
  // screen.orientation a second time would rotate it incorrectly on mobile.
  function sourceRect(videoWidth, videoHeight, video, finder, fit = 'cover') {
    const numbers = [videoWidth, videoHeight, video.width, video.height,
      video.left, video.top, finder.left, finder.top, finder.width, finder.height];
    if (!numbers.every(Number.isFinite) || videoWidth <= 0 || videoHeight <= 0 ||
        video.width <= 0 || video.height <= 0 || finder.width <= 0 || finder.height <= 0 ||
        !['cover', 'contain'].includes(fit)) throw new Error('invalid_scan_geometry');
    const scale = Math[fit === 'cover' ? 'max' : 'min'](video.width / videoWidth, video.height / videoHeight);
    const left = video.left + (video.width - videoWidth * scale) / 2;
    const top = video.top + (video.height - videoHeight * scale) / 2;
    // Do not silently expand/clamp a finder extending into letterboxing or outside
    // the video: suppress this frame until layout and metadata are consistent.
    const right = finder.left + finder.width, bottom = finder.top + finder.height;
    if (finder.left < Math.max(video.left, left) || finder.top < Math.max(video.top, top) ||
        right > Math.min(video.left + video.width, left + videoWidth * scale) ||
        bottom > Math.min(video.top + video.height, top + videoHeight * scale)) {
      throw new Error('finder_outside_video');
    }
    // Round inward so no source pixel outside the visible finder is included.
    const x = Math.ceil((finder.left - left) / scale);
    const y = Math.ceil((finder.top - top) / scale);
    const width = Math.floor((right - left) / scale) - x;
    const height = Math.floor((bottom - top) / scale) - y;
    if (width < 24 || height < 24) throw new Error('scan_region_too_small');
    return { x, y, width, height };
  }

  function inside(point, polygon) {
    let sign = 0;
    for (let i = 0; i < polygon.length; i++) {
      const a = polygon[i], b = polygon[(i + 1) % polygon.length];
      const cross = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x);
      if (Math.abs(cross) < 1e-6) continue;
      if (sign && sign !== Math.sign(cross)) return false;
      sign = Math.sign(cross);
    }
    return sign !== 0;
  }

  function selectCandidate(codes, width, height) {
    if (!codes.length) return { status: 'none', raw: null };
    // Two physical QR codes, even containing the same URL, are ambiguous.
    if (codes.length !== 1) return { status: 'ambiguous', raw: null };
    const code = codes[0], corners = code.corners;
    if (!code.raw || !corners || corners.length !== 4 || corners.some(p =>
      !Number.isFinite(p.x) || !Number.isFinite(p.y) || p.x < 2 || p.y < 2 ||
      p.x > width - 2 || p.y > height - 2)) return { status: 'edge', raw: null };
    if (!inside({ x: width / 2, y: height / 2 }, corners)) return { status: 'off_center', raw: null };
    return { status: 'single', raw: code.raw };
  }

  function jsCode(result) {
    return { raw: result.data, corners: [result.location.topLeftCorner,
      result.location.topRightCorner, result.location.bottomRightCorner, result.location.bottomLeftCorner] };
  }

  function maskCode(pixels, width, height, corners) {
    const minX = Math.max(0, Math.floor(Math.min(...corners.map(p => p.x))) - 1);
    const minY = Math.max(0, Math.floor(Math.min(...corners.map(p => p.y))) - 1);
    const maxX = Math.min(width - 1, Math.ceil(Math.max(...corners.map(p => p.x))) + 1);
    const maxY = Math.min(height - 1, Math.ceil(Math.max(...corners.map(p => p.y))) + 1);
    for (let y = minY; y <= maxY; y++) for (let x = minX; x <= maxX; x++) {
      if (!inside({ x: x + .5, y: y + .5 }, corners)) continue;
      const i = (y * width + x) * 4;
      pixels[i] = pixels[i + 1] = pixels[i + 2] = pixels[i + 3] = 255;
    }
  }

  async function decodeCrop(canvas, context, detector, decoder) {
    const width = canvas.width, height = canvas.height;
    if (detector) {
      // Never pass the full video to BarcodeDetector: both paths use THIS crop.
      const detected = await detector.detect(canvas);
      return selectCandidate(detected.map(c => ({ raw: c.rawValue, corners: c.cornerPoints })), width, height);
    }
    const image = context.getImageData(0, 0, width, height);
    const first = decoder(image.data, width, height, { inversionAttempts: 'dontInvert' });
    if (!first) return selectCandidate([], width, height);
    const code = jsCode(first);
    // jsQR returns only one code. Remove its actual quadrilateral and search
    // again; do not interpret its first result as proof there is only one QR.
    const remaining = new Uint8ClampedArray(image.data);
    maskCode(remaining, width, height, code.corners);
    const second = decoder(remaining, width, height, { inversionAttempts: 'dontInvert' });
    return selectCandidate(second ? [code, jsCode(second)] : [code], width, height);
  }

  class ScanGate {
    constructor() { this.completed = new Set(); this.pending = null; this.reset(); }
    reset() { this.candidate = null; this.frames = 0; this.lastAt = 0; this.geometry = null; }
    claim(raw, now, geometry) {
      if (!raw) { this.reset(); return false; }
      if (this.pending || this.completed.has(raw)) return false;
      if (raw !== this.candidate || geometry !== this.geometry || now - this.lastAt > 750) {
        this.candidate = raw; this.frames = 0; this.geometry = geometry;
      }
      this.lastAt = now;
      if (++this.frames < 3) return false;
      this.pending = raw;
      return true;
    }
    settle(raw, committed) {
      if (committed) this.completed.add(raw);
      this.pending = null; this.reset();
    }
  }

  function canAutoCollect(context) {
    return context.serverEligible === true && context.navigationType === 'navigate' &&
      context.initiallyVisible === true && context.visible === true && context.focused === true &&
      context.prerendered !== true && context.attempted !== true &&
      context.hasSession === true && context.active === true && context.collected !== true &&
      context.redeemed !== true;
  }

  return { sourceRect, inside, selectCandidate, decodeCrop, ScanGate, canAutoCollect };
});
