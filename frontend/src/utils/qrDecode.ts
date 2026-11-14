/**
 * QR and 1D barcode decoding for the spool & Bambu filament scanners (#1574):
 * uses the platform BarcodeDetector where available, falling back to jsQR (and
 * a lightweight 1D EAN-13/UPC-A scanline decoder when includeBarcodes is enabled).
 *
 * Nothing here throws or rejects for an unreadable frame or photo -- that is a
 * frame or photo without a code -- so a failure can't stop the live scan loop.
 */

import jsQR from 'jsqr';

// BarcodeDetector (platform QR/barcode decoder, e.g. Android Chrome, macOS/iOS WebKit)
// isn't in the TS DOM lib yet — minimal shape for the bit we use.
interface DetectedBarcode {
  rawValue: string;
}
export interface QrDetector {
  detect(source: CanvasImageSource): Promise<DetectedBarcode[]>;
}
type BarcodeDetectorCtor = new (opts?: { formats?: string[] }) => QrDetector;

const BARCODE_FORMATS = [
  'qr_code',
  'code_128',
  'code_39',
  'ean_13',
  'ean_8',
  'upc_a',
  'upc_e',
  'data_matrix',
];

/**
 * The platform QR (and optional 1D barcode) decoder, or null when there is none
 * or it can't read the requested formats.
 */
export function createQrDetector(includeBarcodes = false): QrDetector | null {
  const BD = (globalThis as unknown as { BarcodeDetector?: BarcodeDetectorCtor }).BarcodeDetector;
  if (!BD) return null;
  if (includeBarcodes) {
    try {
      return new BD({ formats: BARCODE_FORMATS });
    } catch {
      /* Platform may reject a subset of 1D formats; fall back to qr_code below */
    }
  }
  try {
    return new BD({ formats: ['qr_code'] });
  } catch {
    return null;
  }
}

// ── Lightweight pure-TS EAN-13 / UPC-A 1D scanline fallback ─────────────────
const EAN_L: Record<string, number> = {
  '3211': 0, '2221': 1, '2122': 2, '1411': 3, '1132': 4,
  '1231': 5, '1114': 6, '1312': 7, '1213': 8, '3112': 9,
};
const EAN_G: Record<string, number> = {
  '1123': 0, '1222': 1, '2212': 2, '1141': 3, '2311': 4,
  '1321': 5, '4111': 6, '2131': 7, '3121': 8, '2113': 9,
};
const EAN_R: Record<string, number> = {
  '3211': 0, '2221': 1, '2122': 2, '1411': 3, '1132': 4,
  '1231': 5, '1114': 6, '1312': 7, '1213': 8, '3112': 9,
};
const EAN_PARITY: Record<string, number> = {
  'LLLLLL': 0, 'LLGLGG': 1, 'LLGGLG': 2, 'LLGGGL': 3, 'LGLLGG': 4,
  'LGGLLG': 5, 'LGGGLL': 6, 'LGLGLG': 7, 'LGLGGL': 8, 'LGGLGL': 9,
};

function normalizeFourBars(runs: number[], offset: number): string {
  const total = runs[offset] + runs[offset + 1] + runs[offset + 2] + runs[offset + 3];
  if (total <= 0) return '';
  const raw = [0, 1, 2, 3].map((i) => Math.max(1, Math.min(4, Math.round((runs[offset + i] * 7) / total))));
  let sum = raw[0] + raw[1] + raw[2] + raw[3];
  while (sum !== 7) {
    if (sum > 7) {
      let best = 0;
      for (let i = 1; i < 4; i++) if (raw[i] > raw[best]) best = i;
      if (raw[best] <= 1) break;
      raw[best]--;
      sum--;
    } else {
      let best = 0;
      for (let i = 1; i < 4; i++) if (raw[i] < raw[best]) best = i;
      if (raw[best] >= 4) break;
      raw[best]++;
      sum++;
    }
  }
  return raw.join('');
}

