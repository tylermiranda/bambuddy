"""Unit tests for Print Share Links (#3461)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services import print_shares as share_service

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _running_state(**overrides):
    base = dict(
        connected=True,
        state="RUNNING",
        gcode_file="plate_1.gcode",
        current_print="Demo Print",
        subtask_name=None,
        progress=42,
        remaining_time=30,
        layer_num=10,
        total_layers=100,
        temperatures={"nozzle": 220, "bed": 60, "chamber": 30},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPrintShareTokenHelpers:
    def test_is_print_share_token(self):
        assert share_service.is_print_share_token("bbps_abc")
        assert not share_service.is_print_share_token("bblt_abc")
        assert not share_service.is_print_share_token(None)
        assert not share_service.is_print_share_token("")


class TestCreateAndEndShare:
    async def test_create_requires_active_print(self, db_session, printer_factory):
        printer = await printer_factory()
        with patch.object(share_service.printer_manager, "get_status", return_value=_running_state(state="IDLE")):
            with pytest.raises(ValueError, match="No active print"):
                await share_service.create_print_share(
                    db_session, printer_id=printer.id, created_by_id=None
                )

    async def test_create_and_verify_then_end_on_idle(self, db_session, printer_factory):
        printer = await printer_factory()
        state = _running_state()
        with patch.object(share_service.printer_manager, "get_status", return_value=state):
            share = await share_service.create_print_share(
                db_session,
                printer_id=printer.id,
                created_by_id=None,
                title="Guest Demo",
                project_url="https://makerworld.com/models/1",
            )
            assert share.public_id.startswith("bbps_")
            assert share.status == "active"
            assert share.job_key == "plate_1.gcode"
            assert share.title == "Guest Demo"
            assert share.project_url.endswith("/models/1")

            live = await share_service.verify_active_share_token(
                db_session, share.public_id, printer_id=printer.id
            )
            assert live is not None
            assert live.id == share.id

            # Wrong printer id must not authenticate the stream.
            wrong = await share_service.verify_active_share_token(
                db_session, share.public_id, printer_id=printer.id + 999
            )
            assert wrong is None

        # Printer goes idle → share ends on next verify.
        with patch.object(
            share_service.printer_manager,
            "get_status",
            return_value=_running_state(state="IDLE", gcode_file=""),
        ):
            ended = await share_service.verify_active_share_token(
                db_session, share.public_id, printer_id=printer.id
            )
            assert ended is None
            refreshed = await share_service.get_share_by_public_id(db_session, share.public_id)
            assert refreshed is not None
            assert refreshed.status == "ended"
            assert refreshed.end_reason == "completed"

    async def test_create_supersedes_prior_active_share(self, db_session, printer_factory):
        printer = await printer_factory()
        with patch.object(share_service.printer_manager, "get_status", return_value=_running_state()):
            first = await share_service.create_print_share(
                db_session, printer_id=printer.id, created_by_id=None
            )
            second = await share_service.create_print_share(
                db_session, printer_id=printer.id, created_by_id=None
            )
            assert first.public_id != second.public_id
            refreshed = await share_service.get_share_by_public_id(db_session, first.public_id)
            assert refreshed is not None
            assert refreshed.status == "ended"
            assert refreshed.end_reason == "superseded"
            assert second.status == "active"

    async def test_share_to_owner_dict_builds_url(self):
        share = MagicMock(
            id=1,
            public_id="bbps_testtoken1234567890123456789012",
            printer_id=7,
            archive_id=None,
            job_key="a.gcode",
            title="T",
            project_url=None,
            end_with_print=True,
            expires_at=None,
            status="active",
            end_reason=None,
            created_by_id=1,
            created_at=None,
            ended_at=None,
        )
        payload = share_service.share_to_owner_dict(share, external_url="https://bambu.example")
        assert payload["path"] == f"/share/{share.public_id}"
        assert payload["url"] == f"https://bambu.example/share/{share.public_id}"
        assert payload["end_with_print"] is True

    async def test_end_active_respects_end_with_print_flag(self, db_session, printer_factory):
        printer = await printer_factory()
        with patch.object(share_service.printer_manager, "get_status", return_value=_running_state()):
            stay = await share_service.create_print_share(
                db_session,
                printer_id=printer.id,
                created_by_id=None,
                end_with_print=False,
                expires_in_hours=24,
            )
        ended = await share_service.end_active_shares_for_printer(
            db_session, printer.id, reason="completed", only_end_with_print=True
        )
        assert ended == 0
        refreshed = await share_service.get_share_by_public_id(db_session, stay.public_id)
        assert refreshed is not None
        assert refreshed.status == "active"
