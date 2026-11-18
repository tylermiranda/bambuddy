"""Integration tests for /api/v1/print-advisor/* routes."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile


def _build_sliced_3mf_bytes() -> bytes:
    gcode = "\n".join(
        [
            "M1002 gcode_claim_action : 2",
            "M83",
            "M106 P1 S255",
            "; MACHINE_START_GCODE_END",
            "M73 P0 R20",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.2",
            "; FEATURE: Outer wall",
            "G0 X30 Y30 Z0.2 F18000",
            "G1 X90 Y30 E1.2 F3600",
            "G1 X90 Y90 E1.2 F3600",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.4",
            "; FEATURE: Sparse infill",
            "G0 X30 Y30 Z0.4 F18000",
            "G1 X180 Y30 E4.0 F1200",
            "G1 X180 Y180 E4.0 F1200",
            "G1 X30 Y180 E4.0 F1200",
            "; CHANGE_LAYER",
            "; Z_HEIGHT: 0.6",
            "; FEATURE: Sparse infill",
            "G0 X30 Y30 Z0.6 F18000",
            "G1 X180 Y30 E4.0 F1200",
            "G1 X180 Y180 E4.0 F1200",
            "G1 X30 Y180 E4.0 F1200",
            "; EXECUTABLE_BLOCK_END",
            "",
        ]
    )
    gcode_bytes = gcode.encode("utf-8")
    md5_bytes = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")
    cfg = {
        "filament_type": ["PLA"],
        "nozzle_temperature": ["220"],
        "filament_max_volumetric_speed": ["21"],
        "default_acceleration": "6000",
        "machine_start_gcode": "M1002 gcode_claim_action : 2\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="1200"/></plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", md5_bytes)
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_assess_and_enhance_library_file(async_client: AsyncClient, db_session) -> None:
    content = _build_sliced_3mf_bytes()
    lib_dir = Path(app_settings.base_dir) / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    disk_path = lib_dir / "advisor_fixture.gcode.3mf"
    disk_path.write_bytes(content)

    row = LibraryFile(
        folder_id=None,
        is_external=False,
        filename="advisor_fixture.gcode.3mf",
        file_path="library/advisor_fixture.gcode.3mf",
        file_type="gcode.3mf",
        file_size=len(content),
        file_hash=hashlib.sha256(content).hexdigest(),
        file_metadata={"sliced_for_model": "P2S"},
        source_type="sliced",
    )
    db_session.add(row)
    await db_session.commit()

    # 1. Assess library file and verify cached badge projection on GET /library/files
    assess_resp = await async_client.post(f"/api/v1/print-advisor/library/{row.id}/assess")
    assert assess_resp.status_code == 200, assess_resp.text
    assess_body = assess_resp.json()
    assert assess_body["plate_id"] == 1
    assert assess_body["material"] == "PLA"
    assert len(assess_body["layers"]) == 3
    assert 0 <= assess_body["health_score"] <= 100

    list_resp = await async_client.get("/api/v1/library/files")
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json() if item["id"] == row.id)
    assert listed["print_advisor"] is not None
    assert listed["print_advisor"]["health_score"] == assess_body["health_score"]

    # 2. Enhance library file (fast mode) and verify non-destructive new LibraryFile + valid .gcode.md5
    enhance_resp = await async_client.post(
        f"/api/v1/print-advisor/library/{row.id}/enhance",
        json={"priority": "fast"},
    )
    assert enhance_resp.status_code == 200, enhance_resp.text
    enh = enhance_resp.json()
    assert enh["new_library_file_id"] != row.id
    assert enh["filename"].endswith("-enhanced-fast.gcode.3mf")
    assert enh["after_time_s"] <= enh["before_time_s"]

    # Verify the new library file serves valid G-code
    gcode_resp = await async_client.get(f"/api/v1/library/files/{enh['new_library_file_id']}/gcode")
    assert gcode_resp.status_code == 200
    assert "EXECUTABLE_BLOCK_END" in gcode_resp.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_assess_and_enhance_archive(async_client: AsyncClient, archive_factory, printer_factory) -> None:
    printer = await printer_factory()
    content = _build_sliced_3mf_bytes()
    arch_dir = Path(app_settings.base_dir) / "archive" / "advisor_test"
    arch_dir.mkdir(parents=True, exist_ok=True)
    (arch_dir / "benchy.gcode.3mf").write_bytes(content)

    archive = await archive_factory(
        printer.id,
        print_name="Advisor Benchy",
        file_path="archive/advisor_test/benchy.gcode.3mf",
    )

    assess_resp = await async_client.post(f"/api/v1/print-advisor/archives/{archive.id}/assess")
    assert assess_resp.status_code == 200, assess_resp.text
    assert assess_resp.json()["material"] == "PLA"

    enhance_resp = await async_client.post(
        f"/api/v1/print-advisor/archives/{archive.id}/enhance",
        json={"priority": "gloss"},
    )
    assert enhance_resp.status_code == 200, enhance_resp.text
    assert enhance_resp.json()["filename"].endswith("-enhanced-gloss.gcode.3mf")