function tryDecodeEan13Runs(runs: number[]): string | undefined {
  // An EAN-13 barcode has 59 bars/spaces:
  // 3 (start 1,1,1) + 24 (6 left digits * 4) + 5 (center 1,1,1,1,1) + 24 (6 right digits * 4) + 3 (end 1,1,1)
  for (let start = 0; start + 59 <= runs.length; start++) {
    const unit = (runs[start] + runs[start + 1] + runs[start + 2]) / 3;
    if (unit < 0.8) continue;
    if (
      Math.abs(runs[start] - unit) > unit * 0.8 ||
      Math.abs(runs[start + 1] - unit) > unit * 0.8 ||
      Math.abs(runs[start + 2] - unit) > unit * 0.8
    ) {
      continue;
    }

    let parity = '';
    const digits: number[] = [];
    let ok = true;

    for (let d = 0; d < 6; d++) {
      const key = normalizeFourBars(runs, start + 3 + d * 4);
      if (key in EAN_L) {
        parity += 'L';
        digits.push(EAN_L[key]);
      } else if (key in EAN_G) {
        parity += 'G';
        digits.push(EAN_G[key]);
      } else {
        ok = false;
        break;
      }
    }
    if (!ok || !(parity in EAN_PARITY)) continue;
    const firstDigit = EAN_PARITY[parity];

    const rightOffset = start + 3 + 24 + 5;
    for (let d = 0; d < 6; d++) {
      const key = normalizeFourBars(runs, rightOffset + d * 4);
      if (key in EAN_R) {
        digits.push(EAN_R[key]);
      } else {
        ok = false;
        break;
      }
    }
    if (!ok) continue;

    const full = [firstDigit, ...digits];
    let checksum = 0;
    for (let i = 0; i < 12; i++) {
      checksum += full[i] * (i % 2 === 0 ? 1 : 3);
    }
    const checkDigit = (10 - (checksum % 10)) % 10;
    if (checkDigit === full[12]) {
      return full.join('');
    }
  }
  return undefined;
}

export function decode1dBarcodeFromImageData(
  data: Uint8ClampedArray,
  w: number,
  h: number,
): string | undefined {
  if (w < 60 || h < 10 || data.length < w * h * 4) return undefined;
  const rows = [0.5, 0.4, 0.6, 0.3, 0.7];
  const votes = new Map<string, number>();

  for (const frac of rows) {
    const y = Math.min(h - 1, Math.max(0, Math.floor(h * frac)));
    const rowStart = y * w * 4;
    const lum = new Uint8Array(w);
    let minL = 255;
    let maxL = 0;
    for (let x = 0; x < w; x++) {
      const idx = rowStart + x * 4;
      const l = (data[idx] * 77 + data[idx + 1] * 150 + data[idx + 2] * 29) >> 8;
      lum[x] = l;
      if (l < minL) minL = l;
      if (l > maxL) maxL = l;
    }
    if (maxL - minL < 35) continue;
    const threshold = (minL + maxL) >> 1;

    // Build run lengths starting on a dark bar (isDark = true)
    const runs: number[] = [];
    let currentDark = lum[0] < threshold;
    let count = 0;
    for (let x = 0; x < w; x++) {
      const isDark = lum[x] < threshold;
      if (isDark === currentDark) {
        count++;
      } else {
        if (runs.length > 0 || currentDark) {
          runs.push(count);
        }
        currentDark = isDark;
        count = 1;
      }
    }
    if (count > 0 && (runs.length > 0 || currentDark)) {
      runs.push(count);
    }

    // Only dark-start indices (even indices in runs) can start a guard pattern
    for (let offset = 0; offset < 2 && offset < runs.length; offset += 2) {
      const decoded = tryDecodeEan13Runs(runs.slice(offset));
      if (decoded) {
        const nextCount = (votes.get(decoded) ?? 0) + 1;
        if (nextCount >= 2 || rows.length === 1) return decoded;
        votes.set(decoded, nextCount);
      }
    }
  }
  return votes.keys().next().value;
}

/**
 * Draw a frame/bitmap onto a canvas and decode a QR with jsQR (plus optional 1D
 * barcode fallback). A canvas that can't be drawn or read (jsdom without the
 * canvas package, a video with no frame yet) yields undefined. The caller owns
 * the canvas so the live loop can reuse one across frames.
 */
