"""API routes for Bambuddy Print Advisor ("Assess & Enhance")."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.archives import ArchiveService, _ensure_archive_visible
from backend.app.api.routes.library import (
    _clean_3mf_metadata,
    _ensure_library_file_visible,
    _resolve_slice_destination,
    _stored_file_path,
    _without_print_name,
    get_library_thumbnails_dir,
    to_absolute_path,
    to_relative_path,
)
from backend.app.core.auth import (
    ApiKeyActor,
    RequestActor,
    RequestPrinterScope,
    require_ownership_permission,
    require_permission_if_auth_enabled,
)
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import PrinterScope
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.user import User
from backend.app.schemas.print_advisor import AssessResponse, EnhanceRequest, EnhanceResponse
from backend.app.services.archive import ThreeMFParser
from backend.app.services.print_advisor import (
    EnhanceArtifactResult,
    assess_3mf_plate,
    build_badge_summary,
    enhance_3mf_plate,
)
from backend.app.utils.ams_humidity import ams_humidity_percent
from backend.app.utils.filename import MAX_FILENAME_BYTES, safe_path_component

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print-advisor", tags=["print-advisor"])


def _live_printer_context(
    printer_id: int | None,
    printer_scope: PrinterScope | None = None,
) -> tuple[float | None, float | None, str | None]:
    """Best-effort ``(ams_humidity_pct, printer_nozzle_mm, printer_name)`` for a live printer.

    When ``printer_id`` is omitted on single-printer setups, automatically resolves the
    sole connected printer allowed by ``printer_scope``.
    """
    try:
        from backend.app.services.printer_manager import printer_manager

        target_pid = printer_id
        if target_pid is None:
            allowed_pids = [
                pid
                for pid in list(getattr(printer_manager, "_clients", {}).keys())
                if printer_scope is None or printer_scope.allows(pid)
            ]
            if len(allowed_pids) == 1:
                target_pid = allowed_pids[0]
        elif printer_scope is not None and not printer_scope.allows(target_pid):
            return None, None, None

        if target_pid is None:
            return None, None, None

        state = printer_manager.get_status(target_pid)
        if state is None:
            return None, None, None

        # 1. Max AMS humidity (%)
        raw = getattr(state, "raw_data", None) or {}
        ams_block = (raw.get("ams") or {}).get("ams") or getattr(state, "ams", None) or []
        vals: list[float] = []
        for unit in ams_block:
            unit_dict = unit if isinstance(unit, dict) else getattr(unit, "__dict__", {})
            pct = ams_humidity_percent(unit_dict)
            if pct is not None:
                vals.append(float(pct))
        humidity_pct = max(vals) if vals else None

        # 2. Active nozzle diameter (mm)
        nozzle_mm: float | None = None
        nozzles = getattr(state, "nozzles", None) or []
        if nozzles:
            active_idx = getattr(state, "active_extruder", 0) or 0
            picked_n = nozzles[active_idx] if 0 <= active_idx < len(nozzles) else nozzles[0]
            raw_diam = getattr(picked_n, "nozzle_diameter", None) or (
                picked_n.get("nozzle_diameter") if isinstance(picked_n, dict) else None
            )
            if raw_diam:
                try:
                    cand = float(str(raw_diam).strip())
                    if 0.15 <= cand <= 1.2:
                        nozzle_mm = cand
                except ValueError:
                    nozzle_mm = None

        info = getattr(printer_manager, "_printer_info", {}).get(target_pid)
        printer_name = getattr(info, "name", None) or f"Printer #{target_pid}"
        return humidity_pct, nozzle_mm, printer_name
    except Exception:
        return None, None, None


def _strip_3mf_extensions(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".gcode.3mf"):
        return filename[: -len(".gcode.3mf")]
    if lower.endswith(".3mf"):
        return filename[: -len(".3mf")]
    if lower.endswith(".gcode"):
        return filename[: -len(".gcode")]
    return filename


async def _persist_enhanced_library_file(
    db: AsyncSession,
    *,
    artifact: EnhanceArtifactResult,
    stem_source: str,
    priority: str,
    folder_id: int | None,
    owner_id: int | None,
    fallback_thumbnail: str | None,
    extra_meta: dict,
) -> LibraryFile:
    suffix = f"-enhanced-{priority}.gcode.3mf"
    safe_stem = safe_path_component(
        _strip_3mf_extensions(stem_source),
        fallback="print",
        max_bytes=MAX_FILENAME_BYTES - len(suffix.encode("utf-8")),
    )
    out_filename = f"{safe_stem}{suffix}"

    target_folder: LibraryFolder | None = None
    if folder_id is not None:
        folder_res = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
        target_folder = folder_res.scalar_one_or_none()

    out_path, out_is_external, external_fallback = _resolve_slice_destination(target_folder, out_filename)
    if out_is_external:
        out_filename = out_path.name
    await asyncio.to_thread(out_path.write_bytes, artifact.output_bytes)

    thumbnail_relative: str | None = fallback_thumbnail
    parsed_metadata: dict = {}
    try:

        def _parse_3mf() -> dict:
            return ThreeMFParser(str(out_path)).parse()

        parsed = await asyncio.to_thread(_parse_3mf)
        thumb_data = parsed.get("_thumbnail_data")
        thumb_ext = parsed.get("_thumbnail_ext", ".png")
        if thumb_data:
            thumb_path = get_library_thumbnails_dir() / f"{uuid.uuid4().hex}{thumb_ext}"
            await asyncio.to_thread(thumb_path.write_bytes, thumb_data)
            thumbnail_relative = to_relative_path(thumb_path)
        cleaned = _clean_3mf_metadata(parsed)
        if isinstance(cleaned, dict):
            parsed_metadata = cleaned
    except Exception as exc:
        logger.warning("Failed to parse enhanced 3MF metadata for %s: %s", out_filename, exc)

    metadata: dict = dict(_without_print_name(parsed_metadata) or {})
    metadata["print_time_seconds"] = artifact.after_assess.estimated_time_s
    metadata["print_advisor"] = build_badge_summary(artifact.after_assess)
    metadata["enhanced_priority"] = priority
    if external_fallback:
        metadata["external_write_fallback"] = external_fallback
    metadata.update(extra_meta)

    new_file = LibraryFile(
        folder_id=folder_id,
        is_external=out_is_external,
        filename=out_filename,
        file_path=_stored_file_path(out_path, out_is_external),
        file_type="gcode.3mf",
        file_size=len(artifact.output_bytes),
        file_hash=hashlib.sha256(artifact.output_bytes).hexdigest(),
        thumbnail_path=thumbnail_relative,
        file_metadata=metadata,
        source_type="sliced",
        created_by_id=owner_id,
    )
    db.add(new_file)
    await db.commit()
    return new_file


@router.post("/library/{file_id}/assess", response_model=AssessResponse)
async def assess_library_file(
    file_id: int,
    plate: int | None = Query(default=None, ge=1),
    printer_id: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db, scope="function"),
    auth_result: tuple[User | ApiKeyActor | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_READ_ALL,
            Permission.LIBRARY_READ_OWN,
        )
    ),
    printer_scope: PrinterScope = RequestPrinterScope,
) -> AssessResponse:
    """Run thermal TQI & failure-risk diagnostics on a sliced library file."""
    user, can_read_all = auth_result
    row = (await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))).scalar_one_or_none()
    lib_file = _ensure_library_file_visible(row, user, can_read_all)

    abs_path = to_absolute_path(lib_file.file_path)
    if not abs_path or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    humidity, printer_nozzle_mm, printer_name = _live_printer_context(printer_id, printer_scope)
    try:
        res = await asyncio.to_thread(
            assess_3mf_plate, abs_path, plate, humidity, printer_nozzle_mm, printer_name
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Cache compact badge summary on the library row for instant File Manager rendering
    meta = dict(lib_file.file_metadata or {})
    meta["print_advisor"] = build_badge_summary(res)
    lib_file.file_metadata = meta
    await db.commit()

    return res


@router.post("/archives/{archive_id}/assess", response_model=AssessResponse)
async def assess_archive(
    archive_id: int,
    plate: int | None = Query(default=None, ge=1),
    printer_id: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db, scope="function"),
    auth_result: tuple[User | ApiKeyActor | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_READ_ALL,
            Permission.ARCHIVES_READ_OWN,
        )
    ),
    printer_scope: PrinterScope = RequestPrinterScope,
) -> AssessResponse:
    """Run thermal TQI & failure-risk diagnostics on an archived print's 3MF."""
    user, can_read_all = auth_result
    service = ArchiveService(db)
    archive = await _ensure_archive_visible(await service.get_archive(archive_id), user, can_read_all, printer_scope)

    if not archive.file_path:
        raise HTTPException(status_code=404, detail="Archive has no stored 3MF file")
    file_path = Path(app_settings.base_dir) / archive.file_path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Archive 3MF file not found on disk")

    humidity, printer_nozzle_mm, printer_name = _live_printer_context(
        printer_id or archive.printer_id, printer_scope
    )
    target_plate = plate if plate is not None else archive.plate_id
    try:
        return await asyncio.to_thread(
            assess_3mf_plate, file_path, target_plate, humidity, printer_nozzle_mm, printer_name
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/library/{file_id}/enhance", response_model=EnhanceResponse)
async def enhance_library_file(
    file_id: int,
    request: EnhanceRequest,
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
    actor: User | ApiKeyActor | None = RequestActor,
    printer_scope: PrinterScope = RequestPrinterScope,
) -> EnhanceResponse:
    """Generate a non-destructive speed/cooling-optimized .gcode.3mf copy in Library."""
    can_read_all = actor is None or actor.has_permission(Permission.LIBRARY_READ_ALL.value)
    row = (await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))).scalar_one_or_none()
    lib_file = _ensure_library_file_visible(row, actor, can_read_all)

    abs_path = to_absolute_path(lib_file.file_path)
    if not abs_path or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="Source file not found on disk")

    humidity, printer_nozzle_mm, printer_name = _live_printer_context(None, printer_scope)
    try:
        artifact = await asyncio.to_thread(
            enhance_3mf_plate,
            abs_path,
            plate_id=request.plate,
            priority=request.priority,
            ams_humidity_pct=humidity,
            printer_nozzle_mm=printer_nozzle_mm,
            printer_name=printer_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    owner_id = current_user.id if current_user else (actor.id if isinstance(actor, User) else lib_file.created_by_id)
    folder_id = request.folder_id if request.folder_id is not None else lib_file.folder_id
    new_file = await _persist_enhanced_library_file(
        db,
        artifact=artifact,
        stem_source=lib_file.filename,
        priority=request.priority,
        folder_id=folder_id,
        owner_id=owner_id,
        fallback_thumbnail=lib_file.thumbnail_path,
        extra_meta={"enhanced_from_library_file_id": lib_file.id},
    )

    return EnhanceResponse(
        new_library_file_id=new_file.id,
        filename=new_file.filename,
        priority=request.priority,
        plate_id=artifact.plate_id,
        before_time_s=artifact.before_assess.estimated_time_s,
        after_time_s=artifact.after_assess.estimated_time_s,
        time_saved_s=artifact.before_assess.estimated_time_s - artifact.after_assess.estimated_time_s,
        before_health_score=artifact.before_assess.health_score,
        after_health_score=artifact.after_assess.health_score,
        changes_summary=artifact.changes_summary,
        assess=artifact.after_assess,
    )


@router.post("/archives/{archive_id}/enhance", response_model=EnhanceResponse)
async def enhance_archive(
    archive_id: int,
    request: EnhanceRequest,
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
    actor: User | ApiKeyActor | None = RequestActor,
    printer_scope: PrinterScope = RequestPrinterScope,
) -> EnhanceResponse:
    """Generate a non-destructive optimized .gcode.3mf Library file from an Archive."""
    if actor is not None and not (
        actor.has_permission(Permission.ARCHIVES_READ_ALL.value)
        or actor.has_permission(Permission.ARCHIVES_READ_OWN.value)
    ):
        raise HTTPException(status_code=403, detail="Missing archive read permission")
    can_read_all = actor is None or actor.has_permission(Permission.ARCHIVES_READ_ALL.value)
    service = ArchiveService(db)
    archive = await _ensure_archive_visible(await service.get_archive(archive_id), actor, can_read_all, printer_scope)

    if not archive.file_path:
        raise HTTPException(status_code=404, detail="Archive has no stored 3MF file")
    file_path = Path(app_settings.base_dir) / archive.file_path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Archive 3MF file not found on disk")

    humidity, printer_nozzle_mm, printer_name = _live_printer_context(archive.printer_id, printer_scope)
    target_plate = request.plate if request.plate is not None else archive.plate_id
    try:
        artifact = await asyncio.to_thread(
            enhance_3mf_plate,
            file_path,
            plate_id=target_plate,
            priority=request.priority,
            ams_humidity_pct=humidity,
            printer_nozzle_mm=printer_nozzle_mm,
            printer_name=printer_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    owner_id = current_user.id if current_user else (actor.id if isinstance(actor, User) else archive.created_by_id)
    stem = archive.print_name or archive.filename or f"archive-{archive.id}"
    new_file = await _persist_enhanced_library_file(
        db,
        artifact=artifact,
        stem_source=stem,
        priority=request.priority,
        folder_id=request.folder_id,
        owner_id=owner_id,
        fallback_thumbnail=None,
        extra_meta={"enhanced_from_archive_id": archive.id},
    )

    return EnhanceResponse(
        new_library_file_id=new_file.id,
        filename=new_file.filename,
        priority=request.priority,
        plate_id=artifact.plate_id,
        before_time_s=artifact.before_assess.estimated_time_s,
        after_time_s=artifact.after_assess.estimated_time_s,
        time_saved_s=artifact.before_assess.estimated_time_s - artifact.after_assess.estimated_time_s,
        before_health_score=artifact.before_assess.health_score,
        after_health_score=artifact.after_assess.health_score,
        changes_summary=artifact.changes_summary,
        assess=artifact.after_assess,
    )
