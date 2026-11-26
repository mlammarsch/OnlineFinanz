"""Tests fuer Normalisierung und Fingerprint der Dublettenkennung.

Referenziert F-DUP-01 (SHA-256 ueber normalisierte Felder in fester
Reihenfolge) und grenzt die Felder gegen F-DUP-05 ab (Notiz, geprueft,
abgestimmt, externe Referenz und Status gehoeren nicht in den Fingerprint).
"""

from datetime import date

from finue.services.duplicate import compute_fingerprint, normalize_field


def make_fingerprint(
    account_id: int = 1,
    booking_date: date = date(2026, 1, 15),
    value_date: date | None = date(2026, 1, 16),
    amount_cents: int = -4883,
    currency: str = "EUR",
    counterparty: str | None = "Max Mustermann",
    counterparty_iban: str | None = "DE89370400440532013000",
    purpose: str | None = "SEPA Ueberweisung",
) -> str:
    """Erzeugt einen Fingerprint aus den F-DUP-01-Standardfeldern."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
    )


class TestNormalizeField:
    """Normalisierungsregeln aus F-DUP-01."""

    def test_none_und_leer_werden_zu_leerem_string(self) -> None:
        assert normalize_field(None) == ""
        assert normalize_field("") == ""
        assert normalize_field(" \t\r\n ") == ""

    def test_unicode_nfc_gleicht_zusammengesetzte_und_zerlegte_umlaute_an(self) -> None:
        composed = "Müller"
        decomposed = "Mu\u0308ller"
        assert normalize_field(composed) == "MÜLLER"
        assert normalize_field(decomposed) == "MÜLLER"
        assert normalize_field(composed) == normalize_field(decomposed)

    def test_grossschreibung(self) -> None:
        assert normalize_field("max mustermann") == "MAX MUSTERMANN"
        assert normalize_field("straße") == "STRASSE"

    def test_zeilenumbrueche_werden_entfernt_nicht_durch_leerzeichen_ersetzt(self) -> None:
        assert normalize_field("WORT\nWORT") == "WORTWORT"
        assert normalize_field("WORT\r\nWORT") == "WORTWORT"
        assert normalize_field("WORT\rWORT") == "WORTWORT"

    def test_crlf_und_lf_mix(self) -> None:
        assert normalize_field("Zeile1\r\nZeile2\nZeile3") == "ZEILE1ZEILE2ZEILE3"

    def test_mehrfache_leerzeichen_werden_zusammengefasst(self) -> None:
        assert normalize_field("  Max    Mustermann\t \t") == "MAX MUSTERMANN"

    def test_trimmen(self) -> None:
        assert normalize_field("\t Max Mustermann \r\n") == "MAX MUSTERMANN"

    def test_mt940_umbruch_nach_27_zeichen_mitten_im_wort(self) -> None:
        # MT940 bricht nach 27 Zeichen um; ein Wort kann geteilt werden.
        wrapped = "SEPA UEBERWEISUNG MAX MU\nSTERMANN"
        unwrapped = "SEPA UEBERWEISUNG MAX MUSTERMANN"
        assert normalize_field(wrapped) == normalize_field(unwrapped)

    def test_mt940_umbruch_nach_65_zeichen_mitten_im_wort(self) -> None:
        part1 = "X" * 65
        wrapped = f"{part1}\nREST"
        assert normalize_field(wrapped) == normalize_field(f"{part1}REST")


class TestComputeFingerprint:
    """Fingerprint-Stabilitaet und Feldabgrenzung aus F-DUP-01."""

    def test_gleiche_eingabe_ergibt_gleichen_hash(self) -> None:
        assert make_fingerprint() == make_fingerprint()

    def test_fingerprint_ist_sha256_hex(self) -> None:
        fingerprint = make_fingerprint()
        assert len(fingerprint) == 64
        assert fingerprint == fingerprint.lower()
        assert len(bytes.fromhex(fingerprint)) == 32

    def test_golden_fingerprint_pinnt_kanonische_form(self) -> None:
        # Regression guard fuer Feldreihenfolge und Trenner aus F-DUP-01.
        assert make_fingerprint() == "903227023fc6d8873bf9213b7300c55d45c4b9f6a5d19089f2ee86b3ad39e8c7"

    def test_semantisch_gleich_syntaktisch_verschieden(self) -> None:
        variant = make_fingerprint(
            currency="eur",
            counterparty="  max   mustermann  ",
            counterparty_iban=" de89370400440532013000 ",
            purpose="sepa \r\nueberweisung",
        )
        assert variant == make_fingerprint()

    def test_umlaut_nfc_variante_ergibt_gleichen_fingerprint(self) -> None:
        composed = make_fingerprint(counterparty="Müller")
        decomposed = make_fingerprint(counterparty="Mu\u0308ller")
        assert composed == decomposed

    def test_none_und_leerer_string_sind_gleich(self) -> None:
        without_text = make_fingerprint(counterparty=None, counterparty_iban=None, purpose=None)
        empty_text = make_fingerprint(counterparty="", counterparty_iban="", purpose="")
        assert without_text == empty_text

    def test_fehlende_wertstellung_aendert_fingerprint(self) -> None:
        assert make_fingerprint(value_date=None) != make_fingerprint()

    def test_waehrung_wird_grossgeschrieben(self) -> None:
        assert make_fingerprint(currency="eur") == make_fingerprint(currency="EUR")

    def test_mt940_umbruch_im_verwendungszweck_aendert_fingerprint_nicht(self) -> None:
        wrapped = make_fingerprint(purpose="SEPA UEBERWEISUNG MAX MU\nSTERMANN")
        unwrapped = make_fingerprint(purpose="SEPA UEBERWEISUNG MAX MUSTERMANN")
        assert wrapped == unwrapped

    def test_buchungstag_und_wertstellung_sind_nicht_vertauschbar(self) -> None:
        normal = make_fingerprint(booking_date=date(2026, 1, 15), value_date=date(2026, 1, 16))
        swapped = make_fingerprint(booking_date=date(2026, 1, 16), value_date=date(2026, 1, 15))
        assert normal != swapped

    def test_jede_feldwert_aenderung_aendert_fingerprint(self) -> None:
        base = make_fingerprint()
        variants = {
            "account_id": make_fingerprint(account_id=2),
            "booking_date": make_fingerprint(booking_date=date(2026, 1, 16)),
            "value_date": make_fingerprint(value_date=date(2026, 1, 17)),
            "amount_cents": make_fingerprint(amount_cents=-4884),
            "currency": make_fingerprint(currency="USD"),
            "counterparty": make_fingerprint(counterparty="Erika Musterfrau"),
            "counterparty_iban": make_fingerprint(counterparty_iban="DE89370400440532013001"),
            "purpose": make_fingerprint(purpose="Anderer Zweck"),
        }
        for field, fingerprint in variants.items():
            assert fingerprint != base, field
        assert len(set(variants.values())) == len(variants)