export function decodeCanvas(
  canvas: HTMLCanvasElement,
  source: CanvasImageSource,
  w: number,
  h: number,
  inversionAttempts: 'dontInvert' | 'attemptBoth',
  includeBarcodes = false,
): string | undefined {
  if (w <= 0 || h <= 0) return undefined;
  try {
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return undefined;
    ctx.drawImage(source, 0, 0, w, h);
    const img = ctx.getImageData(0, 0, w, h);
    const qr = jsQR(img.data, w, h, { inversionAttempts })?.data;
    if (qr) return qr;
    if (includeBarcodes) {
      return decode1dBarcodeFromImageData(img.data, w, h);
    }
    return undefined;
  } catch {
    return undefined;
  }
}

/** jsQR's first, fast read of a photo: its long side scaled down to this. */
const PHOTO_FAST_SIDE = 1600;
/**
 * The second read, at the photo's own size up to this long side. iOS Safari
 * refuses a canvas over about 16.7 MP, which a 24 MP photo would be.
 */
const PHOTO_FULL_SIDE = 4096;

function scaledTo(bitmap: ImageBitmap, maxSide: number): [number, number] {
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  return [Math.max(1, Math.round(bitmap.width * scale)), Math.max(1, Math.round(bitmap.height * scale))];
}

/**
 * Decode a QR (or 1D barcode when includeBarcodes is true) from a captured photo.
 * The photo is taken with the phone's native camera (file input + capture), which
 * focuses far better than an in-page getUserMedia preview. Prefer the platform
 * decoder; fall back to jsQR (+ 1D barcode fallback), first scaled down for speed,
 * then near its own size (#3616). Rejects only when the image itself can't be opened.
 */
export async function decodeImageBlob(
  blob: Blob,
  includeBarcodes = false,
): Promise<string | undefined> {
  const bitmap = await createImageBitmap(blob, { imageOrientation: 'from-image' });
  try {
    const detector = createQrDetector(includeBarcodes);
    if (detector) {
      try {
        const codes = await detector.detect(bitmap);
        if (codes[0]?.rawValue) return codes[0].rawValue;
      } catch {
        /* the platform decoder failed on this image — jsQR gets a go */
      }
    }
    const canvas = document.createElement('canvas');
    const [w, h] = scaledTo(bitmap, PHOTO_FAST_SIDE);
    const fast = includeBarcodes
      ? decodeCanvas(canvas, bitmap, w, h, 'attemptBoth', true)
      : decodeCanvas(canvas, bitmap, w, h, 'attemptBoth');
    if (fast) {
      canvas.width = 0;
      canvas.height = 0;
      return fast;
    }
    const [fw, fh] = scaledTo(bitmap, PHOTO_FULL_SIDE);
    // A photo no larger than the first read has nothing more to give.
    if (fw === w && fh === h) {
      canvas.width = 0;
      canvas.height = 0;
      return undefined;
    }
    const full = includeBarcodes
      ? decodeCanvas(canvas, bitmap, fw, fh, 'attemptBoth', true)
      : decodeCanvas(canvas, bitmap, fw, fh, 'attemptBoth');
    // iOS Safari counts a canvas's memory until it is resized, not until it
    // is collected: give the 50 MB back now, for the next photo.
    canvas.width = 0;
    canvas.height = 0;
    return full;
  } finally {
    bitmap.close?.();
  }
}

/**
 * A decoder for the live preview's frames. It starts on the platform decoder
 * and drops it for jsQR the first time it throws or rejects. The returned
 * promise never rejects, so the loop waiting on it always gets to its next
 * frame.
 */
export function createFrameDecoder(
  includeBarcodes = false,
): (video: HTMLVideoElement, canvas: HTMLCanvasElement) => Promise<string | undefined> {
  let detector = createQrDetector(includeBarcodes);
  return async (video, canvas) => {
    if (detector) {
      try {
        const codes = await detector.detect(video);
        return codes[0]?.rawValue;
      } catch {
        detector = null;
      }
    }
    // 'dontInvert' (cheapest) for the hot loop; the photo path uses 'attemptBoth'.
    if (includeBarcodes) {
      return decodeCanvas(canvas, video, video.videoWidth, video.videoHeight, 'dontInvert', true);
    }
    return decodeCanvas(canvas, video, video.videoWidth, video.videoHeight, 'dontInvert');
  };
}
