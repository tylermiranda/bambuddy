"""Official Bambu Lab filament box, barcode, SKU, and code scanner service."""

from __future__ import annotations

import io
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.services.spool_tag_matcher import bambu_spool_core_weight

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent.parent / "data" / "bambu_filaments.json"
BARCODE_MAP_SETTING_KEY = "bambu_barcode_map"

# Matches Bambu SKU strings like:
#   A00-W01-1.75-1000-SPL
#   A00-K0-1.75-1000-SPLFREE
#   G02-B0-1.75-1000-SPL-US
#   A01-W02
_SKU_VARIANT_RE = re.compile(
    r"\b(?:GF)?([A-Z]{1,3}\d{2})-([A-Z]\d{1,2})(?:-(\d+(?:\.\d+)?))?(?:-(\d{3,4}))?(?:-(SPLFREE|SPL))?\b",
    re.IGNORECASE,
)

# 5-digit Bambu filament code (10100..99999), optionally prefixed with F- or # or in parens
_FILAMENT_CODE_RE = re.compile(r"(?:^|[\s(#\-:])([1-9]\d{4})(?:$|[\s)#\-:])")

# 12- or 13-digit retail UPC / EAN-13 barcode
_RETAIL_BARCODE_RE = re.compile(r"^(\d{12,14})$")


@lru_cache(maxsize=1)
def load_bambu_catalog() -> dict[str, Any]:
    """Load the static official Bambu Lab filament catalog JSON."""
    if not DATA_FILE.exists():
        logger.warning("Bambu filament catalog file missing at %s", DATA_FILE)
        return {"version": "1.0.0", "filaments": [], "barcodes": {}}
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    filaments: list[dict[str, Any]] = data.get("filaments", [])
    by_code: dict[str, dict[str, Any]] = {}
    by_variant: dict[str, dict[str, Any]] = {}
    by_filament_id: dict[str, list[dict[str, Any]]] = {}

    for entry in filaments:
        code = str(entry.get("filament_code", "")).strip()
        if code:
            by_code[code] = entry
        fid = str(entry.get("filament_id", "")).strip().upper()
        if fid:
            by_filament_id.setdefault(fid, []).append(entry)
            if fid.startswith("GF"):
                by_filament_id.setdefault(fid[2:], []).append(entry)
        for vid in entry.get("variant_ids", []):
            norm_vid = _normalize_variant_id(vid)
            if norm_vid and norm_vid not in by_variant:
                by_variant[norm_vid] = entry
            raw_vid = vid.strip().upper()
            if raw_vid and raw_vid not in by_variant:
                by_variant[raw_vid] = entry

    return {
        "version": data.get("version", "1.0.0"),
        "filaments": filaments,
        "by_code": by_code,
        "by_variant": by_variant,
        "by_filament_id": by_filament_id,
        "barcodes": data.get("barcodes", {}),
    }


