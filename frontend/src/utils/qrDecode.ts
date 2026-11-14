/**
 * QR and 1D barcode decoding for the spool & Bambu filament scanners (#1574):
 * uses the platform BarcodeDetector where available, falling back to jsQR (and
 * a lightweight 1D EAN-13/UPC-A scanline decoder when includeBarcodes is enabled).
 *
 * Nothing here throws or rejects for an unreadable frame or photo -- that is a
 * frame or photo without a code -- so a failure can't stop the live scan loop.
 */

import jsQR from 'jsqr';
import {
  MultiFormatReader,
  RGBLuminanceSource,
  BinaryBitmap,
  HybridBinarizer,
  DecodeHintType,
  BarcodeFormat,
} from '@zxing/library';

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

// ── Multi-Format Barcode Decoder via ZXing (fallback for iOS Safari / non-BarcodeDetector) ──
let zxingReader: MultiFormatReader | null = null;
let zxingHints: Map<DecodeHintType, unknown> | null = null;

function getZxingReader(): { reader: MultiFormatReader; hints: Map<DecodeHintType, unknown> } {
  if (!zxingReader) {
    zxingReader = new MultiFormatReader();
    zxingHints = new Map();
    zxingHints.set(DecodeHintType.POSSIBLE_FORMATS, [
      BarcodeFormat.EAN_13,
      BarcodeFormat.CODE_128,
      BarcodeFormat.UPC_A,
      BarcodeFormat.EAN_8,
      BarcodeFormat.CODE_39,
      BarcodeFormat.UPC_E,
      BarcodeFormat.QR_CODE,
      BarcodeFormat.DATA_MATRIX,
    ]);
    zxingHints.set(DecodeHintType.TRY_HARDER, true);
  }
  return { reader: zxingReader, hints: zxingHints };
}

export function decode1dBarcodeFromImageData(data: Uint8ClampedArray, width: number, height: number): string | undefined {
  if (width <= 0 || height <= 0 || data.length < width * height * 4) return undefined;
  try {
    const len = width * height;
    const luminances = new Uint8ClampedArray(len);
    for (let i = 0; i < len; i++) {
      const idx = i * 4;
      luminances[i] = (data[idx] + data[idx + 1] + data[idx + 2]) / 3;
    }
    const source = new RGBLuminanceSource(luminances, width, height);
    const bitmap = new BinaryBitmap(new HybridBinarizer(source));
    const { reader, hints } = getZxingReader();
    const result = reader.decode(bitmap, hints);
    return result.getText();
  } catch {
    return undefined;
  }
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
