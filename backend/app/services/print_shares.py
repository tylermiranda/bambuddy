"""Service layer for Print Share Links (#3461)."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
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


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


async def _archive_for_share(
    db: AsyncSession,
    *,
    printer_id: int,
    job_key: str,
    current_print: str | None,
) -> PrintArchive | None:
    """Best-effort archive for project URL / title defaults.

    Prefer the live ``printing`` row; otherwise match a recent archive on this
    printer by filename / print name (MQTT restart-recovery often has no
    ``printing`` row yet but a finished-or-sibling archive still carries
    MakerWorld metadata).
    """
    archive = await _active_archive_for_printer(db, printer_id)
    if archive is not None:
        return archive

    needles = [n for n in {job_key, (current_print or "").strip()} if n]
    if not needles:
        return None

    clauses = []
    for needle in needles:
        short = needle.rsplit("/", 1)[-1]
        clauses.append(PrintArchive.filename.contains(short))
        clauses.append(PrintArchive.print_name.contains(short))
        if short.endswith(".gcode"):
            base = short[: -len(".gcode")]
            clauses.append(PrintArchive.print_name.contains(base))

    result = await db.execute(
        select(PrintArchive)
        .where(PrintArchive.printer_id == printer_id, or_(*clauses))
        .order_by(PrintArchive.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _library_project_url(db: AsyncSession, *, title: str | None) -> str | None:
    """Fall back to a library file's external_url when the title matches."""
    needle = (title or "").strip()
    if not needle:
        return None
    short = needle.rsplit("/", 1)[-1]
    if short.endswith(".gcode"):
        short = short[: -len(".gcode")]
    if len(short) < 3:
        return None
    result = await db.execute(
        select(LibraryFile)
        .where(
            LibraryFile.external_url.is_not(None),
            LibraryFile.filename.contains(short),
        )
        .order_by(LibraryFile.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return (row.external_url or "").strip() or None if row else None


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
    end_with_print: bool = True,
    expires_in_hours: int | None = None,
) -> PrintShare:
    """Create a share for the printer's current print. Ends any prior active share."""
    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected:
        raise ValueError("Printer is not connected")
    is_active_print = state.state in _ACTIVE_PRINT_STATES
    if end_with_print and not is_active_print:
        raise ValueError("No active print to share")

    job_key = _job_key_from_state(state) if is_active_print else ""
    if end_with_print and not job_key:
        raise ValueError("No active print to share")

    archive = (
        await _archive_for_share(
            db,
            printer_id=printer_id,
            job_key=job_key,
            current_print=getattr(state, "current_print", None),
        )
        if is_active_print
        else None
    )
    resolved_title = (
        (title or "").strip()
        or (archive.print_name if archive else None)
        or (getattr(state, "current_print", None) if is_active_print else None)
        or job_key
        or None
    )
    resolved_project = (project_url or "").strip() or _default_project_url(archive)
    if not resolved_project:
        resolved_project = await _library_project_url(db, title=resolved_title)

    expires_at = None
    if expires_in_hours is not None:
        if expires_in_hours < 1 or expires_in_hours > 17520:
            raise ValueError("expires_in_hours must be between 1 and 17520")
        expires_at = _utcnow_naive() + timedelta(hours=expires_in_hours)

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
        end_with_print=bool(end_with_print),
        expires_at=expires_at,
        status="active",
        created_by_id=created_by_id,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    logger.info(
        "Created print share %s for printer %s job=%r archive=%s end_with_print=%s expires_at=%s",
        share.public_id,
        printer_id,
        job_key,
        share.archive_id,
        share.end_with_print,
        share.expires_at,
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
    share.ended_at = _utcnow_naive()
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
    only_end_with_print: bool = False,
) -> int:
    """End active shares for ``printer_id``.

    When ``only_end_with_print`` is True (print-complete callback), shares that
    opted out of end-with-print are left alone until expiry / revoke.
    """
    result = await db.execute(
        select(PrintShare).where(
            PrintShare.printer_id == printer_id,
            PrintShare.status == "active",
        )
    )
    shares = list(result.scalars().all())
    ended = 0
    for share in shares:
        if only_end_with_print and not share.end_with_print:
            continue
        await end_share(db, share, reason=reason, commit=False)
        ended += 1
    if commit and ended:
        await db.commit()
    return ended


def _is_expired(share: PrintShare, *, now: datetime | None = None) -> bool:
    if share.expires_at is None:
        return False
    return (now or _utcnow_naive()) >= share.expires_at


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

    if _is_expired(share):
        await end_share(db, share, reason="expired")
        return None

    # Defence in depth: if end_with_print and the printer is idle / on a
    # different job, end the share.
    if share.end_with_print:
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
        "end_with_print": bool(share.end_with_print),
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        "status": share.status,
        "end_reason": share.end_reason,
        "created_by_id": share.created_by_id,
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "ended_at": share.ended_at.isoformat() if share.ended_at else None,
        "path": path,
        "url": url,
    }
