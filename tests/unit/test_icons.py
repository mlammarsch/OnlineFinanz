"""Tests fuer Icon-Verarbeitung und Icon-Endpunkte (T-090).

Referenziert F-KTO-11 (Upload PNG/JPEG/WebP, hoechstens 512 KB, Typpruefung am
Inhalt, Skalierung auf 128 x 128 PNG, Speicherung in der Datenbank), F-KTO-10
(Bank-Icon ueber denselben Weg) und F-CON-02 (Versionspruefung, 409). Kein
ausgehender Abruf (N-SEC-05); die Testbilder werden im Test erzeugt (D8), es gibt
keine echten Daten.
"""

from __future__ import annotations

import zlib
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from finue.models.account import Account
from finue.services.icons import (
    MAX_ICON_BYTES,
    IconDecompressionBomb,
    IconTooLarge,
    IconTypeInvalid,
    detect_image_format,
    icon_etag,
    process_icon,
)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'


def _image_bytes(image_format: str, size: tuple[int, int] = (64, 64)) -> bytes:
    """Erzeugt ein erfundenes Testbild (D8)."""

    mode = "RGB" if image_format == "JPEG" else "RGBA"
    color: tuple[int, ...] = (200, 30, 30) if mode == "RGB" else (200, 30, 30, 255)
    image = Image.new(mode, size, color)
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def _bomb_png(width: int = 20000, height: int = 20000) -> bytes:
    """Baut ein PNG, dessen IHDR riesige Masse vorgibt (Dekompressionsbombe)."""

    data = bytearray(_image_bytes("PNG", (2, 2)))
    data[16:20] = width.to_bytes(4, "big")
    data[20:24] = height.to_bytes(4, "big")
    data[29:33] = (zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(data)


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_account(client: TestClient, **overrides: object) -> int:
    """Legt ein erfundenes Konto an (D8)."""

    fields: dict[str, object] = {"name": "Konto", "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


# ------------------------------------------------------- Verarbeitung (F-KTO-11)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_gueltige_bildarten_werden_zu_png(image_format: str) -> None:
    """F-KTO-11: gueltige PNG/JPEG/WebP werden akzeptiert und als PNG kodiert."""

    result = process_icon(_image_bytes(image_format, (64, 64)))

    assert result.mime == "image/png"
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(result.data)) as stored:
        assert stored.format == "PNG"
        assert stored.size == (64, 64)


def test_detect_image_format_erkennt_signaturen() -> None:
    """F-KTO-11: die Signaturpruefung erkennt PNG, JPEG und WebP."""

    assert detect_image_format(_image_bytes("PNG")) == "PNG"
    assert detect_image_format(_image_bytes("JPEG")) == "JPEG"
    assert detect_image_format(_image_bytes("WEBP")) == "WEBP"
    assert detect_image_format(SVG) is None


def test_exakt_128x128_bleibt_128x128() -> None:
    """F-KTO-11: ein 128 x 128 grosses Bild bleibt unveraendert gross."""

    result = process_icon(_image_bytes("PNG", (128, 128)))

    assert (result.width, result.height) == (128, 128)


def test_skalierung_erhaelt_seitenverhaeltnis() -> None:
    """F-KTO-11: die Skalierung passt in 128 x 128 und erhaelt das Seitenverhaeltnis."""

    result = process_icon(_image_bytes("PNG", (256, 64)))

    assert (result.width, result.height) == (128, 32)


def test_svg_wird_abgelehnt() -> None:
    """F-KTO-11: SVG ist ausgeschlossen und wird mit Klartext abgelehnt."""

    with pytest.raises(IconTypeInvalid) as error:
        process_icon(SVG)

    assert "SVG" in str(error.value)


def test_fremdinhalt_mit_bildendung_wird_abgelehnt() -> None:
    """F-KTO-11: die Typpruefung entscheidet am Inhalt, nicht an der Endung."""

    with pytest.raises(IconTypeInvalid):
        process_icon(b"kein bild, nur text" * 10)


def test_groesse_vor_dem_dekodieren_geprueft() -> None:
    """F-KTO-11: die 512-KB-plus-ein-Byte-Grenze greift vor dem Dekodieren."""

    oversized = b"\x00" * (MAX_ICON_BYTES + 1)

    with pytest.raises(IconTooLarge):
        process_icon(oversized)


def test_dekompressionsbombe_wird_abgelehnt() -> None:
    """F-KTO-11: eine Dekompressionsbombe wird ueber die Pixelgrenze abgelehnt."""

    with pytest.raises(IconDecompressionBomb):
        process_icon(_bomb_png())


def test_leerer_upload_wird_abgelehnt() -> None:
    """F-KTO-11: ein leerer Upload ist kein Bild."""

    with pytest.raises(IconTypeInvalid):
        process_icon(b"")


def test_icon_etag_ist_stabil() -> None:
    """F-KTO-11: der ETag ist aus den gespeicherten Bytes ableitbar und stabil."""

    data = _image_bytes("PNG")
    first = icon_etag(data)
    assert first == icon_etag(data)
    assert first != icon_etag(_image_bytes("PNG", (16, 16)))
    assert first.startswith('"') and first.endswith('"')


# --------------------------------------------------- Konto-Icon-Routen (F-KTO-11)


def test_konto_icon_upload_auslieferung_und_loeschen(client: TestClient) -> None:
    """F-KTO-11: PUT speichert, GET liefert image/png mit ETag, DELETE entfernt."""

    account_id = _add_account(client)
    png = _image_bytes("JPEG", (200, 100))

    uploaded = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=png,
        headers={"Content-Type": "image/jpeg"},
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["has_icon"] is True
    assert body["version"] == 2
    assert (body["width"], body["height"]) == (128, 64)
    assert body["icon_url"] == f"/api/accounts/{account_id}/icon"

    fetched = client.get(f"/api/accounts/{account_id}/icon")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.content.startswith(b"\x89PNG\r\n\x1a\n")
    etag = fetched.headers["etag"]
    assert etag == icon_etag(fetched.content)

    conditional = client.get(f"/api/accounts/{account_id}/icon", headers={"If-None-Match": etag})
    assert conditional.status_code == 304

    removed = client.delete(f"/api/accounts/{account_id}/icon", params={"version": 2})
    assert removed.status_code == 204
    assert client.get(f"/api/accounts/{account_id}/icon").status_code == 404


def test_konto_icon_versionskonflikt(client: TestClient) -> None:
    """F-CON-02: ein veralteter Stand beim Icon-Upload liefert 409 mit dem Stand."""

    account_id = _add_account(client)
    client.put(f"/api/accounts/{account_id}/icon", params={"version": 1}, content=_image_bytes("PNG"))

    response = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=_image_bytes("PNG"),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == account_id
    assert body["current"]["version"] == 2


def test_konto_icon_svg_und_fremdtyp_abgelehnt(client: TestClient) -> None:
    """F-KTO-11: SVG und Fremdinhalt werden als Fachfehler im Klartext abgelehnt."""

    account_id = _add_account(client)

    svg_response = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=SVG,
        headers={"Content-Type": "image/svg+xml"},
    )
    assert svg_response.status_code == 422
    assert svg_response.json()["code"] == "domain_error"
    assert "SVG" in svg_response.json()["message"]

    # falsche Endung: als image/png deklariert, aber kein Bild im Inhalt
    wrong_type = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=b"reiner text",
        headers={"Content-Type": "image/png"},
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "domain_error"


