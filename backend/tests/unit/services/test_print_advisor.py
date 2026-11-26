"""Unit tests for Bambuddy Print Advisor physics & G-code optimizer."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import pytest

from backend.app.services.print_advisor import (
    _format_signed_tqi,
    assess_3mf_plate,
    assess_gcode_text,
    enhance_3mf_plate,
)


def _make_3mf(tmp_path: Path, gcode: str, settings: dict | None = None, name: str = "test.gcode.3mf") -> Path:
    cfg = settings or {
        "filament_type": ["PLA"],
        "nozzle_temperature": ["220"],
        "filament_max_volumetric_speed": ["21"],
        "default_acceleration": "6000",
        "machine_start_gcode": "M1002 gcode_claim_action : 2\nM620 S0A\n",
    }
    gcode_bytes = gcode.encode("utf-8")
    md5_bytes = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="1800"/></plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", md5_bytes)
    return p


def _extract_xyze(gcode: str) -> list[tuple[str, str | None, str | None, str | None, str | None]]:
    """Extract ordered (op, X, Y, Z, E) tuples from all motion commands."""
    coords: list[tuple[str, str | None, str | None, str | None, str | None]] = []
    for raw in gcode.splitlines():
        cmd = raw.split(";", 1)[0].strip()
        if not cmd:
            continue
        op = cmd.split()[0].upper()
        if op not in ("G0", "G1", "G2", "G3"):
            continue
        vals = []
        for axis in ("X", "Y", "Z", "E"):
            m = re.search(rf"\b{axis}([+-]?[0-9]*\.?[0-9]+)", cmd)
            vals.append(m.group(1) if m else None)
        coords.append((op, vals[0], vals[1], vals[2], vals[3]))
    return coords


@pytest.mark.unit
def test_hot_chimney_triggers_positive_tqi_and_overheating_finding() -> None:
    """Tiny fast-revisited spire layers with zero fan must flag overheating_curl."""
    lines = [
        "M83",
        "M106 P1 S0",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(8):
        z = round(0.2 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; LINE_WIDTH: 0.45",
                "; FEATURE: Outer wall",
                f"G0 X100 Y100 Z{z} F18000",
                "G1 X102 Y100 E0.08 F9000",
                "G1 X102 Y102 E0.08 F9000",
                "G1 X100 Y102 E0.08 F9000",
                "G1 X100 Y100 E0.08 F9000",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    res = assess_gcode_text("\n".join(lines))
    assert res.tqi_hot_layers >= 1
    assert any(lp.tqi > 45.0 for lp in res.layers[1:])
    assert any(f.code == "overheating_curl" for f in res.findings)


@pytest.mark.unit
def test_slow_overcooled_petg_triggers_cold_delamination_and_humidity_warning() -> None:
    """Very slow revisit times with 100% fan on PETG + wet AMS triggers cold & moisture warnings."""
    lines = [
        "M83",
        "M106 P1 S255",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(6):
        z = round(0.2 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; FEATURE: Sparse infill",
                f"G0 X10 Y10 Z{z} F12000",
                "G1 X210 Y10 E6.5 F600",
                "G1 X210 Y210 E6.5 F600",
                "G1 X10 Y210 E6.5 F600",
                "G1 X10 Y10 E6.5 F600",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    res = assess_gcode_text(
        "\n".join(lines),
        settings_dict={"filament_type": ["PETG"], "nozzle_temperature": ["250"]},
        ams_humidity_pct=48.0,
    )
    assert res.material == "PETG"
    assert res.tqi_cold_layers >= 1
    codes = {f.code for f in res.findings}
    assert "cold_delamination" in codes
    assert "hygroscopic_moisture" in codes
    assert res.estimated_fast_savings_s > 0


@pytest.mark.unit
def test_enhance_preserves_exact_xyze_coordinates_caps_flow_and_updates_md5(tmp_path: Path) -> None:
    """Enhance must never touch X/Y/Z/E, must cap excessive flow, and must write valid .gcode.md5."""
    lines = [
        "M1002 gcode_claim_action : 2",
        "M83",
        "M106 P1 S255",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(5):
        z = round(0.2 * (layer + 1), 2)
        lines.extend(
            [
                f"M73 P{layer * 20} R{50 - layer * 10}",
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; FEATURE: Outer wall",
                f"G0 X20 Y20 Z{z} F18000",
                # Excessive flow move (high E over 50mm at F18000 = 300 mm/s)
                "G1 X70 Y20 E4.8 F18000",
                "; FEATURE: Sparse infill",
                "G1 X70 Y170 E3.2 F1800",
                "G1 X20 Y170 E3.2",
                "G0 X20 Y20",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    src_gcode = "\n".join(lines) + "\n"
    src_3mf = _make_3mf(tmp_path, src_gcode)

    before = assess_3mf_plate(src_3mf)
    assert any(f.code == "volumetric_flow_ceiling" for f in before.findings)

    enhanced = enhance_3mf_plate(src_3mf, priority="balanced")
    with zipfile.ZipFile(io.BytesIO(enhanced.output_bytes), "r") as zf:
        new_gcode_bytes = zf.read("Metadata/plate_1.gcode")
        new_md5 = zf.read("Metadata/plate_1.gcode.md5").decode("ascii")

    expected_md5 = hashlib.md5(new_gcode_bytes, usedforsecurity=False).hexdigest().upper()
    assert new_md5 == expected_md5

    new_gcode = new_gcode_bytes.decode("utf-8")
    assert _extract_xyze(src_gcode) == _extract_xyze(new_gcode)
    assert enhanced.after_assess.max_volumetric_flow_mm3s <= before.flow_limit_mm3s


@pytest.mark.unit
def test_normal_pla_long_layers_stay_in_ideal_tqi_without_false_cold_alarm() -> None:
    """Open-air PLA layers cooled to ~30C ambient with 100% fan should be healthy, not delaminating."""
    lines = [
        "M83",
        "M106 P1 S255",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(6):
        z = round(0.2 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; FEATURE: Outer wall",
                f"G0 X20 Y20 Z{z} F18000",
                "G1 X220 Y20 E6.5 F900",
                "G1 X220 Y220 E6.5 F900",
                "; FEATURE: Sparse infill",
                "G1 X20 Y220 E6.5 F900",
                "G1 X20 Y20 E6.5 F900",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    res = assess_gcode_text("\n".join(lines), settings_dict={"filament_type": ["PLA"]})
    assert res.tqi_cold_layers == 0
    assert res.tqi_hot_layers == 0
    assert res.health_score >= 90
    assert -25.0 <= res.tqi_mean <= 25.0


@pytest.mark.unit
def test_nozzle_mismatch_helio_header_and_proportional_prediction_scaling(tmp_path: Path) -> None:
    """0.2mm plate vs 0.4mm printer flags nozzle_mismatch; Enhance scales slice_info prediction proportionally."""
    lines = [
        ";helioadditive=(optimization.id=d458e1cc-23e6-4905-85f3-b6fcac974956)",
        "M1002 gcode_claim_action : 2",
        "M83",
        "M106 P1 S255",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(4):
        z = round(0.1 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; FEATURE: Sparse infill",
                f"G0 X20 Y20 Z{z} F18000",
                "G1 X120 Y20 E1.2 F1200",
                "G1 X120 Y120 E1.2 F1200",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    gcode_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    md5_bytes = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")

    p = tmp_path / "ring_02mm.gcode.3mf"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"filament_type": ["PLA"], "filament_max_volumetric_speed": ["2.0"]}),
        )
        zf.writestr(
            "Metadata/slice_info.config",
            (
                '<config><plate><metadata key="index" value="1"/>'
                '<metadata key="nozzle_diameters" value="0.2"/>'
                '<metadata key="prediction" value="11160"/></plate></config>'
            ),
        )
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", md5_bytes)

    assessed = assess_3mf_plate(p, printer_nozzle_mm=0.4, printer_name="P2S")
    assert assessed.nozzle_diameter_mm == 0.2
    assert assessed.estimated_time_s == 11160
    codes = [f.code for f in assessed.findings]
    assert codes[0] == "nozzle_mismatch"
    assert "already_optimized" in codes

    enhanced = enhance_3mf_plate(p, priority="fast", printer_nozzle_mm=0.4, printer_name="P2S")
    assert enhanced.before_assess.estimated_time_s == 11160
    assert enhanced.after_assess.estimated_time_s < 11160
    with zipfile.ZipFile(io.BytesIO(enhanced.output_bytes), "r") as zf:
        xml_out = zf.read("Metadata/slice_info.config").decode("utf-8")
    m = re.search(r'key="prediction"\s+value="(\d+)"', xml_out)
    assert m is not None
    assert int(m.group(1)) == enhanced.after_assess.estimated_time_s


@pytest.mark.unit
def test_assess_performance_under_two_seconds() -> None:
    """50k-line plate must assess well under the 1.5s budget."""
    chunk = [
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 1.0",
        "; FEATURE: Sparse infill",
    ] + [f"G1 X{20 + (i % 100)} Y{20 + ((i * 3) % 100)} E0.04 F9000" for i in range(500)]
    gcode = "M83\n; MACHINE_START_GCODE_END\n" + "\n".join(chunk * 80) + "\n; EXECUTABLE_BLOCK_END\n"
    t0 = time.perf_counter()
    res = assess_gcode_text(gcode)
    elapsed = time.perf_counter() - t0
    assert len(res.layers) == 80
    assert elapsed < 2.0


@pytest.mark.unit
def test_g2_g3_circular_arcs_use_true_arc_length_not_chord_in_assess_and_enhance(tmp_path: Path) -> None:
    """Near-closed G3 tree-support arcs and full 360-deg G2 circles must use true arc length (R * dtheta),
    never straight-line chord distance, preventing false >100 mm3/s flow spikes and F900 throttling.
    """
    lines = [
        "M1002 gcode_claim_action : 2",
        "M83",
        "M106 P1 S255",
        "; MACHINE_START_GCODE_END",
    ]
    for layer in range(6):
        z = round(0.16 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                "; FEATURE: Outer wall",
                f"G0 X30 Y30 Z{z} F18000",
                "G1 X190 Y30 E4.45 F3600",
                "G1 X190 Y190 E4.45 F3600",
                "G1 X30 Y190 E4.45 F3600",
                "G1 X30 Y30 E4.45 F3600",
                "; FEATURE: Support",
                # Real Bambu Studio 356-deg CCW tree-support arc: chord = 0.057mm, true arc = 5.59mm (~9.3 mm3/s)
                f"G0 X205.768 Y181.918 Z{z} F18000",
                "G3 X205.711 Y181.91 I-0.075 J0.887 E0.14347 F9000",
                # Full 360-deg CW G2 circle where start XY == end XY: chord = 0.0mm, true arc = 2*pi*1.5 = 9.425mm
                f"G0 X160.0 Y160.0 Z{z} F18000",
                "G2 X160.0 Y160.0 I1.5 J0.0 E0.24 F9000",
            ]
        )
    lines.append("; EXECUTABLE_BLOCK_END")
    src_gcode = "\n".join(lines) + "\n"
    src_3mf = _make_3mf(
        tmp_path,
        src_gcode,
        settings={
            "filament_type": ["PLA"],
            "nozzle_temperature": ["220"],
            "filament_max_volumetric_speed": ["22"],
            "default_acceleration": "10000",
            "machine_start_gcode": "M1002 gcode_claim_action : 2\nM620 S0A\n",
        },
    )

    assessed = assess_3mf_plate(src_3mf)
    assert assessed.max_volumetric_flow_mm3s < 12.0, (
        f"Expected < 12 mm3/s true flow, got {assessed.max_volumetric_flow_mm3s}"
    )
    assert not any(f.code == "volumetric_flow_ceiling" for f in assessed.findings)
    assert assessed.health_score == 100

    enhanced = enhance_3mf_plate(src_3mf, priority="balanced")
    with zipfile.ZipFile(io.BytesIO(enhanced.output_bytes), "r") as zf:
        new_gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")
    assert _extract_xyze(src_gcode) == _extract_xyze(new_gcode)
    # Neither the 356-deg G3 arc nor the 360-deg G2 circle should be throttled down to F900!
    arc_lines = [ln for ln in new_gcode.splitlines() if ln.startswith(("G2 ", "G3 "))]
    assert len(arc_lines) == 12
    assert not any(re.search(r"\bF900\b", ln) for ln in arc_lines), (
        f"G2/G3 arcs were erroneously throttled: {arc_lines[:3]}"
    )
    assert all(re.search(r"\bF9000\b", ln) for ln in arc_lines), (
        f"Expected F9000 preserved on G2/G3 arcs: {arc_lines[:3]}"
    )
    assert enhanced.after_assess.health_score == 100


@pytest.mark.unit
def test_dual_material_petg_pla_per_slot_physics_flow_humidity_and_interfaces() -> None:
    """Stacked PETG (T0) + PLA (T1) plates must evaluate each extruded slot with its own
    material physics, volumetric flow limit, and close_fan_the_first_x_layers, avoid false
    cold_delamination across dissimilar release interfaces, and detect hygroscopic slots in any order.
    """
    settings = {
        "filament_type": ["PETG", "PLA"],
        "filament_settings_id": ["Bambu PETG Basic @BBL P2S 0.4 nozzle", "Bambu PLA Basic @BBL P2S"],
        "nozzle_temperature": ["250", "220"],
        "filament_max_volumetric_speed": ["15", "21"],
        "close_fan_the_first_x_layers": ["3", "1"],
        "default_acceleration": "6000",
    }
    lines = [
        "M83",
        "M620 M ;enable remap",
        "M620.10 A0 F374.174 H0.4 T270 P245 S1",
        "M620.10 A1 F523.843 H0.4 T240 P220 S1",
        "M620 S0A",
        "T0",
        "M106 P1 S0",
        "; MACHINE_START_GCODE_END",
    ]

    # Layers 0..2 (1-based 1..3): T0 PETG with intentional 0% fan + Bridge on Layer 3 (layer_idx=2)
    for layer in range(3):
        z = round(0.2 * (layer + 1), 2)
        feat = "Bridge" if layer == 2 else "Outer wall"
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                f"; FEATURE: {feat}",
                f"G0 X30 Y30 Z{z} F18000",
                "G1 X210 Y30 E5.8 F1200",
                "G1 X210 Y210 E5.8 F1200",
                "G1 X30 Y210 E5.8 F1200",
                "G1 X30 Y30 E5.8 F1200",
            ]
        )

    # Layer 3: T0 PETG normal 35% fan (full perimeter loop for realistic layer time)
    lines.extend(
        [
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.8",
            "M106 P1 S90",
            "; FEATURE: Sparse infill",
            "G0 X30 Y30 Z0.8 F18000",
            "G1 X210 Y30 E5.8 F1200",
            "G1 X210 Y210 E5.8 F1200",
            "G1 X30 Y210 E5.8 F1200",
            "G1 X30 Y30 E5.8 F1200",
        ]
    )

    # Layers 4..5: T1 PLA release interface + PLA bulk at 100% fan and ~14.3 mm³/s Gap infill
    # (68% of PLA's 21 mm³/s limit, but >92% of PETG's 15 mm³/s limit if misclassified!)
    for layer in (4, 5):
        z = round(0.2 * (layer + 1), 2)
        lines.extend(
            [
                "; CHANGE_LAYER",
                f"; Z_HEIGHT: {z}",
                *(["M620 S1A", "T1", "M106 P1 S255"] if layer == 4 else []),
                "; FEATURE: Gap infill",
                f"G0 X30 Y30 Z{z} F18000",
                # 180mm at F10800 (180 mm/s), dt ~ 1.03s, E=6.12 => ~14.3 mm³/s
                "G1 X210 Y30 E6.12 F10800",
                "; FEATURE: Sparse infill",
                "G1 X210 Y210 E5.8 F900",
                "G1 X30 Y210 E5.8 F900",
                "G1 X30 Y30 E5.8 F900",
            ]
        )

    # Layer 6: T0 PETG printed directly over T1 PLA release layer (even with high bridge fan on release)
    lines.extend(
        [
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 1.4",
            "M620 S0A",
            "T0",
            "M106 P1 S255",
            "; FEATURE: Outer wall",
            "G0 X30 Y30 Z1.4 F18000",
            "G1 X210 Y30 E5.8 F900",
            "G1 X210 Y210 E5.8 F900",
            "M620 S65535",
            "T65535",
            "; EXECUTABLE_BLOCK_END",
        ]
    )

    res = assess_gcode_text("\n".join(lines), settings_dict=settings, ams_humidity_pct=43.0)
    assert res.material == "PETG + PLA"
    codes = [f.code for f in res.findings]
    assert codes == ["hygroscopic_moisture"], f"Expected only hygroscopic_moisture, got {res.findings}"
    assert res.tqi_hot_layers == 0
    assert res.tqi_cold_layers == 0
    assert res.health_score >= 90

    # Reverse order: PLA (T0) first + PETG (T1) second must STILL warn on high AMS humidity when T1 extrudes
    pla_first_settings = {
        "filament_type": ["PLA", "PETG"],
        "nozzle_temperature": ["220", "250"],
        "filament_max_volumetric_speed": ["21", "15"],
    }
    pla_then_petg_gcode = "\n".join(
        [
            "M83",
            "M620 S0A",
            "T0",
            "; MACHINE_START_GCODE_END",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.2",
            "G0 X30 Y30 Z0.2 F12000",
            "G1 X120 Y30 E3.0 F2400",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.4",
            "M620 S1A",
            "T1",
            "G0 X30 Y30 Z0.4 F12000",
            "G1 X120 Y30 E3.0 F2400",
            "; EXECUTABLE_BLOCK_END",
        ]
    )
    res_pla_petg = assess_gcode_text(pla_then_petg_gcode, settings_dict=pla_first_settings, ams_humidity_pct=43.0)
    assert res_pla_petg.material == "PLA + PETG"
    assert any(f.code == "hygroscopic_moisture" for f in res_pla_petg.findings)

    # Unused hygroscopic slot (Slot 1 PETG never extruded) must NOT trigger hygroscopic_moisture
    pla_only_gcode = "\n".join(
        [
            "M83",
            "M620 S0A",
            "T0",
            "; MACHINE_START_GCODE_END",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.2",
            "G0 X30 Y30 Z0.2 F12000",
            "G1 X120 Y30 E3.0 F2400",
            "; EXECUTABLE_BLOCK_END",
        ]
    )
    res_pla_only = assess_gcode_text(pla_only_gcode, settings_dict=pla_first_settings, ams_humidity_pct=43.0)
    assert res_pla_only.material == "PLA"
    assert not any(f.code == "hygroscopic_moisture" for f in res_pla_only.findings)


@pytest.mark.unit
def test_enhance_multi_material_preserves_pla_fan_interface_speeds_and_per_slot_flow_caps(tmp_path: Path) -> None:
    """Enhance on PETG+PLA plates must preserve M106 S255 on PLA layers, leave dissimilar-material
    interface speeds/cooling untouched, and enforce per-slot volumetric flow caps independently.
    """
    settings = {
        "filament_type": ["PETG", "PLA"],
        "nozzle_temperature": ["250", "220"],
        "filament_max_volumetric_speed": ["15", "21"],
        "close_fan_the_first_x_layers": ["3", "1"],
        "default_acceleration": "10000",
        "machine_start_gcode": "M1002 gcode_claim_action : 2\nM620 S0A\n",
    }
    lines = [
        "M1002 gcode_claim_action : 2",
        "M83",
        "M620 S0A",
        "T0",
        "M106 P1 S80",
        "; MACHINE_START_GCODE_END",
        # Layer 0: T0 PETG base
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 0.2",
        "; FEATURE: Outer wall",
        "G0 X20 Y20 Z0.2 F18000",
        "G1 X200 Y20 E5.8 F2400",
        # Layer 1: T0 PETG bulk with an excessive >20 mm³/s PETG move that SHOULD be capped to 13.2 mm³/s
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 0.4",
        "; FEATURE: Outer wall",
        "G0 X20 Y20 Z0.4 F18000",
        "G1 X120 Y20 E10.0 F12000 ; petg_high_flow",
        "G1 X200 Y20 E2.6 F2400",
        # Layer 2: T1 PLA printed directly on Layer 1 PETG (dissimilar interface: keep F1200 infill & S255 untouched!)
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 0.6",
        "M620 S1A",
        "T1",
        "M106 P1 S255 ; pla_interface_fan",
        "; FEATURE: Sparse infill",
        "G0 X20 Y20 Z0.6 F18000",
        "G1 X200 Y20 E5.8 F1200 ; pla_interface_infill",
        "G1 X200 Y200 E5.8 F1200",
        "G1 X20 Y200 E5.8 F1200",
        "G1 X20 Y20 E5.8 F1200",
        # Layer 3: T1 PLA on Layer 2 PLA (100% fan preserved; ~14.7 mm³/s PLA wall preserved; ~32 mm³/s PLA move capped!)
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 0.8",
        "M106 P1 S255 ; pla_bulk_fan",
        "; FEATURE: Outer wall",
        "G0 X20 Y20 Z0.8 F18000",
        "G1 X200 Y20 E6.10 F10800 ; pla_mid_flow_14mm3s",
        "G1 X200 Y120 E13.5 F12000 ; pla_excess_flow_30mm3s",
        "G1 X20 Y120 E5.8 F1200",
        "G1 X20 Y20 E3.2 F1200",
        # Layer 4: T0 PETG printed back over Layer 3 PLA (dissimilar interface: keep F3000 & S200 untouched!)
        "; CHANGE_LAYER",
        "; Z_HEIGHT: 1.0",
        "M620 S0A",
        "T0",
        "M106 P1 S200 ; petg_release_bridge_fan",
        "; FEATURE: Sparse infill",
        "G0 X20 Y20 Z1.0 F18000",
        "G1 X200 Y20 E5.8 F3000 ; petg_interface_infill",
        "; EXECUTABLE_BLOCK_END",
    ]
    src_gcode = "\n".join(lines) + "\n"
    src_3mf = _make_3mf(tmp_path, src_gcode, settings=settings, name="dual_petg_pla.gcode.3mf")

    for mode in ("balanced", "strong"):
        enhanced = enhance_3mf_plate(src_3mf, priority=mode)
        with zipfile.ZipFile(io.BytesIO(enhanced.output_bytes), "r") as zf:
            out_gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

        assert _extract_xyze(src_gcode) == _extract_xyze(out_gcode)
        # PLA 100% fan commands and PETG release-interface fan command must NEVER be capped to S102/S76
        assert "M106 P1 S255 ; pla_interface_fan" in out_gcode
        assert "M106 P1 S255 ; pla_bulk_fan" in out_gcode
        assert "M106 P1 S200 ; petg_release_bridge_fan" in out_gcode

        by_tag = {
            ln.split(";", 1)[1].strip(): ln.split(";", 1)[0].strip()
            for ln in out_gcode.splitlines()
            if ";" in ln and not ln.lstrip().startswith(";")
        }
        # Interface infill moves must NOT be accelerated
        assert re.search(r"\bF1200\b", by_tag["pla_interface_infill"])
        assert re.search(r"\bF3000\b", by_tag["petg_interface_infill"])
        # PLA ~14.7 mm³/s Outer wall exceeds PETG's cap (12.0/13.2) but is under PLA's cap (16.8/18.48), so F10800 stays intact!
        assert re.search(r"\bF10800\b", by_tag["pla_mid_flow_14mm3s"])
        # PETG >20 mm³/s move and PLA ~30 mm³/s move must each be capped below F12000
        m_petg = re.search(r"\bF(\d+)\b", by_tag["petg_high_flow"])
        m_pla = re.search(r"\bF(\d+)\b", by_tag["pla_excess_flow_30mm3s"])
        assert m_petg is not None and int(m_petg.group(1)) < 3500
        # Under PLA's cap (16.8 strong / 18.48 balanced), F is ~3104 / ~3415 (vs ~2217 / ~2439 if capped as PETG)
        assert m_pla is not None and 3000 < int(m_pla.group(1)) < 4000


@pytest.mark.unit
def test_close_fan_first_x_layers_and_tqi_formatting_without_plus_minus() -> None:
    """Overhang/bridge fan checks must respect close_fan_the_first_x_layers, emit low_overhang_fan
    instead of overheating_curl on cool layers, and never format negative TQI with '+-'.
    """
    assert _format_signed_tqi(62.4) == "+62"
    assert _format_signed_tqi(0.0) == "0"
    assert _format_signed_tqi(-0.2) == "0"
    assert _format_signed_tqi(-43.3) == "-43"

    def _build_overhang_gcode(num_layers: int) -> str:
        out = ["M83", "M106 P1 S0", "; MACHINE_START_GCODE_END"]
        for li in range(num_layers):
            z = round(0.2 * (li + 1), 2)
            out.extend(
                [
                    "; CHANGE_LAYER",
                    f"; Z_HEIGHT: {z}",
                    "; FEATURE: Bridge",
                    f"G0 X20 Y20 Z{z} F18000",
                    "G1 X220 Y20 E6.4 F900",
                    "G1 X220 Y220 E6.4 F900",
                    "G1 X20 Y220 E6.4 F900",
                    "G1 X20 Y20 E6.4 F900",
                ]
            )
        out.append("; EXECUTABLE_BLOCK_END")
        return "\n".join(out)

    # 3 layers (indices 0, 1, 2) with close_fan_the_first_x_layers = 3 => 0 warnings
    res_3 = assess_gcode_text(
        _build_overhang_gcode(3),
        settings_dict={"filament_type": ["PETG"], "close_fan_the_first_x_layers": ["3"]},
    )
    assert res_3.findings == []
    assert res_3.tqi_hot_layers == 0

    # 5 layers (indices 0..4) with 0% fan on Bridge after close_fan_the_first_x_layers = 3 => low_overhang_fan on L3..4
    res_5 = assess_gcode_text(
        _build_overhang_gcode(5),
        settings_dict={"filament_type": ["PETG"], "close_fan_the_first_x_layers": ["3"]},
    )
    codes = [f.code for f in res_5.findings]
    assert "low_overhang_fan" in codes
    assert "overheating_curl" not in codes
    assert res_5.tqi_hot_layers == 0
    low_f = next(f for f in res_5.findings if f.code == "low_overhang_fan")
    assert low_f.layer_start == 3
    assert low_f.layer_end == 4
    for f in res_5.findings:
        assert "+-" not in f.title
        assert "+-" not in f.detail
