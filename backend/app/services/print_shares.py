"""Service layer for Print Share Links (#3461)."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_share import PrintShare
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "bbps_"
_SECRET_LEN = 32

# Printer states that still count as "this print is ongoing".
_ACTIVE_PRINT_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE", "SLICING"})


def is_print_share_token(token: str | None) -> bool:
    return bool(token) and token.startswith(_TOKEN_PREFIX)


def _generate_public_id() -> str:
    secret = secrets.token_urlsafe(48).replace("_", "").replace("-", "")[:_SECRET_LEN]
    return f"{_TOKEN_PREFIX}{secret}"


def _job_key_from_state(state) -> str:
    if state is None:
        return ""
    return (state.gcode_file or state.current_print or state.subtask_name or "").strip()


async def _active_archive_for_printer(db: AsyncSession, printer_id: int) -> PrintArchive | None:
    result = await db.execute(
        select(PrintArchive)
        .where(
            PrintArchive.printer_id == printer_id,
            PrintArchive.status == "printing",
        )
        .order_by(PrintArchive.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _default_project_url(archive: PrintArchive | None) -> str | None:
    if archive is None:
        return None
    return archive.external_url or archive.makerworld_url or None


async def create_print_share(
    db: AsyncSession,
    *,
    printer_id: int,
    created_by_id: int | None,
    title: str | None = None,
    project_url: str | None = None,
) -> PrintShare:
    """Create a share for the printer's current print. Ends any prior active share."""
    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected:
        raise ValueError("Printer is not connected")
    if state.state not in _ACTIVE_PRINT_STATES:
        raise ValueError("No active print to share")

    job_key = _job_key_from_state(state)
    if not job_key:
        raise ValueError("No active print to share")

    archive = await _active_archive_for_printer(db, printer_id)
    resolved_project = (project_url or "").strip() or _default_project_url(archive)
    resolved_title = (title or "").strip() or (archive.print_name if archive else None) or job_key

    # One active share per printer — ending the old one keeps guest URLs honest
    # when a new share is minted for a later plate / reprint.
    await end_active_shares_for_printer(db, printer_id, reason="superseded", commit=False)

    share = PrintShare(
        public_id=_generate_public_id(),
        printer_id=printer_id,
        archive_id=archive.id if archive else None,
        job_key=job_key[:255],
        title=resolved_title[:200] if resolved_title else None,
        project_url=resolved_project[:500] if resolved_project else None,
        status="active",
        created_by_id=created_by_id,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    logger.info(
        "Created print share %s for printer %s job=%r archive=%s",
        share.public_id,
        printer_id,
        job_key,
        share.archive_id,
    )
    return share


async def get_share_by_public_id(db: AsyncSession, public_id: str) -> PrintShare | None:
    result = await db.execute(select(PrintShare).where(PrintShare.public_id == public_id))
    return result.scalar_one_or_none()


async def list_shares_for_user(
    db: AsyncSession,
    *,
    user_id: int | None,
    include_ended: bool = True,
    limit: int = 50,
) -> list[PrintShare]:
    stmt = select(PrintShare).order_by(PrintShare.id.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(PrintShare.created_by_id == user_id)
    if not include_ended:
        stmt = stmt.where(PrintShare.status == "active")
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def end_share(
    db: AsyncSession,
    share: PrintShare,
    *,
    reason: str,
    commit: bool = True,
) -> PrintShare:
    if share.status != "active":
        return share
    share.status = "ended"
    share.end_reason = reason
    share.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if commit:
        await db.commit()
        await db.refresh(share)
    logger.info("Ended print share %s reason=%s", share.public_id, reason)
    return share


async def end_active_shares_for_printer(
    db: AsyncSession,
    printer_id: int,
    *,
    reason: str,
    commit: bool = True,
) -> int:
    result = await db.execute(
        select(PrintShare).where(
            PrintShare.printer_id == printer_id,
            PrintShare.status == "active",
        )
    )
    shares = list(result.scalars().all())
    for share in shares:
        await end_share(db, share, reason=reason, commit=False)
    if commit and shares:
        await db.commit()
    return len(shares)


async def verify_active_share_token(
    db: AsyncSession,
    token: str,
    *,
    printer_id: int | None = None,
) -> PrintShare | None:
    """Return the share when ``token`` is an active print-share id.

    When ``printer_id`` is set, the share must belong to that printer — used by
    camera / overlay-status routes so a share for printer A cannot pull B's feed.
    """
    if not is_print_share_token(token):
        return None
    share = await get_share_by_public_id(db, token)
    if share is None or share.status != "active":
        return None
    if printer_id is not None and share.printer_id != printer_id:
        return None

    # Defence in depth: if the printer is idle / on a different job, end the share.
    state = printer_manager.get_status(share.printer_id)
    if state is None or state.state not in _ACTIVE_PRINT_STATES:
        await end_share(db, share, reason="completed")
        return None
    current_key = _job_key_from_state(state)
    if share.job_key and current_key and share.job_key != current_key:
        # Filename can shift slightly (plate_1.gcode vs basename); allow either
        # containing the other, otherwise end.
        if share.job_key not in current_key and current_key not in share.job_key:
            await end_share(db, share, reason="superseded")
            return None
    return share


def share_to_owner_dict(share: PrintShare, *, external_url: str | None = None) -> dict:
    path = f"/share/{share.public_id}"
    url = f"{external_url.rstrip('/')}{path}" if external_url else path
    return {
        "id": share.id,
        "public_id": share.public_id,
        "printer_id": share.printer_id,
        "archive_id": share.archive_id,
        "job_key": share.job_key,
        "title": share.title,
        "project_url": share.project_url,
        "status": share.status,
        "end_reason": share.end_reason,
        "created_by_id": share.created_by_id,
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "ended_at": share.ended_at.isoformat() if share.ended_at else None,
        "path": path,
        "url": url,
    }