def test_konto_icon_ignoriert_falsche_deklarierte_endung(client: TestClient) -> None:
    """F-KTO-11: gueltiger Inhalt wird auch bei falsch deklariertem Typ akzeptiert."""

    account_id = _add_account(client)

    response = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=_image_bytes("PNG"),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 200
    assert response.json()["has_icon"] is True


def test_konto_icon_zu_gross_liefert_413(client: TestClient) -> None:
    """F-KTO-11: ein Upload ueber 512 KB liefert 413 payload_too_large."""

    account_id = _add_account(client)

    response = client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": 1},
        content=b"\x00" * (MAX_ICON_BYTES + 1),
    )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_konto_icon_unbekanntes_konto(client: TestClient) -> None:
    """F-KTO-11: ein unbekanntes Konto liefert 404."""

    assert client.get("/api/accounts/9999/icon").status_code == 404
    assert client.put("/api/accounts/9999/icon", params={"version": 1}, content=_image_bytes("PNG")).status_code == 404


def test_konto_icon_ohne_vorhandenes_icon(client: TestClient) -> None:
    """F-KTO-11: ohne Icon liefert GET 404 im Klartext."""

    account_id = _add_account(client)

    response = client.get(f"/api/accounts/{account_id}/icon")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_konto_icon_ereignis(client: TestClient) -> None:
    """F-CON-04: Upload und Loeschen melden das Ereignis ``account.changed``."""

    events: list[object] = []
    client.app.state.services.event_bus.subscribe(events.append)
    account_id = _add_account(client)

    client.put(f"/api/accounts/{account_id}/icon", params={"version": 1}, content=_image_bytes("PNG"))
    client.delete(f"/api/accounts/{account_id}/icon", params={"version": 2})

    types = [getattr(event, "type", None) for event in events]
    assert types.count("account.changed") == 2
    assert getattr(events[0], "get")("account") == account_id
