"""Physics-based thermal simulation, failure-risk diagnostics, and geometry-safe
G-code speed/cooling optimization ("Assess & Enhance") for Bambu 3MF plates.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.app.schemas.print_advisor import (
    AdvisorFinding,
    AdvisorPriority,
    AssessResponse,
    LayerThermalPoint,
    PrintAdvisorBadgeSummary,
)
from backend.app.services.slice_output_check import start_gcode_is_missing
from backend.app.utils.threemf_tools import default_plate_gcode_name, select_plate_gcode_name

logger = logging.getLogger(__name__)

FILAMENT_AREA_MM2 = math.pi * (1.75 / 2.0) ** 2  # ~2.40528 mm²
GRID_SIZE = 16
_PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"
_SLICE_INFO_PATH = "Metadata/slice_info.config"


@dataclass(frozen=True)
class MaterialPhysics:
    name: str
    t_nozzle: float
    t_amb: float
    t_bond_min: float
    t_ideal: float
    t_sag_tg: float
    max_flow_mm3s: float
    tau_base_s: float
    hygroscopic: bool
    max_fan_pwm: int


MATERIAL_PROFILES: dict[str, MaterialPhysics] = {
    "PLA": MaterialPhysics("PLA", 220.0, 30.0, 18.0, 35.0, 58.0, 21.0, 1.8, False, 255),
    "PLA-SILK": MaterialPhysics("PLA-SILK", 225.0, 30.0, 20.0, 36.0, 56.0, 14.0, 2.1, False, 255),
    "PETG": MaterialPhysics("PETG", 250.0, 42.0, 39.0, 48.0, 76.0, 13.0, 2.4, True, 204),
    "TPU": MaterialPhysics("TPU", 230.0, 30.0, 18.0, 35.0, 68.0, 3.8, 2.6, True, 178),
    "ABS": MaterialPhysics("ABS", 260.0, 45.0, 52.0, 72.0, 102.0, 16.0, 3.2, False, 102),
    "ASA": MaterialPhysics("ASA", 260.0, 45.0, 52.0, 72.0, 102.0, 16.0, 3.2, False, 102),
    "PA": MaterialPhysics("PA", 280.0, 45.0, 50.0, 70.0, 98.0, 12.0, 3.2, True, 76),
    "PC": MaterialPhysics("PC", 280.0, 50.0, 65.0, 92.0, 135.0, 14.0, 3.5, True, 76),
}

_PLATE_NUM_RE = re.compile(r"plate_(\d+)\.gcode$", re.IGNORECASE)
_F_PARAM_RE = re.compile(r"\bF([0-9]*\.?[0-9]+)", re.IGNORECASE)
_M73_R_RE = re.compile(r"(\bM73\b[^\n;]*?\bR)(\d+)", re.IGNORECASE)
_PREDICTION_RE = re.compile(r'(<metadata\s+key="prediction"\s+value=")(\d+)(")')
_PLATE_BLOCK_RE = re.compile(r"<plate\b[^>]*>.*?</plate>", re.DOTALL | re.IGNORECASE)
_META_INDEX_RE = re.compile(r'<metadata\s+key="index"\s+value="(\d+)"', re.IGNORECASE)
_META_NOZZLE_RE = re.compile(r'<metadata\s+key="nozzle_diameters"\s+value="([^"]+)"', re.IGNORECASE)
_HELIO_HEADER_RE = re.compile(r"^\s*;\s*helioadditive\s*=", re.IGNORECASE | re.MULTILINE)


@dataclass
class ResolvedProfile:
    physics: MaterialPhysics
    default_accel_mms2: float
    nozzle_diam_mm: float = 0.4


def _parse_plate_slice_info(slice_info_xml: str | None, plate_id: int) -> tuple[int | None, float | None]:
    """Extract ``(prediction_s, nozzle_diam_mm)`` for ``plate_id`` from ``slice_info.config``."""
    if not slice_info_xml:
        return None, None
    blocks = [m.group(0) for m in _PLATE_BLOCK_RE.finditer(slice_info_xml)]
    target_block: str | None = None
    for blk in blocks:
        idx_m = _META_INDEX_RE.search(blk)
        if idx_m and int(idx_m.group(1)) == plate_id:
            target_block = blk
            break
    if target_block is None and len(blocks) == 1:
        target_block = blocks[0]
    scope = target_block or slice_info_xml

    pred_s: int | None = None
    pm = _PREDICTION_RE.search(scope)
    if pm:
        pred_s = int(pm.group(2))

    noz_mm: float | None = None
    nm = _META_NOZZLE_RE.search(scope)
    if nm:
        first_tok = nm.group(1).split(",")[0].strip()
        cand = _first_float(first_tok)
        if cand and 0.15 <= cand <= 1.2:
            noz_mm = cand
    return pred_s, noz_mm


def _scale_plate_prediction_xml(raw_xml: str, plate_id: int, time_ratio: float, fallback_s: int) -> tuple[str, int | None, int | None]:
    """Scale ``prediction`` inside the matching ``<plate>`` block by ``time_ratio``."""
    orig_pred: int | None = None
    new_pred: int | None = None

    def _replace_pred(block_text: str) -> str:
        nonlocal orig_pred, new_pred
        pm = _PREDICTION_RE.search(block_text)
        if not pm:
            return block_text
        orig_pred = int(pm.group(2))
        new_pred = max(1, int(round(orig_pred * time_ratio))) if orig_pred > 0 else max(1, fallback_s)
        return _PREDICTION_RE.sub(rf"\g<1>{new_pred}\g<3>", block_text, count=1)

    blocks = list(_PLATE_BLOCK_RE.finditer(raw_xml))
    for m in blocks:
        blk = m.group(0)
        idx_m = _META_INDEX_RE.search(blk)
        if (idx_m and int(idx_m.group(1)) == plate_id) or len(blocks) == 1:
            updated_blk = _replace_pred(blk)
            return raw_xml[: m.start()] + updated_blk + raw_xml[m.end() :], orig_pred, new_pred

    return _replace_pred(raw_xml), orig_pred, new_pred


def _first_float(val: object) -> float | None:
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    try:
        s = str(val).strip().rstrip("%")
        return float(s) if s else None
    except ValueError:
        return None


def _first_str(val: object) -> str:
    if isinstance(val, list):
        return str(val[0]).strip() if val else ""
    return str(val).strip() if val is not None else ""


def _normalize_material_key(raw_type: str, raw_preset_id: str = "") -> str:
    upper = f"{raw_type} {raw_preset_id}".upper()
    if "SILK" in upper:
        return "PLA-SILK"
    for key in ("PETG", "TPU", "ASA", "ABS", "PC", "PA"):
        if key in upper:
            return key
    if "NYLON" in upper:
        return "PA"
    return "PLA"


def _slot_item(val: object, slot: int) -> object:
    if isinstance(val, list):
        if not val:
            return None
        return val[slot] if 0 <= slot < len(val) else val[0]
    return val


def _resolve_profile_from_settings(
    settings_dict: dict | None,
    gcode_sample: str,
    slice_nozzle_mm: float | None = None,
) -> ResolvedProfile:
    raw_type = ""
    raw_preset = ""
    nozzle_override: float | None = None
    flow_override: float | None = None
    nozzle_diam: float = slice_nozzle_mm if (slice_nozzle_mm and 0.15 <= slice_nozzle_mm <= 1.2) else 0.4
    accel = 6000.0

    # Detect first active AMS filament slot (M620 S<slot> or T<slot>) so multi-slot
    # projects don't inherit an unused Slot 1 TPU volumetric cap on a Slot 2 PLA print!
    active_slot = 0
    m_slot = re.search(r"(?:^|\n)\s*M620\s+S(\d+)", gcode_sample)
    if m_slot:
        cand = int(m_slot.group(1))
        if 0 <= cand <= 15:
            active_slot = cand

    if isinstance(settings_dict, dict):
        raw_type = _first_str(_slot_item(settings_dict.get("filament_type"), active_slot))
        raw_preset = _first_str(_slot_item(settings_dict.get("filament_settings_id"), active_slot))
        nozzle_override = _first_float(_slot_item(settings_dict.get("nozzle_temperature"), active_slot))
        flow_override = _first_float(_slot_item(settings_dict.get("filament_max_volumetric_speed"), active_slot))
        if slice_nozzle_mm is None:
            nd = _first_float(_slot_item(settings_dict.get("nozzle_diameter"), 0))
            if nd and 0.15 <= nd <= 1.2:
                nozzle_diam = nd
        parsed_accel = _first_float(settings_dict.get("default_acceleration"))
        if parsed_accel and parsed_accel > 100.0:
            accel = parsed_accel

    for line in gcode_sample.splitlines():
        if not line.startswith(";"):
            continue
        body = line[1:].strip()
        if "=" not in body:
            continue
        k, v = [p.strip() for p in body.split("=", 1)]
        kl = k.lower()
        tokens = [tok.strip().strip('"') for tok in re.split(r"[;,]", v) if tok.strip()]
        picked = tokens[active_slot] if 0 <= active_slot < len(tokens) else (tokens[0] if tokens else "")
        if kl == "filament_type" and not raw_type:
            raw_type = picked
        elif kl == "filament_settings_id" and not raw_preset:
            raw_preset = picked
        elif kl == "nozzle_temperature" and nozzle_override is None:
            nozzle_override = _first_float(picked)
        elif kl == "filament_max_volumetric_speed" and flow_override is None:
            flow_override = _first_float(picked)
        elif kl == "nozzle_diameter" and slice_nozzle_mm is None:
            nd = _first_float(picked)
            if nd and 0.15 <= nd <= 1.2:
                nozzle_diam = nd
        elif kl == "default_acceleration":
            pa = _first_float(picked)
            if pa and pa > 100.0:
                accel = pa

    mat_key = _normalize_material_key(raw_type, raw_preset)
    base = MATERIAL_PROFILES[mat_key]
    # Guard against unconfigured slot placeholder (e.g. 2 mm³/s on a >=0.4mm non-TPU print)
    if flow_override is not None and flow_override <= 3.0 and nozzle_diam >= 0.35 and mat_key != "TPU":
        flow_override = None

    physics = MaterialPhysics(
        name=base.name,
        t_nozzle=nozzle_override if (nozzle_override and nozzle_override >= 170.0) else base.t_nozzle,
        t_amb=base.t_amb,
        t_bond_min=base.t_bond_min,
        t_ideal=base.t_ideal,
        t_sag_tg=base.t_sag_tg,
        max_flow_mm3s=flow_override if (flow_override and flow_override > 0.5) else base.max_flow_mm3s,
        tau_base_s=base.tau_base_s,
        hygroscopic=base.hygroscopic,
        max_fan_pwm=base.max_fan_pwm,
    )
    return ResolvedProfile(physics=physics, default_accel_mms2=accel, nozzle_diam_mm=round(nozzle_diam, 2))


def _read_axis(cmd_part: str, axis: str) -> float | None:
    idx = cmd_part.find(axis)
    if idx < 0:
        return None
    start = idx + 1
    end = start
    n = len(cmd_part)
    while end < n and (cmd_part[end].isdigit() or cmd_part[end] in "+-."):
        end += 1
    if end == start:
        return None
    try:
        return float(cmd_part[start:end])
    except ValueError:
        return None


def _motion_geometry(
    op: str,
    cmd_part: str,
    x: float,
    y: float,
    z: float,
    tx: float,
    ty: float,
    tz: float,
) -> tuple[float, bool, float, float]:
    """Return ``(dist_3d_mm, has_xy_motion, mid_x, mid_y)`` for linear and circular G-code moves."""
    if op not in ("G2", "G3"):
        dx = tx - x
        dy = ty - y
        dz = tz - z
        return (
            math.sqrt(dx * dx + dy * dy + dz * dz),
            (tx != x or ty != y),
            0.5 * (x + tx),
            0.5 * (y + ty),
        )

    two_pi = 2.0 * math.pi
    i_off = _read_axis(cmd_part, "I")
    j_off = _read_axis(cmd_part, "J")
    if i_off is not None or j_off is not None:
        ii = 0.0 if i_off is None else i_off
        jj = 0.0 if j_off is None else j_off
        r0 = math.hypot(ii, jj)
        if r0 > 1e-6:
            cx = x + ii
            cy = y + jj
            r1 = math.hypot(tx - cx, ty - cy)
            r = 0.5 * (r0 + r1)
            chord = math.hypot(tx - x, ty - y)
            a0 = math.atan2(y - cy, x - cx)
            if chord <= 1e-5:
                sweep = two_pi
            else:
                a1 = math.atan2(ty - cy, tx - cx)
                sweep = ((a0 - a1) % two_pi) if op == "G2" else ((a1 - a0) % two_pi)
                if sweep <= 1e-6:
                    sweep = two_pi
            arc_xy = r * sweep
            a_mid = (a0 - 0.5 * sweep) if op == "G2" else (a0 + 0.5 * sweep)
            mx = cx + r * math.cos(a_mid)
            my = cy + r * math.sin(a_mid)
            return math.hypot(arc_xy, tz - z), arc_xy > 1e-6, mx, my

    r_param = _read_axis(cmd_part, "R")
    chord = math.hypot(tx - x, ty - y)
    if r_param is not None and abs(r_param) > 1e-6 and chord > 1e-6:
        r_abs = max(abs(r_param), 0.5 * chord)
        half_ang = math.asin(min(1.0, chord / (2.0 * r_abs)))
        sweep = (two_pi - 2.0 * half_ang) if r_param < 0 else (2.0 * half_ang)
        arc_xy = r_abs * sweep
        return math.hypot(arc_xy, tz - z), True, 0.5 * (x + tx), 0.5 * (y + ty)

    dx = tx - x
    dy = ty - y
    dz = tz - z
    return (
        math.sqrt(dx * dx + dy * dy + dz * dz),
        (tx != x or ty != y),
        0.5 * (x + tx),
        0.5 * (y + ty),
    )


def _move_duration_s(dist_mm: float, speed_mms: float, accel_mms2: float) -> float:
    if dist_mm <= 1e-6:
        return 0.0
    v = max(speed_mms, 1.0)
    a = max(accel_mms2, 200.0)
    d_crit = (v * v) / a
    if dist_mm < d_crit:
        t_kin = 2.0 * math.sqrt(dist_mm / a)
    else:
        t_kin = (dist_mm / v) + (v / a)
    return max(dist_mm / v, t_kin)


def _compute_tqi(t_sub: float, p: MaterialPhysics) -> float:
    if t_sub >= p.t_ideal:
        span = max(p.t_sag_tg - p.t_ideal, 4.0)
        raw = 65.0 * ((t_sub - p.t_ideal) / span)
        return float(min(100.0, raw))
    span = max(p.t_ideal - p.t_bond_min, 4.0)
    raw = -65.0 * ((p.t_ideal - t_sub) / span)
    return float(max(-100.0, raw))


def _extract_plate_from_path(
    file_path: Path, plate_id: int | None
) -> tuple[int, str, dict | None, str | None, str | None]:
    """Return ``(resolved_plate_id, gcode_text, project_settings, target_zip_member, slice_info_xml)``."""
    suffix = file_path.name.lower()
    is_zip = zipfile.is_zipfile(file_path) or suffix.endswith(".3mf")
    if is_zip:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            gcode_files = [n for n in names if n.endswith(".gcode")]
            if not gcode_files:
                raise ValueError("No sliced G-code found in 3MF file")
            if plate_id is not None:
                selected = select_plate_gcode_name(gcode_files, plate_id)
                if selected is None:
                    raise FileNotFoundError(f"Plate {plate_id} not found in 3MF file")
            else:
                selected = default_plate_gcode_name(gcode_files) or gcode_files[0]
            m = _PLATE_NUM_RE.search(selected)
            resolved_plate = int(m.group(1)) if m else (plate_id or 1)
            gcode_text = zf.read(selected).decode("utf-8", errors="ignore")
            settings_dict: dict | None = None
            if _PROJECT_SETTINGS_PATH in names:
                try:
                    loaded = json.loads(zf.read(_PROJECT_SETTINGS_PATH).decode("utf-8", errors="ignore"))
                    if isinstance(loaded, dict):
                        settings_dict = loaded
                except json.JSONDecodeError:
                    settings_dict = None
            slice_info_xml: str | None = None
            if _SLICE_INFO_PATH in names:
                slice_info_xml = zf.read(_SLICE_INFO_PATH).decode("utf-8", errors="ignore")
            return resolved_plate, gcode_text, settings_dict, selected, slice_info_xml

    if plate_id is not None and plate_id != 1:
        raise FileNotFoundError(f"Plate {plate_id} not found in plain G-code file")
    return 1, file_path.read_text(encoding="utf-8", errors="ignore"), None, None, None


def assess_gcode_text(
    gcode_text: str,
    *,
    plate_id: int = 1,
    settings_dict: dict | None = None,
    ams_humidity_pct: float | None = None,
    slice_nozzle_mm: float | None = None,
    slicer_prediction_s: int | None = None,
    printer_nozzle_mm: float | None = None,
    printer_name: str | None = None,
) -> AssessResponse:
    """Run single-pass kinematic, 32x32 bed-cell inter-layer thermal TQI, and risk analysis."""
    # Bambu Studio writes `; CONFIG_BLOCK_START` at the tail of `.gcode` as well as head
    config_sample = gcode_text[:16384] + "\n" + gcode_text[-16384:] if len(gcode_text) > 32768 else gcode_text
    resolved = _resolve_profile_from_settings(settings_dict, config_sample, slice_nozzle_mm=slice_nozzle_mm)
    prof = resolved.physics
    accel = resolved.default_accel_mms2
    plate_nozzle_mm = resolved.nozzle_diam_mm

    has_start_end_marker = "; MACHINE_START_GCODE_END" in gcode_text

    # Single-pass kinematic + 32x32 XY bed-grid (~8mm cells over 256mm bed) inter-layer thermal model.
    # Separating `prev_layer_cell_t` from `cur_layer_cell_t` ensures side-by-side perimeters/infill
    # within the SAME layer do not masquerade as 0.1s vertical layer revisit times!
    bed_grid = 32
    bed_span = 256.0
    prev_layer_cell_t = np.full((bed_grid, bed_grid), -1e9, dtype=np.float64)
    cur_layer_cell_t = np.full((bed_grid, bed_grid), -1e9, dtype=np.float64)

    in_print = not has_start_end_marker
    rel_e = True
    x = y = z = e = 0.0
    has_pos = False
    feed_mm_min = 3000.0
    fan_pwm = 0
    aux_fan_pwm = 0
    line_width = 0.42
    cur_feature = "wall"
    t_now = 0.0

    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    max_z = 0.0
    l0_min_x = l0_min_y = float("inf")
    l0_max_x = l0_max_y = float("-inf")
    saw_brim_or_skirt = False

    saw_explicit_layer = False
    layer_idx = 0
    layer_z = 0.2
    prev_layer_z = 0.0
    layer_has_extrusion = False
    layer_start_t = 0.0
    layer_max_flow = 0.0
    layer_ow_speed_weighted = 0.0
    layer_ow_dist = 0.0
    layer_fan_sum = 0.0
    layer_ext_time = 0.0
    layer_cell_tqis: list[float] = []
    layer_overhang_low_fan = False
    layer_infill_time = 0.0
    upper_fast_moves = False

    layers_out: list[LayerThermalPoint] = []
    fast_savings_s = 0.0

    def flush_layer() -> None:
        nonlocal layer_idx, prev_layer_z, layer_has_extrusion, layer_start_t
        nonlocal layer_max_flow, layer_ow_speed_weighted, layer_ow_dist
        nonlocal layer_fan_sum, layer_ext_time, layer_cell_tqis
        nonlocal layer_overhang_low_fan, layer_infill_time, fast_savings_s
        nonlocal prev_layer_cell_t, cur_layer_cell_t

        if not layer_has_extrusion:
            layer_start_t = t_now
            return

        dur = max(t_now - layer_start_t, 0.01)
        mean_fan_pct = int(round((layer_fan_sum / max(layer_ext_time, 1e-4)) * 100.0 / 255.0))
        mean_fan_pct = max(0, min(100, mean_fan_pct))

        if layer_idx == 0 or not layer_cell_tqis:
            layer_tqi = 0.0
        else:
            arr = np.asarray(layer_cell_tqis, dtype=np.float64)
            p80_pos = float(np.percentile(arr, 80))
            p20_neg = float(np.percentile(arr, 20))
            layer_tqi = p80_pos if abs(p80_pos) >= abs(p20_neg) else p20_neg

        flags: list[str] = []
        if layer_tqi > 45.0 or layer_overhang_low_fan:
            flags.append("overheating_curl")
        if layer_idx > 2 and layer_tqi < -50.0:
            flags.append("cold_delamination")
        if layer_max_flow > 0.92 * prof.max_flow_mm3s:
            flags.append("volumetric_flow_ceiling")

        ow_speed = (layer_ow_speed_weighted / layer_ow_dist) if layer_ow_dist > 0.5 else None

        # Estimate safe time savings on cool/safe layers where infill/inner walls can accelerate
        if layer_tqi < 10.0 and layer_max_flow < 0.82 * prof.max_flow_mm3s:
            fast_savings_s += layer_infill_time * 0.22

        layers_out.append(
            LayerThermalPoint(
                layer=layer_idx,
                z=round(layer_z, 3),
                duration_s=round(dur, 2),
                tqi=round(float(max(-100.0, min(100.0, layer_tqi))), 1),
                max_flow_mm3s=round(layer_max_flow, 2),
                outer_wall_speed_mms=round(ow_speed, 1) if ow_speed is not None else None,
                fan_pct=mean_fan_pct,
                risk_flags=flags,
            )
        )

        # Promote current layer's cell first-visit timestamps for the next Z layer
        prev_layer_cell_t[:] = cur_layer_cell_t
        cur_layer_cell_t.fill(-1e9)

        prev_layer_z = layer_z
        layer_idx += 1
        layer_has_extrusion = False
        layer_start_t = t_now
        layer_max_flow = 0.0
        layer_ow_speed_weighted = 0.0
        layer_ow_dist = 0.0
        layer_fan_sum = 0.0
        layer_ext_time = 0.0
        layer_cell_tqis = []
        layer_overhang_low_fan = False
        layer_infill_time = 0.0

    for raw in gcode_text.splitlines():
        if not raw:
            continue
        c_first = raw[0]
        if c_first in (" ", "\t"):
            s = raw.strip()
            if not s:
                continue
            c_first = s[0]
        else:
            s = raw.rstrip("\r")

        if c_first == ";":
            body = s[1:].strip()
            bu = body.upper()
            if bu.startswith("MACHINE_START_GCODE_END"):
                in_print = True
                layer_start_t = t_now
            elif bu.startswith("EXECUTABLE_BLOCK_END"):
                break
            elif bu in ("CHANGE_LAYER", "LAYER_CHANGE") or bu.startswith(("CHANGE_LAYER ", "LAYER_CHANGE ")):
                saw_explicit_layer = True
                if in_print:
                    flush_layer()
            elif ":" in body:
                k, v = body.split(":", 1)
                ku = k.strip().upper()
                vs = v.strip()
                if ku in ("FEATURE", "TYPE"):
                    cur_feature = vs.lower()
                    if "skirt" in cur_feature or "brim" in cur_feature or "raft" in cur_feature:
                        saw_brim_or_skirt = True
                elif ku in ("LINE_WIDTH", "WIDTH"):
                    pw = _first_float(vs)
                    if pw and pw > 0.05:
                        line_width = pw
                elif ku in ("Z_HEIGHT", "Z"):
                    pz = _first_float(vs)
                    if pz is not None and pz >= 0:
                        layer_z = pz
            continue

        if c_first not in ("G", "M", "g", "m"):
            continue

        cmd_part = s.split(";", 1)[0].strip() if ";" in s else s.strip()
        if not cmd_part:
            continue
        sp = cmd_part.find(" ")
        op = (cmd_part[:sp] if sp > 0 else cmd_part).upper()

        if op not in ("G0", "G1", "G2", "G3"):
            if op == "M83":
                rel_e = True
            elif op == "M82":
                rel_e = False
            elif op == "G92":
                re_val = _read_axis(cmd_part, "E")
                if re_val is not None:
                    e = re_val
            elif op == "M107":
                fan_pwm = 0
            elif op == "M106":
                p_idx = _read_axis(cmd_part, "P")
                s_val = _read_axis(cmd_part, "S")
                if s_val is not None:
                    pwm = max(0, min(255, int(round(s_val))))
                    if p_idx is None or int(p_idx) in (0, 1):
                        fan_pwm = pwm
                    elif int(p_idx) == 2:
                        aux_fan_pwm = pwm
            continue

        f_val = _read_axis(cmd_part, "F")
        if f_val is not None and f_val > 0.0:
            feed_mm_min = f_val

        nx = _read_axis(cmd_part, "X")
        ny = _read_axis(cmd_part, "Y")
        nz = _read_axis(cmd_part, "Z")
        re_val = _read_axis(cmd_part, "E")

        tx = x if nx is None else nx
        ty = y if ny is None else ny
        tz = z if nz is None else nz
        de = 0.0
        if re_val is not None:
            de = re_val if rel_e else (re_val - e)
            e = re_val

        if not has_pos:
            x, y, z = tx, ty, tz
            has_pos = True
            continue

        dist, has_xy_motion, mx, my = _motion_geometry(op, cmd_part, x, y, z, tx, ty, tz)
        speed_mms = feed_mm_min / 60.0
        dt = _move_duration_s(dist, speed_mms, accel)

        if in_print:
            if de > 0.0 and has_xy_motion:
                if not saw_explicit_layer and layer_has_extrusion and abs(tz - layer_z) > 0.04:
                    flush_layer()
                if not layer_has_extrusion:
                    layer_z = tz if tz > 0 else layer_z
                layer_has_extrusion = True

                lo_x = tx if tx <= mx else mx
                hi_x = tx if tx >= mx else mx
                lo_y = ty if ty <= my else my
                hi_y = ty if ty >= my else my
                if lo_x < min_x:
                    min_x = lo_x
                if lo_y < min_y:
                    min_y = lo_y
                if hi_x > max_x:
                    max_x = hi_x
                if hi_y > max_y:
                    max_y = hi_y
                if tz > max_z:
                    max_z = tz
                if layer_idx == 0 and tz <= 0.6:
                    l0_min_x = min(l0_min_x, x, tx, mx)
                    l0_max_x = max(l0_max_x, x, tx, mx)
                    l0_min_y = min(l0_min_y, y, ty, my)
                    l0_max_y = max(l0_max_y, y, ty, my)

                layer_h = max(0.06, min(0.60, layer_z - prev_layer_z if layer_z > prev_layer_z else 0.20))
                flow = (de * FILAMENT_AREA_MM2) / max(dt, 1e-4)
                if flow > layer_max_flow:
                    layer_max_flow = flow

                eff_pwm = min(255.0, fan_pwm + 0.35 * aux_fan_pwm)
                fan_pct_now = (eff_pwm / 255.0) * 100.0
                layer_fan_sum += eff_pwm * dt
                layer_ext_time += dt

                if "outer wall" in cur_feature or cur_feature == "external perimeter":
                    layer_ow_speed_weighted += speed_mms * dist
                    layer_ow_dist += dist
                elif "infill" in cur_feature or "inner wall" in cur_feature or cur_feature == "perimeter":
                    layer_infill_time += dt

                is_ext_overhang = "overhang wall" in cur_feature or cur_feature in ("overhang", "bridge")
                if is_ext_overhang and layer_idx >= 2:
                    min_oh_fan = 55.0 if prof.name in ("PLA", "PLA-SILK") else (25.0 if prof.name == "PETG" else 0.0)
                    if min_oh_fan > 0.0 and fan_pct_now < min_oh_fan:
                        layer_overhang_low_fan = True

                if tz > 40.0 and speed_mms > 200.0:
                    upper_fast_moves = True

                # Evaluate inter-layer cooling once per 8mm cell per layer against prev_layer_cell_t
                # Recording only the FIRST touch per cell per layer prevents late-layer infill from
                # overwriting early-layer perimeter timestamps inside the same 8mm cell.
                cx = max(0, min(bed_grid - 1, int((mx / bed_span) * (bed_grid - 1))))
                cy = max(0, min(bed_grid - 1, int((my / bed_span) * (bed_grid - 1))))
                if cur_layer_cell_t[cx, cy] < -1e8:
                    if layer_idx > 0:
                        last_visit = float(prev_layer_cell_t[cx, cy])
                        if last_visit > -1e8:
                            dt_rev = max(0.05, min(300.0, t_now - last_visit))
                            tau = (
                                prof.tau_base_s
                                * ((line_width * layer_h) / (0.42 * 0.20))
                                * (1.0 / (0.30 + 0.70 * (fan_pct_now / 100.0)))
                            )
                            t_eff_amb = (
                                prof.t_amb - 12.0 * max(0.0, (fan_pct_now - 50.0) / 50.0)
                                if prof.t_amb >= 40.0
                                else prof.t_amb
                            )
                            t_sub = t_eff_amb + (prof.t_nozzle - t_eff_amb) * math.exp(-dt_rev / max(tau, 0.20))
                            layer_cell_tqis.append(_compute_tqi(t_sub, prof))
                    cur_layer_cell_t[cx, cy] = t_now + dt
            elif tz > 40.0 and speed_mms > 200.0 and dist > 5.0:
                upper_fast_moves = True

            t_now += dt

        x, y, z = tx, ty, tz

    if in_print and layer_has_extrusion:
        flush_layer()

    span_x = max(max_x - min_x, 1.0) if math.isfinite(min_x) else 256.0
    span_y = max(max_y - min_y, 1.0) if math.isfinite(min_y) else 256.0

    total_layers = len(layers_out)
    tqi_vals = [lp.tqi for lp in layers_out]
    tqi_mean = round(float(np.mean(tqi_vals)), 1) if tqi_vals else 0.0
    hot_layers = [lp for lp in layers_out if "overheating_curl" in lp.risk_flags]
    cold_layers = [lp for lp in layers_out if "cold_delamination" in lp.risk_flags]
    flow_layers = [lp for lp in layers_out if "volumetric_flow_ceiling" in lp.risk_flags]
    max_flow_seen = max((lp.max_flow_mm3s for lp in layers_out), default=0.0)

    # Outer-wall gloss banding check (ignore initial bottom-shell ramp and minor slowdowns)
    gloss_jump_layers: list[int] = []
    prev_ow: float | None = None
    for lp in layers_out:
        if lp.outer_wall_speed_mms is not None and lp.layer >= 4:
            if prev_ow is not None:
                hi = max(prev_ow, lp.outer_wall_speed_mms)
                lo = max(min(prev_ow, lp.outer_wall_speed_mms), 1.0)
                if (hi / lo) > 1.45 and (hi - lo) >= 30.0:
                    gloss_jump_layers.append(lp.layer)
                    if "outer_wall_gloss_banding" not in lp.risk_flags:
                        lp.risk_flags.append("outer_wall_gloss_banding")
            prev_ow = lp.outer_wall_speed_mms

    # Slenderness / bed detachment check
    l0_span_x = (l0_max_x - l0_min_x) if math.isfinite(l0_min_x) else span_x
    l0_span_y = (l0_max_y - l0_min_y) if math.isfinite(l0_min_y) else span_y
    min_base = max(min(l0_span_x, l0_span_y), 1.0)
    slenderness_ratio = max_z / min_base

    findings: list[AdvisorFinding] = []
    recs: dict[str, str | float | int] = {}

    if hot_layers:
        worst_tqi = max(lp.tqi for lp in hot_layers)
        sev = "critical" if worst_tqi > 75.0 else "warning"
        findings.append(
            AdvisorFinding(
                code="overheating_curl",
                severity=sev,
                title=f"Overheating / curling risk on {len(hot_layers)} layer(s)",
                detail=(
                    f"Local plastic stays above softening threshold (peak TQI +{worst_tqi:.0f}) "
                    f"between layers {hot_layers[0].layer + 1}–{hot_layers[-1].layer + 1}, risking sag or curled overhangs."
                ),
                layer_start=hot_layers[0].layer,
                layer_end=hot_layers[-1].layer,
                slicer_fix="Increase minimum layer time (slow_down_layer_time) or run Print Advisor Enhance (Balanced).",
            )
        )
        recs["slow_down_layer_time"] = 8
        recs["overhang_fan_speed"] = "100%"

    if cold_layers:
        worst_cold = min(lp.tqi for lp in cold_layers)
        findings.append(
            AdvisorFinding(
                code="cold_delamination",
                severity="warning",
                title=f"Overcooling / weak layer bond on {len(cold_layers)} layer(s)",
                detail=(
                    f"Long layer revisit times cool the substrate below optimal bonding window "
                    f"(min TQI {worst_cold:.0f}), reducing Z-axis tensile strength."
                ),
                layer_start=cold_layers[0].layer,
                layer_end=cold_layers[-1].layer,
                slicer_fix="Lower max part-cooling fan speed by 15–25% or run Enhance (Strong / Balanced).",
            )
        )
        recs["fan_max_speed"] = "60%" if prof.name == "PETG" else "80%"

    if flow_layers:
        sev = "critical" if max_flow_seen > 1.05 * prof.max_flow_mm3s else "warning"
        findings.append(
            AdvisorFinding(
                code="volumetric_flow_ceiling",
                severity=sev,
                title=f"Hotend volumetric flow near/above limit ({max_flow_seen:.1f} / {prof.max_flow_mm3s:.1f} mm³/s)",
                detail=(
                    f"{len(flow_layers)} layer(s) push extrusion beyond 92% of {prof.name}'s melt ceiling, "
                    "risking extruder skipping, matte under-extrusion, or weak infill."
                ),
                layer_start=flow_layers[0].layer,
                layer_end=flow_layers[-1].layer,
                slicer_fix=f"Cap filament_max_volumetric_speed at {round(prof.max_flow_mm3s * 0.88, 1)} mm³/s.",
            )
        )
        recs["filament_max_volumetric_speed"] = round(prof.max_flow_mm3s * 0.88, 1)

    if slenderness_ratio > 6.0 and not saw_brim_or_skirt and (upper_fast_moves or max_z > 60.0):
        top_start = max(0, int(total_layers * 0.6))
        for lp in layers_out[top_start:]:
            if "slenderness_detachment" not in lp.risk_flags:
                lp.risk_flags.append("slenderness_detachment")
        findings.append(
            AdvisorFinding(
                code="slenderness_detachment",
                severity="warning",
                title=f"Tall-part knock-over risk ({slenderness_ratio:.1f}:1 height-to-base ratio without brim)",
                detail=(
                    f"Model is {max_z:.1f} mm tall on a {min_base:.1f} mm narrow base with no brim detected. "
                    "High-leverage upper moves risk detaching the print from the plate."
                ),
                layer_start=top_start,
                layer_end=max(0, total_layers - 1),
                slicer_fix="Enable a 5–8 mm Outer Brim (brim_type = outer_only) and Z-hop.",
            )
        )
        recs["brim_type"] = "outer_only"
        recs["brim_width"] = 6

    if len(gloss_jump_layers) >= 5:
        sev = "warning" if prof.name in ("PLA-SILK", "PETG") else "info"
        findings.append(
            AdvisorFinding(
                code="outer_wall_gloss_banding",
                severity=sev,
                title=f"Outer-wall gloss/matte speed banding ({len(gloss_jump_layers)} sharp transitions)",
                detail=(
                    "Outer-wall speed shifts by >35% across adjacent layers, which creates visible "
                    "shiny-to-matte horizontal bands (especially on Silk PLA and PETG)."
                ),
                layer_start=gloss_jump_layers[0],
                layer_end=gloss_jump_layers[-1],
                slicer_fix="Enable 'Don't slow down outer walls' or run Print Advisor Enhance (Uniform Gloss).",
            )
        )
        recs["slow_down_for_layer_cooling_outer_wall"] = "false"

    if (
        printer_nozzle_mm is not None
        and 0.15 <= printer_nozzle_mm <= 1.2
        and abs(plate_nozzle_mm - printer_nozzle_mm) >= 0.05
    ):
        p_label = printer_name or "printer"
        findings.insert(
            0,
            AdvisorFinding(
                code="nozzle_mismatch",
                severity="critical",
                title=f"Nozzle mismatch: sliced for {plate_nozzle_mm:g}mm vs {printer_nozzle_mm:g}mm on {p_label}",
                detail=(
                    f"This plate expects a {plate_nozzle_mm:g}mm nozzle, while {p_label} is configured for "
                    f"{printer_nozzle_mm:g}mm (triggers Bambu [0500-803C] pause at start)."
                ),
                slicer_fix=f"Re-slice for a {printer_nozzle_mm:g}mm nozzle or update the nozzle setting on {p_label}.",
            ),
        )
        recs["nozzle_diameter"] = round(printer_nozzle_mm, 2)

    if ams_humidity_pct is not None and ams_humidity_pct > 35.0 and prof.hygroscopic:
        findings.append(
            AdvisorFinding(
                code="hygroscopic_moisture",
                severity="warning",
                title=f"Elevated AMS humidity ({ams_humidity_pct:.0f}%) for hygroscopic {prof.name}",
                detail=(
                    f"{prof.name} absorbs moisture rapidly above 35% RH, causing stringing, "
                    "popping steam bubbles, and weaker layer adhesion."
                ),
                slicer_fix="Run AMS filament drying before starting this print.",
            )
        )

    if _HELIO_HEADER_RE.search(gcode_text[:16384]):
        findings.append(
            AdvisorFinding(
                code="already_optimized",
                severity="info",
                title="Pre-optimized G-code detected (Helio Additive)",
                detail=(
                    "This plate already contains Helio Additive feedrate adjustments; "
                    "running Enhance again yields smaller incremental time changes."
                ),
            )
        )

    # Health score calculation (informational already_optimized notice does not penalize health)
    crit_count = sum(1 for f in findings if f.severity == "critical")
    warn_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info" and f.code != "already_optimized")
    hot_ratio = (len(hot_layers) / total_layers) if total_layers else 0.0
    cold_ratio = (len(cold_layers) / total_layers) if total_layers else 0.0
    penalty = crit_count * 18 + warn_count * 9 + info_count * 3 + min(20, int(hot_ratio * 35.0 + cold_ratio * 20.0))
    health_score = max(0, min(100, 100 - penalty))

    kin_time_s = max(1, int(round(t_now)))
    if slicer_prediction_s is not None and slicer_prediction_s > 0:
        est_time_s = slicer_prediction_s
        scaled_fast = fast_savings_s * (slicer_prediction_s / kin_time_s)
    else:
        est_time_s = kin_time_s
        scaled_fast = fast_savings_s
    est_savings_s = min(int(est_time_s * 0.35), max(0, int(round(scaled_fast))))

    return AssessResponse(
        plate_id=plate_id,
        material=prof.name,
        nozzle_diameter_mm=plate_nozzle_mm,
        health_score=health_score,
        tqi_mean=tqi_mean,
        tqi_hot_layers=len(hot_layers),
        tqi_cold_layers=len(cold_layers),
        estimated_time_s=est_time_s,
        estimated_fast_savings_s=est_savings_s,
        max_volumetric_flow_mm3s=round(max_flow_seen, 2),
        flow_limit_mm3s=round(prof.max_flow_mm3s, 2),
        findings=findings,
        layers=layers_out,
        slicer_recommendations=recs,
    )


def assess_3mf_plate(
    file_path: Path,
    plate_id: int | None = None,
    ams_humidity_pct: float | None = None,
    printer_nozzle_mm: float | None = None,
    printer_name: str | None = None,
) -> AssessResponse:
    """Assess a sliced ``.gcode.3mf`` or ``.gcode`` file on disk."""
    resolved_plate, gcode_text, settings_dict, _, slice_info_xml = _extract_plate_from_path(file_path, plate_id)
    slicer_pred_s, slice_nozzle_mm = _parse_plate_slice_info(slice_info_xml, resolved_plate)
    return assess_gcode_text(
        gcode_text,
        plate_id=resolved_plate,
        settings_dict=settings_dict,
        ams_humidity_pct=ams_humidity_pct,
        slice_nozzle_mm=slice_nozzle_mm,
        slicer_prediction_s=slicer_pred_s,
        printer_nozzle_mm=printer_nozzle_mm,
        printer_name=printer_name,
    )


def build_badge_summary(assess: AssessResponse) -> dict:
    """Compact JSON dict stored under ``file_metadata['print_advisor']``."""
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    top = max(assess.findings, key=lambda f: severity_rank.get(f.severity, 0), default=None)
    return PrintAdvisorBadgeSummary(
        health_score=assess.health_score,
        tqi_mean=assess.tqi_mean,
        estimated_fast_savings_s=assess.estimated_fast_savings_s,
        top_risk_code=top.code if top else None,
        top_risk_severity=top.severity if top else None,
        assessed_plate=assess.plate_id,
    ).model_dump()


def try_build_advisor_badge_summary(file_path: Path) -> dict | None:
    """Fail-open helper called inside ``slice_and_persist``."""
    try:
        res = assess_3mf_plate(file_path, plate_id=None)
        return build_badge_summary(res)
    except Exception as exc:
        logger.debug("Print advisor auto-assess skipped for %s: %s", file_path, exc)
        return None


@dataclass
class EnhanceArtifactResult:
    output_bytes: bytes
    plate_id: int
    before_assess: AssessResponse
    after_assess: AssessResponse
    changes_summary: list[str]


def _set_or_replace_f(cmd_part: str, new_f: float) -> str:
    f_token = f"F{int(round(new_f))}"
    if _F_PARAM_RE.search(cmd_part):
        return _F_PARAM_RE.sub(f_token, cmd_part, count=1)
    return f"{cmd_part} {f_token}"


def enhance_3mf_plate(
    source_path: Path,
    *,
    plate_id: int | None = None,
    priority: AdvisorPriority = "balanced",
    ams_humidity_pct: float | None = None,
    printer_nozzle_mm: float | None = None,
    printer_name: str | None = None,
) -> EnhanceArtifactResult:
    """Rewrite extruding ``G1``/``G2``/``G3`` feedrates and layer cooling inside a
    ``.gcode.3mf`` without altering ANY ``X/Y/Z/E`` toolpath geometry, recomputing
    ``.gcode.md5`` and updating ``M73`` / ``slice_info.config`` time predictions.
    """
    resolved_plate, gcode_text, settings_dict, target_member, slice_info_xml = _extract_plate_from_path(
        source_path, plate_id
    )
    if target_member is None:
        raise ValueError("Enhance requires a sliced .gcode.3mf package")

    slicer_pred_s, slice_nozzle_mm = _parse_plate_slice_info(slice_info_xml, resolved_plate)
    kin_before = assess_gcode_text(
        gcode_text,
        plate_id=resolved_plate,
        settings_dict=settings_dict,
        ams_humidity_pct=ams_humidity_pct,
        slice_nozzle_mm=slice_nozzle_mm,
        printer_nozzle_mm=printer_nozzle_mm,
        printer_name=printer_name,
    )
    config_sample = gcode_text[:16384] + "\n" + gcode_text[-16384:] if len(gcode_text) > 32768 else gcode_text
    resolved = _resolve_profile_from_settings(settings_dict, config_sample, slice_nozzle_mm=slice_nozzle_mm)
    prof = resolved.physics
    accel = resolved.default_accel_mms2

    layer_map = {lp.layer: lp for lp in kin_before.layers}
    has_slenderness_risk = any(f.code == "slenderness_detachment" for f in kin_before.findings)

    # Target uniform outer-wall speed for "gloss" mode (25th percentile across layers)
    ow_speeds = [
        lp.outer_wall_speed_mms for lp in kin_before.layers if lp.outer_wall_speed_mms is not None and lp.layer >= 1
    ]
    gloss_target_mms = float(np.percentile(ow_speeds, 25)) if ow_speeds else 60.0
    gloss_target_mms = max(35.0, min(120.0, gloss_target_mms))

    flow_cap_ratio = {
        "balanced": 0.88,
        "fast": 0.90,
        "strong": 0.80,
        "gloss": 0.86,
    }[priority]
    flow_cap_mm3s = prof.max_flow_mm3s * flow_cap_ratio

    has_start_marker = "; MACHINE_START_GCODE_END" in gcode_text
    in_print = not has_start_marker
    saw_explicit_layer = False
    rel_e = True
    x = y = z = e = 0.0
    has_pos = False
    orig_modal_f = 3000.0
    emitted_modal_f = 3000.0
    cur_feature = "wall"
    cur_layer_idx = 0
    layer_z = 0.2
    layer_has_extrusion = False

    scaled_moves = 0
    sped_up_layers: set[int] = set()
    cooled_layers: set[int] = set()
    warmed_layers: set[int] = set()

    out_lines: list[str] = []
    for raw in gcode_text.splitlines():
        s = raw.strip()
        if not s:
            out_lines.append(raw)
            continue

        if s[0] == ";":
            out_lines.append(raw)
            body = s[1:].strip()
            bu = body.upper()
            if bu.startswith("MACHINE_START_GCODE_END"):
                in_print = True
            elif bu.startswith("EXECUTABLE_BLOCK_END"):
                in_print = False
            elif bu in ("CHANGE_LAYER", "LAYER_CHANGE") or bu.startswith(("CHANGE_LAYER ", "LAYER_CHANGE ")):
                if in_print and saw_explicit_layer and layer_has_extrusion:
                    cur_layer_idx += 1
                    layer_has_extrusion = False
                saw_explicit_layer = True
                if in_print and cur_layer_idx >= 1:
                    lp = layer_map.get(cur_layer_idx)
                    if lp is not None:
                        if lp.tqi > 35.0:
                            out_lines.append(f"M106 P1 S{prof.max_fan_pwm} ; PrintAdvisor cool hot layer")
                            cooled_layers.add(cur_layer_idx)
                        elif priority == "strong" and lp.tqi < -35.0:
                            out_lines.append("M106 P1 S76 ; PrintAdvisor preserve interlayer bond")
                            warmed_layers.add(cur_layer_idx)
            elif ":" in body:
                k, v = body.split(":", 1)
                ku = k.strip().upper()
                vs = v.strip()
                if ku in ("FEATURE", "TYPE"):
                    cur_feature = vs.lower()
                elif ku in ("Z_HEIGHT", "Z"):
                    pz = _first_float(vs)
                    if pz is not None and pz >= 0:
                        layer_z = pz
            continue

        if ";" in raw:
            cmd_part, comment_part = raw.split(";", 1)
            comment_suffix = f" ;{comment_part}"
        else:
            cmd_part = raw
            comment_suffix = ""
        cmd_clean = cmd_part.strip()
        if not cmd_clean:
            out_lines.append(raw)
            continue

        parts = cmd_clean.split()
        op = parts[0].upper()

        if op == "M83":
            rel_e = True
            out_lines.append(raw)
            continue
        if op == "M82":
            rel_e = False
            out_lines.append(raw)
            continue
        if op == "G92":
            re_val = _read_axis(cmd_clean, "E")
            if re_val is not None:
                e = re_val
            out_lines.append(raw)
            continue

        if op not in ("G0", "G1", "G2", "G3"):
            if op == "M106" and in_print and priority in ("strong", "balanced"):
                lp_cur = layer_map.get(cur_layer_idx)
                if lp_cur is not None and lp_cur.tqi < -50.0:
                    p_idx = _read_axis(cmd_clean, "P")
                    s_val = _read_axis(cmd_clean, "S")
                    if s_val is not None and s_val > 102 and (p_idx is None or int(p_idx) in (0, 1)):
                        capped_cmd = re.sub(r"\bS\d+", "S102", cmd_clean, count=1)
                        out_lines.append(f"{capped_cmd}{comment_suffix}")
                        warmed_layers.add(cur_layer_idx)
                        continue
            out_lines.append(raw)
            continue

        f_val = _read_axis(cmd_clean, "F")
        if f_val is not None and f_val > 0.0:
            orig_modal_f = f_val

        nx = _read_axis(cmd_clean, "X")
        ny = _read_axis(cmd_clean, "Y")
        nz = _read_axis(cmd_clean, "Z")
        re_val = _read_axis(cmd_clean, "E")

        tx = x if nx is None else nx
        ty = y if ny is None else ny
        tz = z if nz is None else nz
        de = 0.0
        if re_val is not None:
            de = re_val if rel_e else (re_val - e)
            e = re_val

        if not has_pos:
            x, y, z = tx, ty, tz
            has_pos = True
            if f_val is not None:
                emitted_modal_f = f_val
            out_lines.append(raw)
            continue

        if op in ("G1", "G2", "G3"):
            dist, has_xy_motion, _, _ = _motion_geometry(op, cmd_clean, x, y, z, tx, ty, tz)
        else:
            dist = 0.0
            has_xy_motion = tx != x or ty != y

        is_xy_extrusion = in_print and op in ("G1", "G2", "G3") and de > 0.0 and has_xy_motion
        if is_xy_extrusion:
            if not saw_explicit_layer and layer_has_extrusion and abs(tz - layer_z) > 0.04:
                cur_layer_idx += 1
            layer_z = tz if tz > 0 else layer_z
            layer_has_extrusion = True

        # Do not alter travels or non-extruding moves —
        # restore orig_modal_f if a previous extruding move changed modal F!
        if not is_xy_extrusion:
            if f_val is None and abs(emitted_modal_f - orig_modal_f) > 0.5:
                restored = _set_or_replace_f(cmd_clean, orig_modal_f)
                emitted_modal_f = orig_modal_f
                out_lines.append(f"{restored}{comment_suffix}")
            else:
                if f_val is not None:
                    emitted_modal_f = orig_modal_f
                out_lines.append(raw)
            x, y, z = tx, ty, tz
            continue

        orig_speed_mms = orig_modal_f / 60.0
        lp = layer_map.get(cur_layer_idx)
        layer_tqi = lp.tqi if lp is not None else 0.0

        is_outer_wall = "outer wall" in cur_feature or cur_feature == "external perimeter"
        is_inner_wall = "inner wall" in cur_feature or cur_feature == "perimeter"
        is_infill = "infill" in cur_feature

        target_speed_mms = orig_speed_mms

        if cur_layer_idx >= 1:
            if priority == "gloss" and is_outer_wall:
                target_speed_mms = gloss_target_mms
            elif layer_tqi > 35.0:
                k_slow = max(0.50, 35.0 / max(layer_tqi, 36.0))
                target_speed_mms = max(15.0, orig_speed_mms * k_slow)
                cooled_layers.add(cur_layer_idx)
            elif layer_tqi < 10.0 and not has_slenderness_risk:
                k_fast = 1.0
                if priority == "balanced":
                    if is_infill:
                        k_fast = 1.25
                    elif is_inner_wall:
                        k_fast = 1.15
                elif priority == "fast":
                    if is_infill:
                        k_fast = 1.35
                    elif is_inner_wall:
                        k_fast = 1.22
                    elif is_outer_wall:
                        k_fast = 1.12
                elif priority == "gloss" and is_infill:
                    k_fast = 1.25
                if k_fast > 1.0:
                    target_speed_mms = min(300.0, orig_speed_mms * k_fast)
                    sped_up_layers.add(cur_layer_idx)

        # Enforce volumetric flow cap strictly on every extruding segment
        if dist > 1e-5 and de > 0.0:
            # Q = (de * A_f) / (dist / v) => v_max = (Q_cap * dist) / (de * A_f)
            v_flow_cap = (flow_cap_mm3s * dist) / (de * FILAMENT_AREA_MM2)
            if target_speed_mms > v_flow_cap:
                target_speed_mms = max(15.0, v_flow_cap)

        # Enforce slenderness upper-Z cap if tall/narrow without brim
        if has_slenderness_risk and cur_layer_idx > int(len(kin_before.layers) * 0.5):
            target_speed_mms = min(target_speed_mms, 90.0)

        new_f = max(900.0, min(18000.0, target_speed_mms * 60.0))
        if abs(new_f - orig_modal_f) > 12.0 or abs(emitted_modal_f - new_f) > 12.0:
            rewritten = _set_or_replace_f(cmd_clean, new_f)
            emitted_modal_f = round(new_f)
            if abs(new_f - orig_modal_f) > 12.0:
                scaled_moves += 1
            out_lines.append(f"{rewritten}{comment_suffix}")
        else:
            if f_val is not None:
                emitted_modal_f = orig_modal_f
            out_lines.append(raw)

        x, y, z = tx, ty, tz

    prelim_gcode = "\n".join(out_lines) + ("\n" if gcode_text.endswith("\n") else "")
    kin_after = assess_gcode_text(
        prelim_gcode,
        plate_id=resolved_plate,
        settings_dict=settings_dict,
        ams_humidity_pct=ams_humidity_pct,
        slice_nozzle_mm=slice_nozzle_mm,
        printer_nozzle_mm=printer_nozzle_mm,
        printer_name=printer_name,
    )

    # Pure kinematic ratio before/after so slicer cornering prediction scales proportionally
    # instead of jumping to raw stop-to-stop segment sum on dense 600k-chord meshes.
    time_ratio = kin_after.estimated_time_s / max(kin_before.estimated_time_s, 1)
    if slicer_pred_s is not None and slicer_pred_s > 0:
        anchored_before_s = slicer_pred_s
        anchored_after_s = max(1, int(round(slicer_pred_s * time_ratio)))
        scaled_after_savings = max(0, int(round(kin_after.estimated_fast_savings_s * (slicer_pred_s / max(kin_before.estimated_time_s, 1)))))
        before = kin_before.model_copy(
            update={
                "estimated_time_s": anchored_before_s,
                "estimated_fast_savings_s": max(0, int(round(kin_before.estimated_fast_savings_s * (slicer_pred_s / max(kin_before.estimated_time_s, 1))))),
            }
        )
        after = kin_after.model_copy(
            update={
                "estimated_time_s": anchored_after_s,
                "estimated_fast_savings_s": scaled_after_savings,
            }
        )
    else:
        before = kin_before
        after = kin_after

    # Rescale M73 R<remaining_minutes> markers proportionally
    final_gcode = _M73_R_RE.sub(
        lambda m: f"{m.group(1)}{max(0, int(round(int(m.group(2)) * time_ratio)))}",
        prelim_gcode,
    )

    gcode_bytes = final_gcode.encode("utf-8")
    md5_name = f"{target_member}.md5"
    md5_bytes = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")

    out_buf = io.BytesIO()
    with zipfile.ZipFile(source_path, "r") as zf_in, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        wrote_md5 = False
        for item in zf_in.namelist():
            info = zf_in.getinfo(item)
            if item == target_member:
                zf_out.writestr(info, gcode_bytes)
            elif item == md5_name:
                zf_out.writestr(info, md5_bytes)
                wrote_md5 = True
            elif item == _SLICE_INFO_PATH:
                raw_xml = zf_in.read(item).decode("utf-8", errors="ignore")
                updated_xml, _, _ = _scale_plate_prediction_xml(
                    raw_xml, resolved_plate, time_ratio, after.estimated_time_s
                )
                zf_out.writestr(info, updated_xml.encode("utf-8"))
            else:
                zf_out.writestr(info, zf_in.read(item))
        if not wrote_md5:
            zf_out.writestr(md5_name, md5_bytes)

    out_bytes = out_buf.getvalue()

    # Safety gate: if input carried Bambu start G-code, output must preserve it
    if not start_gcode_is_missing(source_path.read_bytes(), export_3mf=True):
        if start_gcode_is_missing(out_bytes, export_3mf=True):
            raise RuntimeError("Safety check failed: printer start G-code missing after optimization")

    summary: list[str] = [f"Adjusted feedrates on {scaled_moves:,} extruding segments ({priority} profile)"]
    if sped_up_layers:
        summary.append(f"Accelerated infill/walls across {len(sped_up_layers)} cool layer(s)")
    if cooled_layers:
        summary.append(f"Protected {len(cooled_layers)} hot layer(s) with slower speeds & boosted part fan")
    if warmed_layers:
        summary.append(f"Reduced part fan on {len(warmed_layers)} overcooled layer(s) for stronger Z-bonding")
    if priority == "gloss":
        summary.append(f"Locked outer-wall speed to {gloss_target_mms:.0f} mm/s for uniform surface sheen")

    return EnhanceArtifactResult(
        output_bytes=out_bytes,
        plate_id=resolved_plate,
        before_assess=before,
        after_assess=after,
        changes_summary=summary,
    )