def _normalize_variant_id(variant: str) -> str:
    """Canonicalize 'GFA00-W01' / 'A00-W01' / 'A00-W1' -> 'A00-W1'."""
    v = variant.strip().upper()
    if v.startswith("GF") and len(v) > 4 and v[2].isalpha():
        v = v[2:]
    m = re.match(r"^([A-Z]{1,3}\d{2})-([A-Z]+?)0+(\d+)$", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    return v


def _detect_refill_and_weight(text: str) -> tuple[bool | None, int | None]:
    """Detect whether raw scan text indicates Refill vs With Spool and net weight (g)."""
    upper = text.upper()
    is_refill: bool | None = None
    if "SPLFREE" in upper or "REFILL" in upper or "SPOOL-LESS" in upper or "SPOOLLESS" in upper or "WITHOUT SPOOL" in upper:
        is_refill = True
    elif "-SPL" in upper or "WITH SPOOL" in upper:
        is_refill = False

    weight: int | None = None
    m_sku_w = re.search(r"-1\.75-(250|500|1000|2000)\b", upper)
    if m_sku_w:
        weight = int(m_sku_w.group(1))
    elif re.search(r"\b0\.25\s*KG\b|\b250\s*G\b", upper):
        weight = 250
    elif re.search(r"\b0\.5\s*KG\b|\b500\s*G\b", upper):
        weight = 500
    elif re.search(r"\b1(?:\.0)?\s*KG\b|\b1000\s*G\b", upper):
        weight = 1000

    return is_refill, weight


async def get_learned_barcode_map(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Read user-learned retail barcode mappings from the Settings table."""
    result = await db.execute(select(Settings).where(Settings.key == BARCODE_MAP_SETTING_KEY))
    row = result.scalar_one_or_none()
    if not row or not row.value:
        return {}
    try:
        parsed = json.loads(row.value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def save_learned_barcode_mapping(
    db: AsyncSession,
    barcode: str,
    filament_code: str,
    is_refill: bool = False,
    label_weight: int = 1000,
) -> dict[str, Any]:
    """Save a retail barcode -> Bambu filament code mapping in Settings."""
    clean_barcode = barcode.strip()
    clean_code = filament_code.strip()
    catalog = load_bambu_catalog()
    if clean_code not in catalog["by_code"]:
        raise ValueError(f"Unknown Bambu filament code: {clean_code}")

    current = await get_learned_barcode_map(db)
    entry = {
        "filament_code": clean_code,
        "is_refill": bool(is_refill),
        "label_weight": int(label_weight) if label_weight > 0 else 1000,
    }
    current[clean_barcode] = entry

    result = await db.execute(select(Settings).where(Settings.key == BARCODE_MAP_SETTING_KEY))
    row = result.scalar_one_or_none()
    if row:
        row.value = json.dumps(current)
    else:
        db.add(Settings(key=BARCODE_MAP_SETTING_KEY, value=json.dumps(current)))
    await db.commit()
    return entry


def search_bambu_catalog(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search official Bambu Lab filaments by 5-digit code, SKU variant, material, or color name."""
    catalog = load_bambu_catalog()
    q = (query or "").strip()
    if not q:
        return catalog["filaments"][:limit]

    q_lower = q.lower()
    q_tokens = [tok for tok in re.split(r"[\s\-_/(),]+", q_lower) if tok]

    scored: list[tuple[int, dict[str, Any]]] = []
    for item in catalog["filaments"]:
        code = item["filament_code"].lower()
        fid = item["filament_id"].lower()
        fila_type = item["fila_type"].lower()
        color_name = item["color_name"].lower()
        variants = [v.lower() for v in item.get("variant_ids", [])]
        haystack = f"bambu lab {fila_type} {color_name} {code} {fid} {' '.join(variants)}"

        if code == q_lower or q_lower in variants:
            scored.append((100, item))
            continue
        if code.startswith(q_lower):
            scored.append((90, item))
            continue
        if color_name == q_lower:
            scored.append((85, item))
            continue
        if all(tok in haystack for tok in q_tokens):
            score = 50
            if color_name.startswith(q_lower):
                score += 20
            if fila_type in q_lower:
                score += 15
            scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["fila_type"], pair[1]["filament_code"]))
    return [item for _, item in scored[:limit]]


