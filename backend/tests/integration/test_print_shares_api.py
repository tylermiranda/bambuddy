"""Integration tests for Print Share Links API (#3461)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _setup_admin(async_client: AsyncClient, *, suffix: str) -> str:
    await async_client.post(
        "/api/v1/auth/setup",
        json={
            "auth_enabled": True,
            "admin_username": f"shareadmin{suffix}",
            "admin_password": "AdminPass1!",
        },
    )
    login = await async_client.post(
        "/api/v1/auth/login",
        json={"username": f"shareadmin{suffix}", "password": "AdminPass1!"},
    )
    return login.json()["access_token"]


@pytest.fixture
async def printer_row(db_session):
    from backend.app.models.printer import Printer

    printer = Printer(
        name="Share P1S",
        ip_address="192.168.1.99",
        access_code="12345678",
        serial_number="01P00A000000099",
        model="P1S",
    )
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


def _running(printer_name_unused=None):
    return SimpleNamespace(
        connected=True,
        state="RUNNING",
        gcode_file="guest_plate.gcode",
        current_print="Guest Plate",
        subtask_name=None,
        progress=12,
        remaining_time=90,
        layer_num=3,
        total_layers=50,
        temperatures={"nozzle": 220, "bed": 55, "chamber": 28},
    )


class TestPrintSharesApi:
    async def test_create_public_and_revoke(
        self, async_client: AsyncClient, printer_row, db_session
    ):
        jwt = await _setup_admin(async_client, suffix="_happy")

        with patch(
            "backend.app.services.print_shares.printer_manager.get_status",
            return_value=_running(),
        ):
            created = await async_client.post(
                "/api/v1/print-shares/",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"printer_id": printer_row.id},
            )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "active"
        assert body["public_id"].startswith("bbps_")
        assert body["path"].startswith("/share/")
        public_id = body["public_id"]

        with patch(
            "backend.app.services.print_shares.printer_manager.get_status",
            return_value=_running(),
        ), patch(
            "backend.app.api.routes.print_shares.printer_manager.get_status",
            return_value=_running(),
        ):
            public = await async_client.get(f"/api/v1/print-shares/{public_id}/public")
        assert public.status_code == 200, public.text
        payload = public.json()
        assert payload["status"] == "active"
        assert payload["printer_id"] == printer_row.id
        assert payload["printer_name"] == "Share P1S"
        assert "access_code" not in payload
        assert "ip_address" not in payload

        # bbps_ token authenticates overlay-status for that printer only.
        with patch(
            "backend.app.services.print_shares.printer_manager.get_status",
            return_value=_running(),
        ):
            overlay = await async_client.get(
                f"/api/v1/printers/{printer_row.id}/overlay-status?token={public_id}"
            )
        assert overlay.status_code == 200, overlay.text

        revoked = await async_client.delete(
            f"/api/v1/print-shares/{public_id}",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "ended"
        assert revoked.json()["end_reason"] == "revoked"

        public_after = await async_client.get(f"/api/v1/print-shares/{public_id}/public")
        assert public_after.status_code == 200
        assert public_after.json()["status"] == "ended"
        assert public_after.json()["end_reason"] == "revoked"

        # Ended share must not open the overlay feed.
        with patch(
            "backend.app.services.print_shares.printer_manager.get_status",
            return_value=_running(),
        ):
            overlay_after = await async_client.get(
                f"/api/v1/printers/{printer_row.id}/overlay-status?token={public_id}"
            )
        assert overlay_after.status_code == 401

    async def test_create_without_active_print_is_409(
        self, async_client: AsyncClient, printer_row
    ):
        jwt = await _setup_admin(async_client, suffix="_idle")
        idle = _running()
        idle.state = "IDLE"
        with patch(
            "backend.app.services.print_shares.printer_manager.get_status",
            return_value=idle,
        ):
            response = await async_client.post(
                "/api/v1/print-shares/",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"printer_id": printer_row.id},
            )
        assert response.status_code == 409

    async def test_public_unknown_is_404(self, async_client: AsyncClient):
        await _setup_admin(async_client, suffix="_missing")
        response = await async_client.get(
            "/api/v1/print-shares/bbps_doesnotexist000000000000000000/public"
        )
        assert response.status_code == 404
