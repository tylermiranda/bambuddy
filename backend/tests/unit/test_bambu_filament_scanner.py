"""Unit tests for the official Bambu Lab filament box/barcode/code scanner."""

import pytest

from backend.app.services.bambu_filament_scanner import (
    load_bambu_catalog,
    resolve_bambu_scan,
    save_learned_barcode_mapping,
    search_bambu_catalog,
)


def test_bambu_catalog_loaded():
    catalog = load_bambu_catalog()
    assert len(catalog["filaments"]) >= 340
    assert "10100" in catalog["by_code"]
    jade = catalog["by_code"]["10100"]
    assert jade["fila_type"] == "PLA Basic"
    assert jade["material"] == "PLA"
    assert jade["subtype"] == "Basic"
    assert jade["color_name"] == "Jade White"
    assert jade["rgba"] == "FFFFFFFF"
    assert jade["filament_id"] == "GFA00"


def test_search_bambu_catalog():
    by_code = search_bambu_catalog("10100")
    assert by_code
    assert by_code[0]["filament_code"] == "10100"

    by_variant = search_bambu_catalog("A00-W1")
    assert by_variant
    assert by_variant[0]["filament_code"] == "10100"

    by_name = search_bambu_catalog("PLA Matte Charcoal")
    assert by_name
    assert by_name[0]["filament_code"] == "11101"


@pytest.mark.asyncio
async def test_resolve_bambu_scan_filament_code_and_sku(db_session):
    # 5-digit code
    res = await resolve_bambu_scan(db_session, "10100")
    assert res["matched"] is True
    assert res["match_type"] == "filament_code"
    assert res["filament"]["color_name"] == "Jade White"
    assert res["is_refill"] is False
    assert res["label_weight"] == 1000
    assert res["core_weight"] == 250

    # SKU with spool (A00-W01-1.75-1000-SPL)
    res_sku = await resolve_bambu_scan(db_session, "A00-W01-1.75-1000-SPL")
    assert res_sku["matched"] is True
    assert res_sku["match_type"] == "sku"
    assert res_sku["filament"]["filament_code"] == "10100"
    assert res_sku["is_refill"] is False
    assert res_sku["core_weight"] == 250

    # SKU refill (A01-K01-1.75-1000-SPLFREE -> 11101 PLA Matte Charcoal)
    res_refill = await resolve_bambu_scan(db_session, "A01-K01-1.75-1000-SPLFREE")
    assert res_refill["matched"] is True
    assert res_refill["match_type"] == "sku"
    assert res_refill["filament"]["filament_code"] == "11101"
    assert res_refill["is_refill"] is True
    assert res_refill["core_weight"] == 0


@pytest.mark.asyncio
async def test_resolve_and_learn_retail_barcode(db_session):
    unmapped = await resolve_bambu_scan(db_session, "6975337039990")
    assert unmapped["matched"] is False
    assert unmapped["match_type"] == "unmapped_barcode"
    assert unmapped["detected_barcode"] == "6975337039990"

    await save_learned_barcode_mapping(
        db_session,
        barcode="6975337039990",
        filament_code="10101",
        is_refill=True,
        label_weight=1000,
    )

    mapped = await resolve_bambu_scan(db_session, "6975337039990")
    assert mapped["matched"] is True
    assert mapped["match_type"] == "barcode"
    assert mapped["filament"]["filament_code"] == "10101"
    assert mapped["filament"]["color_name"] == "Black"
    assert mapped["is_refill"] is True
    assert mapped["core_weight"] == 0


@pytest.mark.asyncio
async def test_bambu_scanner_api_routes(async_client):
    resp = await async_client.get("/api/v1/inventory/bambu-catalog?q=10100")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["filaments"]) >= 1
    assert data["filaments"][0]["filament_code"] == "10100"

    scan_resp = await async_client.post(
        "/api/v1/inventory/scan-bambu",
        json={"raw_input": "A00-W01-1.75-1000-SPL"},
    )
    assert scan_resp.status_code == 200
    scan_data = scan_resp.json()
    assert scan_data["matched"] is True
    assert scan_data["filament"]["filament_code"] == "10100"

    learn_resp = await async_client.post(
        "/api/v1/inventory/scan-bambu/learn-barcode",
        json={
            "barcode": "6975337038887",
            "filament_code": "11100",
            "is_refill": False,
            "label_weight": 1000,
        },
    )
    assert learn_resp.status_code == 200
    learn_data = learn_resp.json()
    assert learn_data["matched"] is True
    assert learn_data["filament"]["filament_code"] == "11100"
    assert learn_data["filament"]["color_name"] == "Ivory White"
