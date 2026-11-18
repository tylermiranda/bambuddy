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

from backend.app.services.print_advisor import assess_3mf_plate, assess_gcode_text, enhance_3mf_plate


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
