"""Print Share Links (#3461) — guest URLs that end when the print ends.

A share is a high-entropy public id bound to one printer's active job. Guests
with the URL can watch the camera and progress for that print only; they never
receive printer control, LAN addresses, or access codes. When the print
completes, fails, or is cancelled — or the owner revokes — the share ends.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrintShare(Base):
    """One shareable guest link for an in-flight (or recently ended) print."""

    __tablename__ = "print_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Public credential in the URL: ``bbps_<32 urlsafe chars>``. Indexed for
    # guest lookups; treated as a secret (anyone with it can watch the print).
    public_id: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    printer_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("printers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Archive row for the active print when known — used for project URL
    # defaults and end-cap finish photos. Nullable for MQTT-only starts that
    # have not been archived yet.
    archive_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("print_archives.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Fingerprint of the job this share tracks (filename / print name at create).
    # Used to refuse reuse across a later print on the same printer.
    job_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # MakerWorld / Printables / etc. Snapshot at create so the guest page keeps
    # a stable outbound link even if the archive is edited later.
    project_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # active | ended
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    # completed | failed | cancelled | revoked | superseded
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PrintShare id={self.id} public_id={self.public_id!r} "
            f"printer_id={self.printer_id} status={self.status!r}>"
        )
