"""Read-only Cam Wall feed for token-authenticated kiosk displays (#2531).

The Cam Wall inside the SPA runs on the ordinary printers API, behind a JWT. A
wall pinned to a TV has no login, so it authenticates with a long-lived
``camwall``-scoped token carried in the URL — and a URL on a lobby screen is
about as private as a sticky note.

That is why this endpoint exists instead of letting a token through to
``GET /printers``: the printer list carries ``serial_number`` and
``ip_address`` (see ``schemas/printer.py``), and neither belongs on a screen in
a shared room. What a wall tile actually draws is the whole payload here — a
name, a connection flag, a state, a progress bar.

Notably absent is the print filename. A token wall renders the compact status
overlay, so the part being printed is never named to the room; the field simply
isn't served rather than being served and then hidden client-side.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequireCamWallTokenIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.printer_scope import PrinterScope
from backend.app.models.printer import Printer
from backend.app.services.printer_manager import display_temperatures, printer_manager

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/camwall", tags=["camwall"])


@router.get("/printers")
async def list_camwall_printers(
    printer_scope: PrinterScope = RequireCamWallTokenIfAuthEnabled,
    db: AsyncSession = Depends(get_db, scope="function"),
) -> list[dict]:
    """Every printer the token's owner may see (#1727), plus the handful of
    status fields a Cam Wall tile draws.

    One call for the whole wall rather than one per printer: a kiosk polls this
    on a fixed interval with no WebSocket to invalidate it, and N+1 requests
    every few seconds is a poor trade for a screen nobody is interacting with.

    Ordered by name so tile positions stay put across polls — a wall that
    reshuffles itself is unusable to watch.
    """
    query = select(Printer).order_by(Printer.name)
    if (clause := printer_scope.where_strict(Printer.id)) is not None:
        query = query.where(clause)
    result = await db.execute(query)
    printers = list(result.scalars().all())

    payload: list[dict] = []
    for printer in printers:
        state = printer_manager.get_status(printer.id)
        entry: dict = {
            "id": printer.id,
            "name": printer.name,
            "model": printer.model,
            "camera_rotation": printer.camera_rotation or 0,
            "awaiting_plate_clear": bool(getattr(printer, "awaiting_plate_clear", False)),
            # Mirrors get_printer_status(): no state object at all means the
            # printer was never connected this run; a state object still has
            # to be asked whether its link is currently up.
            "connected": bool(state and state.connected),
            "state": None,
            "progress": None,
            "remaining_time": None,
            "layer_num": None,
            "total_layers": None,
            "temperatures": {},
            "ams_trays": [],
            # Codes only — enough for the client to run the same
            # filterKnownHMSErrors() it uses on the authenticated wall, so the
            # error chip means the same thing in both modes.
            "hms_errors": [],
        }
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
            entry.update(
                {
                    "state": state.state,
                    "progress": state.progress,
                    "remaining_time": state.remaining_time,
                    "layer_num": state.layer_num,
                    "total_layers": state.total_layers,
                    "temperatures": display_temperatures(state.temperatures, printer.model),
                    "ams_trays": ams_trays,
                    "hms_errors": [
                        {
                            "code": e.code,
                            "attr": e.attr,
                            "module": e.module,
                            "severity": e.severity,
                            "actions": e.actions or [],
                            # The wall counts a fault only when Bambu publishes
                            # text for it or it offers actions, the same rule as
                            # the printer card, so it needs the text (#2728).
                            "full_code": e.full_code,
                            "description": e.description,
                        }
                        for e in (state.hms_errors or [])
                    ],
                }
            )
        payload.append(entry)

    return payload