async def resolve_bambu_scan(db: AsyncSession, raw_input: str) -> dict[str, Any]:
    """Resolve a scanned barcode, SKU, 5-digit Bambu code, or label text into a Spool prefill."""
    text = (raw_input or "").strip()
    catalog = load_bambu_catalog()
    plastic_core_weight, plastic_catalog_id = await bambu_spool_core_weight(db)

    is_refill_hint, weight_hint = _detect_refill_and_weight(text)

    def _build_response(
        *,
        matched: bool,
        match_type: str,
        filament: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        is_refill: bool = False,
        label_weight: int = 1000,
        detected_barcode: str | None = None,
    ) -> dict[str, Any]:
        effective_refill = is_refill_hint if is_refill_hint is not None else is_refill
        effective_weight = weight_hint if weight_hint is not None else label_weight
        return {
            "matched": matched,
            "match_type": match_type,
            "raw_input": text,
            "detected_barcode": detected_barcode,
            "is_refill": effective_refill,
            "label_weight": effective_weight,
            "core_weight": 0 if effective_refill else plastic_core_weight,
            "core_weight_catalog_id": None if effective_refill else plastic_catalog_id,
            "filament": filament,
            "candidates": candidates or [],
        }

    if not text:
        return _build_response(matched=False, match_type="none")

    # 1. Exact 5-digit Bambu filament code (e.g. "10100" or "F-10100")
    clean_digits = re.sub(r"^[Ff]#?-?", "", text.strip())
    if re.fullmatch(r"[1-9]\d{4}", clean_digits) and clean_digits in catalog["by_code"]:
        return _build_response(
            matched=True,
            match_type="filament_code",
            filament=catalog["by_code"][clean_digits],
        )

    # 2. SKU / Variant ID (e.g. "A00-W01-1.75-1000-SPL", "A01-K1", "GFA00-W01")
    sku_match = _SKU_VARIANT_RE.search(text)
    if sku_match:
        prefix, color_part, _diam, sku_weight, spl_token = sku_match.groups()
        raw_vid = f"{prefix.upper()}-{color_part.upper()}"
        norm_vid = _normalize_variant_id(raw_vid)
        entry = catalog["by_variant"].get(raw_vid) or catalog["by_variant"].get(norm_vid)
        sku_refill = (spl_token or "").upper() == "SPLFREE"
        sku_w = int(sku_weight) if sku_weight and sku_weight.isdigit() else 1000
        if entry:
            return _build_response(
                matched=True,
                match_type="sku",
                filament=entry,
                is_refill=sku_refill,
                label_weight=sku_w,
            )

    # 3. Retail EAN-13 / UPC barcode (12-14 digits)
    barcode_match = _RETAIL_BARCODE_RE.match(text)
    if barcode_match:
        bc = barcode_match.group(1)
        learned = await get_learned_barcode_map(db)
        # Check exact barcode or leading-0 normalized UPC/EAN-13 equivalent
        bc_variants = [bc]
        if len(bc) == 12:
            bc_variants.append("0" + bc)
        elif len(bc) == 13 and bc.startswith("0"):
            bc_variants.append(bc[1:])

        for candidate_bc in bc_variants:
            mapping = learned.get(candidate_bc) or catalog["barcodes"].get(candidate_bc)
            if mapping:
                fcode = str(mapping.get("filament_code", ""))
                entry = catalog["by_code"].get(fcode)
                if entry:
                    return _build_response(
                        matched=True,
                        match_type="barcode",
                        filament=entry,
                        is_refill=bool(mapping.get("is_refill", False)),
                        label_weight=int(mapping.get("label_weight", 1000)),
                        detected_barcode=bc,
                    )

        # Unmapped retail barcode: return detected_barcode so UI can link it in 1 tap
        return _build_response(
            matched=False,
            match_type="unmapped_barcode",
            detected_barcode=bc,
            candidates=catalog["filaments"][:12],
        )

    # 4. Embedded 5-digit Bambu filament code inside multiline label text (e.g. "Bambu PLA Basic Jade White (10100)")
    for code_m in _FILAMENT_CODE_RE.finditer(text):
        candidate_code = code_m.group(1)
        if candidate_code in catalog["by_code"]:
            return _build_response(
                matched=True,
                match_type="filament_code",
                filament=catalog["by_code"][candidate_code],
            )

    # 5. Slicer filament ID prefix (e.g. "GFA00" or "A00")
    upper_text = text.upper()
    if upper_text in catalog["by_filament_id"]:
        cands = catalog["by_filament_id"][upper_text]
        return _build_response(
            matched=len(cands) == 1,
            match_type="sku" if len(cands) == 1 else "text",
            filament=cands[0] if len(cands) == 1 else None,
            candidates=cands[:25],
        )

    # 6. Label text / color & material search
    cands = search_bambu_catalog(text, limit=20)
    if len(cands) == 1:
        return _build_response(matched=True, match_type="text", filament=cands[0], candidates=cands)
    # Check if top candidate is an exact "<fila_type> <color_name>" or "<color_name>" match
    norm_q = " ".join(text.lower().replace("bambu lab", "").replace("bambu", "").split())
    for c in cands:
        full_label = f"{c['fila_type']} {c['color_name']}".lower()
        rev_label = f"{c['color_name']} {c['fila_type']}".lower()
        if norm_q in (full_label, rev_label):
            return _build_response(matched=True, match_type="text", filament=c, candidates=cands)

    return _build_response(
        matched=False,
        match_type="text" if cands else "none",
        candidates=cands,
    )


def decode_barcodes_from_image_bytes(image_bytes: bytes) -> list[str]:
    """Decode 1D barcodes and 2D QR codes from an uploaded image using OpenCV + PIL."""
    results: list[str] = []
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        pil_img = Image.open(io.BytesIO(image_bytes))
        pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")
        arr = np.array(pil_img)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

        # 1. Try OpenCV 1D BarcodeDetector if available in this build
        barcode_cls = getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)
        if barcode_cls is not None:
            try:
                bd = barcode_cls()
                retval, decoded_info, *_ = bd.detectAndDecode(bgr)
                if retval and decoded_info:
                    for val in decoded_info:
                        if val and val.strip() and val.strip() not in results:
                            results.append(val.strip())
            except Exception:
                pass

        # 2. Try OpenCV QRCodeDetector
        try:
            qd = cv2.QRCodeDetector()
            retval, decoded_info, _, _ = qd.detectAndDecodeMulti(bgr)
            if retval and decoded_info:
                for val in decoded_info:
                    if val and val.strip() and val.strip() not in results:
                        results.append(val.strip())
            else:
                val, _, _ = qd.detectAndDecode(bgr)
                if val and val.strip() and val.strip() not in results:
                    results.append(val.strip())
        except Exception:
            pass
    except Exception as exc:
        logger.debug("Server-side image barcode decode skipped: %s", exc)

    return results
