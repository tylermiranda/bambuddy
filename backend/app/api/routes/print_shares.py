"""Print Share Links API (#3461)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting
from backend.app.core.auth import (
    RequirePermissionIfAuthEnabled,
    get_current_user_optional,
    is_auth_enabled,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import print_shares as share_service
from backend.app.services.printer_manager import (
    display_temperatures,
    get_derived_status_name,
    printer_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print-shares", tags=["print-shares"])


class PrintShareCreate(BaseModel):
    printer_id: int
    title: str | None = Field(None, max_length=200)
    project_url: str | None = Field(None, max_length=500)
    end_with_print: bool = True
    # Hours until hard expiry; omit / null = never (OE Live Link "Never").
    expires_in_hours: int | None = Field(None, ge=1, le=17520)

    @field_validator("project_url")
    @classmethod
    def validate_project_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("project_url must be an http(s) URL")
        return v


def _require_user_when_auth(user: User | None, auth_on: bool) -> User | None:
    if auth_on and user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


@router.post("")
@router.post("/")
async def create_share(
    body: PrintShareCreate,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    _=RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Create a guest share link for the printer's current print."""
    auth_on = await is_auth_enabled(db)
    user = _require_user_when_auth(user, auth_on)

    result = await db.execute(select(Printer).where(Printer.id == body.printer_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(404, "Printer not found")

    try:
        share = await share_service.create_print_share(
            db,
            printer_id=body.printer_id,
            created_by_id=user.id if user else None,
            title=body.title,
            project_url=body.project_url,
            end_with_print=body.end_with_print,
            expires_in_hours=body.expires_in_hours,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    external = await get_setting(db, "external_url")
    return share_service.share_to_owner_dict(share, external_url=external or None)


@router.get("")
@router.get("/")
async def list_shares(
    include_ended: bool = True,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    _=RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    auth_on = await is_auth_enabled(db)
    user = _require_user_when_auth(user, auth_on)
    shares = await share_service.list_shares_for_user(
        db,
        user_id=user.id if user else None,
        include_ended=include_ended,
    )
    external = await get_setting(db, "external_url")
    return [share_service.share_to_owner_dict(s, external_url=external or None) for s in shares]


@router.delete("/{public_id}")
async def revoke_share(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    _=RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    auth_on = await is_auth_enabled(db)
    user = _require_user_when_auth(user, auth_on)

    share = await share_service.get_share_by_public_id(db, public_id)
    if share is None:
        raise HTTPException(404, "Share not found")
    if user is not None and share.created_by_id not in (None, user.id) and not user.is_admin:
        raise HTTPException(403, "Not allowed to revoke this share")

    await share_service.end_share(db, share, reason="revoked")
    external = await get_setting(db, "external_url")
    return share_service.share_to_owner_dict(share, external_url=external or None)


@router.get("/{public_id}/public")
async def get_public_share(public_id: str, db: AsyncSession = Depends(get_db)):
    """Guest status payload — no login. Safe fields only."""
    share = await share_service.get_share_by_public_id(db, public_id)
    if share is None:
        raise HTTPException(404, "Share not found")

    # Re-check liveness for active shares (may end the row on expiry / job end).
    if share.status == "active":
        live = await share_service.verify_active_share_token(db, public_id, printer_id=share.printer_id)
        if live is None:
            share = await share_service.get_share_by_public_id(db, public_id) or share

    result = await db.execute(select(Printer).where(Printer.id == share.printer_id))
    printer = result.scalar_one_or_none()
    time_format = await get_setting(db, "time_format") or "system"

    payload = {
        "public_id": share.public_id,
        "status": share.status,
        "end_reason": share.end_reason,
        "end_with_print": bool(share.end_with_print),
        "title": share.title,
        "project_url": share.project_url,
        "printer_id": share.printer_id,
        "printer_name": printer.name if printer else None,
        "model": printer.model if printer else None,
        "camera_rotation": (printer.camera_rotation or 0) if printer else 0,
        "awaiting_plate_clear": bool(getattr(printer, "awaiting_plate_clear", False)) if printer else False,
        "time_format": time_format,
        "ended_at": share.ended_at.isoformat() if share.ended_at else None,
        # Live print fields — only while active
        "connected": False,
        "state": None,
        "current_print": None,
        "gcode_file": None,
        "progress": None,
        "remaining_time": None,
        "layer_num": None,
        "total_layers": None,
        "stg_cur_name": None,
        "temperatures": {},
        "ams_trays": [],
    }

    if share.status == "active" and printer is not None:
        state = printer_manager.get_status(share.printer_id)
        if state is not None:
            ams_trays = []
            for unit in (getattr(state, "ams", None) or []):
                for t in (getattr(unit, "tray", None) or []):
                    t_type = getattr(t, "tray_type", None)
                    if t_type:
                        remain = getattr(t, "remain", None)
                        ams_trays.append(
                            {
                                "ams_id": getattr(unit, "id", 0),
                                "slot_id": getattr(t, "id", 0),
                                "type": t_type,
                                "color": getattr(t, "tray_color", None),
                                "remain": remain if isinstance(remain, (int, float)) and remain >= 0 else None,
                            }
                        )
            for ext in (getattr(state, "vt_tray", None) or []):
                t_type = getattr(ext, "tray_type", None)
                if t_type:
                    remain = getattr(ext, "remain", None)
                    ams_trays.append(
                        {
                            "ams_id": 255,
                            "slot_id": getattr(ext, "id", 254),
                            "type": t_type,
                            "color": getattr(ext, "tray_color", None),
                            "remain": remain if isinstance(remain, (int, float)) and remain >= 0 else None,
                        }
                    )
            payload.update(
                {
                    "connected": state.connected,
                    "state": state.state,
                    "current_print": state.current_print,
                    "gcode_file": state.gcode_file,
                    "progress": state.progress,
                    "remaining_time": state.remaining_time,
                    "layer_num": state.layer_num,
                    "total_layers": state.total_layers,
                    "stg_cur_name": get_derived_status_name(state, printer.model),
                    "temperatures": display_temperatures(state.temperatures, printer.model),
                    "ams_trays": ams_trays,
                }
            )

    return payload
