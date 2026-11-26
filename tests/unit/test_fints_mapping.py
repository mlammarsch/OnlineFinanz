"""Tests fuer die Abbildung von camt- und MT940-Umsaetzen auf Buchungsdaten.

Referenziert F-SYN-03 (Saldo, gebuchte und vorgemerkte Umsaetze),
F-DUP-01 (identischer Fingerprint fuer dieselbe Buchung), F-DUP-04
(Statustrennung gebucht/vorgemerkt), N-DB-02 (ganze Cent, kein Float) und
D8 (nur erfundene Daten, kein Bankzugriff).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mt940.models import Balance, Transactions
from mt940.options import Options

from finue.services.duplicate import (
    TransactionData,
    compute_fingerprint,
    normalize_field,
)
from finue.services.fints_mapping import (
    FinTSMappingError,
    map_balance,
    map_camt_transactions,
    map_mt940_transactions,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fints"


def _camt_booked() -> bytes:
    """Erfundener camt.053-Stream mit zwei Buchungen und einem Minimal-Eintrag."""
    return (FIXTURES / "camt053_booked.xml").read_bytes()


def _camt_pending() -> bytes:
    """Erfundener camt.052-Stream mit zwei Vormerkungen."""
    return (FIXTURES / "camt052_pending.xml").read_bytes()


def _mt940_transactions() -> list[Any]:
    """Parst den erfundenen MT940-Stream so, wie python-fints ihn liefert."""
    text = (FIXTURES / "mt940_booked.mt940").read_text(encoding="utf-8")
    transactions = Transactions(options=Options(applicant_iban=True))
    return list(transactions.parse(text))


def _minimal_camt(entry_inner: str) -> str:
    """Baut ein camt-Dokument mit genau einem ``Ntry``-Inhalt."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
        "<BkToCstmrStmt><Stmt><Ntry>"
        f"{entry_inner}"
        "</Ntry></Stmt></BkToCstmrStmt></Document>"
    )


def _fingerprint(transaction: TransactionData) -> str:
    """Berechnet den Fingerprint einer ``TransactionData`` fuer ein Konto."""
    return compute_fingerprint(
        account_id=1,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
    )


class TestCamtGebucht:
    """Gebuchte camt.053-Eintraege (F-SYN-03, N-DB-02)."""

    def test_soll_und_haben_werden_in_ganzen_cent_abgebildet(self) -> None:
        transactions = map_camt_transactions([_camt_booked()])
        assert [t.amount_cents for t in transactions] == [-85000, 250000, 123]
        assert all(isinstance(t.amount_cents, int) for t in transactions)
        assert all(not isinstance(t.amount_cents, bool) for t in transactions)

    def test_origin_und_status_sind_fints_und_gebucht(self) -> None:
        transactions = map_camt_transactions([_camt_booked()])
        assert {t.origin for t in transactions} == {"fints"}
        assert {t.status for t in transactions} == {"gebucht"}

    def test_waehrung_gegenpartei_iban_und_zweck(self) -> None:
        debit, credit, _ = map_camt_transactions([_camt_booked()])
        assert debit.currency == "EUR"
        assert debit.counterparty == "Max Mustermann"
        assert debit.counterparty_iban == "DE89370400440532013000"
        assert debit.purpose == "RECHNUNG 4711\nMIETE JANUAR"
        assert credit.counterparty == "Müller GmbH"
        assert credit.counterparty_iban == "DE02120300000000202051"

    def test_umlaute_bleiben_erhalten(self) -> None:
        _, credit, _ = map_camt_transactions([_camt_booked()])
        assert credit.counterparty == "Müller GmbH"

    def test_mehrzeiliger_zweck_ergibt_denselben_normalisierten_text(self) -> None:
        debit, _, _ = map_camt_transactions([_camt_booked()])
        assert debit.purpose is not None
        assert debit.purpose.splitlines() == ["RECHNUNG 4711", "MIETE JANUAR"]
        assert normalize_field(debit.purpose) == "RECHNUNG 4711MIETE JANUAR"

    def test_buchungs_und_wertstellungsdatum(self) -> None:
        debit, credit, _ = map_camt_transactions([_camt_booked()])
        assert debit.booking_date == date(2026, 1, 7)
        assert debit.value_date == date(2026, 1, 5)
        assert credit.booking_date == date(2026, 1, 6)
        assert credit.value_date == date(2026, 1, 6)

    def test_externe_referenz_nur_wenn_geliefert(self) -> None:
        debit, credit, without_reference = map_camt_transactions([_camt_booked()])
        assert debit.external_ref == "E2E-4711"
        assert credit.external_ref == "E2E-4712"
        assert without_reference.external_ref is None

    def test_fehlende_felder_bleiben_leer(self) -> None:
        _, _, minimal = map_camt_transactions([_camt_booked()])
        assert minimal.counterparty is None
        assert minimal.counterparty_iban is None
        assert minimal.purpose is None
        assert minimal.external_ref is None
        assert minimal.amount_cents == 123

    def test_grosse_betraege_bleiben_exakt(self) -> None:
        document = _minimal_camt(
            '<Amt Ccy="EUR">1234567890123.45</Amt>'
            "<CdtDbtInd>CRDT</CdtDbtInd>"
            "<BookgDt><Dt>2026-02-01</Dt></BookgDt>"
            "<ValDt><Dt>2026-02-01</Dt></ValDt>"
        )
        transaction = map_camt_transactions([document])[0]
        assert transaction.amount_cents == 123456789012345


class TestCamtVorgemerkt:
    """Vorgemerkte camt.052-Eintraege (F-SYN-03, F-DUP-04)."""

    def test_status_ist_vorgemerkt(self) -> None:
        transactions = map_camt_transactions([], [_camt_pending()])
        assert [t.status for t in transactions] == ["vorgemerkt", "vorgemerkt"]

    def test_fehlender_buchungstag_faellt_auf_wertstellung_zurueck(self) -> None:
        debit, credit = map_camt_transactions([], [_camt_pending()])
        assert debit.booking_date == date(2026, 1, 8)
        assert debit.value_date == date(2026, 1, 8)
        assert credit.booking_date == date(2026, 1, 9)
        assert credit.value_date == date(2026, 1, 10)

    def test_vormerkungen_werden_nach_gebuchten_geliefert(self) -> None:
        transactions = map_camt_transactions([_camt_booked()], [_camt_pending()])
        assert [t.status for t in transactions] == [
            "gebucht",
            "gebucht",
            "gebucht",
            "vorgemerkt",
            "vorgemerkt",
        ]

    def test_nonref_gilt_als_keine_externe_referenz(self) -> None:
        debit, credit = map_camt_transactions([], [_camt_pending()])
        assert debit.external_ref is None
        assert credit.external_ref is None

    def test_fehlendes_sts_nutzt_die_stream_vorgabe(self) -> None:
        document = _minimal_camt(
            '<Amt Ccy="EUR">1.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-02-01</Dt></BookgDt>'
        )
        assert map_camt_transactions([document])[0].status == "gebucht"
        assert map_camt_transactions([], [document])[0].status == "vorgemerkt"

    def test_soll_und_haben_bei_vormerkungen(self) -> None:
        debit, credit = map_camt_transactions([], [_camt_pending()])
        assert debit.amount_cents == -999
        assert credit.amount_cents == 1500


class TestCamtFehler:
    """Fehlerfaelle der camt-Abbildung (N-DB-02, F-SYN-03)."""

    def test_unlesbares_xml_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_camt_transactions([b"<kein-xml"])

    def test_fehlender_betrag_wirft_mapping_fehler(self) -> None:
        document = _minimal_camt("<CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-02-01</Dt></BookgDt>")
        with pytest.raises(FinTSMappingError):
            map_camt_transactions([document])

    def test_fehlende_waehrung_wirft_mapping_fehler(self) -> None:
        document = _minimal_camt("<Amt>1.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-02-01</Dt></BookgDt>")
        with pytest.raises(FinTSMappingError):
            map_camt_transactions([document])

    def test_mehr_als_zwei_nachkommastellen_werden_abgelehnt(self) -> None:
        document = _minimal_camt(
            '<Amt Ccy="EUR">1.005</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-02-01</Dt></BookgDt>'
        )
        with pytest.raises(FinTSMappingError):
            map_camt_transactions([document])

    def test_leerer_stream_ergibt_keine_buchungen(self) -> None:
        document = "<Document><BkToCstmrStmt><Stmt></Stmt></BkToCstmrStmt></Document>"
        assert map_camt_transactions([document]) == []


class TestMt940Gebucht:
    """Gebuchte MT940-Buchungen aus dem Fallback (F-SYN-03, N-DB-02)."""

    def test_soll_und_haben_werden_uebernommen(self) -> None:
        transactions = map_mt940_transactions(_mt940_transactions())
        assert [t.amount_cents for t in transactions] == [-85000, 250000]
        assert all(isinstance(t.amount_cents, int) for t in transactions)

    def test_origin_und_status(self) -> None:
        transactions = map_mt940_transactions(_mt940_transactions())
        assert {t.origin for t in transactions} == {"fints"}
        assert {t.status for t in transactions} == {"gebucht"}

    def test_daten_gegenpartei_iban_und_zweck(self) -> None:
        debit, credit = map_mt940_transactions(_mt940_transactions())
        assert debit.booking_date == date(2026, 1, 7)
        assert debit.value_date == date(2026, 1, 5)
        assert debit.currency == "EUR"
        assert debit.counterparty == "Max Mustermann"
        assert debit.counterparty_iban == "DE89370400440532013000"
        assert normalize_field(debit.purpose) == "RECHNUNG 4711MIETE JANUAR"
        assert credit.counterparty == "Müller GmbH"
        assert credit.counterparty_iban == "DE02120300000000202051"
        assert normalize_field(credit.purpose) == "GEHALT DEZEMBERBEISPIEL GMBH"

    def test_nonref_wird_nicht_als_externe_referenz_uebernommen(self) -> None:
        transactions = map_mt940_transactions(_mt940_transactions())
        assert all(t.external_ref is None for t in transactions)

    def test_lange_und_mehrzeilige_zwecke_bleiben_vollstaendig(self) -> None:
        purpose = "ZAHLUNG RECHNUNG 2026-0001\n" + ("X" * 300)
        transaction = map_mt940_transactions(
            [
                {
                    "amount": Decimal("-1.00"),
                    "currency": "EUR",
                    "date": date(2026, 1, 2),
                    "entry_date": date(2026, 1, 2),
                    "purpose": purpose,
                }
            ]
        )[0]
        assert transaction.purpose == purpose
        assert len(transaction.purpose) > 280

    def test_externe_referenz_wird_uebernommen_wenn_geliefert(self) -> None:
        transaction = map_mt940_transactions(
            [
                {
                    "amount": Decimal("1.00"),
                    "currency": "EUR",
                    "date": date(2026, 1, 2),
                    "entry_date": date(2026, 1, 2),
                    "customer_reference": "KREF-4711",
                }
            ]
        )[0]
        assert transaction.external_ref == "KREF-4711"

    def test_fehlende_optionale_felder_bleiben_leer(self) -> None:
        transaction = map_mt940_transactions(
            [
                {
                    "amount": Decimal("1.00"),
                    "currency": "EUR",
                    "date": date(2026, 1, 2),
                    "entry_date": date(2026, 1, 2),
                    "customer_reference": "NONREF",
                }
            ]
        )[0]
        assert transaction.counterparty is None
        assert transaction.counterparty_iban is None
        assert transaction.purpose is None
        assert transaction.external_ref is None


class TestMt940Vorgemerkt:
    """Vormerkungen im MT940-Fallback (F-SYN-03, F-DUP-04)."""

    def _data(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "amount": Decimal("-2.50"),
            "currency": "EUR",
            "date": date(2026, 1, 8),
            "entry_date": date(2026, 1, 8),
        }
        data.update(overrides)
        return data

    def test_status_argument_markiert_alle_als_vorgemerkt(self) -> None:
        transactions = map_mt940_transactions([self._data(), self._data()], status="vorgemerkt")
        assert [t.status for t in transactions] == ["vorgemerkt", "vorgemerkt"]

    def test_explizites_pending_kennzeichen_gewinnt(self) -> None:
        transactions = map_mt940_transactions([self._data(pending=True)])
        assert transactions[0].status == "vorgemerkt"

    def test_pdng_status_gewinnt(self) -> None:
        transactions = map_mt940_transactions([self._data(status="PDNG")])
        assert transactions[0].status == "vorgemerkt"

    def test_fehlender_buchungstag_gilt_als_vorgemerkt(self) -> None:
        data = self._data()
        del data["entry_date"]
        transaction = map_mt940_transactions([data])[0]
        assert transaction.status == "vorgemerkt"
        assert transaction.booking_date == date(2026, 1, 8)

    def test_unbekannter_status_wird_abgelehnt(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions([self._data()], status="unbekannt")


class TestMt940Fehler:
    """Fehlerfaelle der MT940-Abbildung (N-DB-02)."""

    def test_fehlender_betrag_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions([{"currency": "EUR", "date": date(2026, 1, 2)}])

    def test_float_betraege_werden_abgelehnt(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions(
                [
                    {
                        "amount": 1.23,
                        "currency": "EUR",
                        "date": date(2026, 1, 2),
                    }
                ]
            )

    def test_fehlende_waehrung_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions([{"amount": Decimal("1.00"), "date": date(2026, 1, 2)}])

    def test_fehlendes_datum_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions([{"amount": Decimal("1.00"), "currency": "EUR"}])

    def test_mehr_als_zwei_nachkommastellen_werden_abgelehnt(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_mt940_transactions(
                [
                    {
                        "amount": Decimal("1.005"),
                        "currency": "EUR",
                        "date": date(2026, 1, 2),
                    }
                ]
            )


class TestParitaetCamtMt940:
    """Dieselbe Buchung aus camt und MT940 ergibt denselben Fingerprint (F-DUP-01)."""

    def test_identischer_fingerprint_je_buchung(self) -> None:
        camt = map_camt_transactions([_camt_booked()])[:2]
        mt940 = map_mt940_transactions(_mt940_transactions())
        assert len(camt) == len(mt940) == 2
        for camt_transaction, mt940_transaction in zip(camt, mt940, strict=True):
            assert _fingerprint(camt_transaction) == _fingerprint(mt940_transaction)
            assert camt_transaction.amount_cents == mt940_transaction.amount_cents
            assert camt_transaction.currency == mt940_transaction.currency
            assert camt_transaction.booking_date == mt940_transaction.booking_date
            assert camt_transaction.value_date == mt940_transaction.value_date
            assert camt_transaction.counterparty == mt940_transaction.counterparty
            assert camt_transaction.counterparty_iban == mt940_transaction.counterparty_iban
            assert normalize_field(camt_transaction.purpose) == normalize_field(mt940_transaction.purpose)

    def test_externe_referenz_und_status_gehen_nicht_in_den_fingerprint_ein(self) -> None:
        camt = map_camt_transactions([_camt_booked()])[0]
        changed = replace(camt, external_ref="ANDERE-REFERENZ", status="vorgemerkt")
        assert _fingerprint(camt) == _fingerprint(changed)


class TestBalance:
    """Saldo-Abbildung auf ``BalanceResult`` (F-SYN-03, N-DB-02)."""

    def test_haben_saldo_positiv_in_cent(self) -> None:
        result = map_balance(Balance("C", "1234,56", date(2026, 1, 6), currency="EUR"))
        assert result.amount_cents == 123456
        assert isinstance(result.amount_cents, int)
        assert result.currency == "EUR"
        assert result.as_of == date(2026, 1, 6)

    def test_soll_saldo_negativ_in_cent(self) -> None:
        result = map_balance(Balance("D", "12,34", date(2026, 1, 6), currency="EUR"))
        assert result.amount_cents == -1234

    def test_grosse_betraege_bleiben_exakt(self) -> None:
        result = map_balance(
            SimpleNamespace(amount=Decimal("1234567890123.45"), status="C"),
            fallback_currency="EUR",
        )
        assert result.amount_cents == 123456789012345

    def test_fallback_waehrung_wird_genutzt(self) -> None:
        result = map_balance(
            SimpleNamespace(amount=Decimal("5.00"), status="C"),
            fallback_currency="eur",
        )
        assert result.currency == "EUR"

    def test_fehlendes_datum_bleibt_leer(self) -> None:
        result = map_balance(
            SimpleNamespace(amount=Decimal("5.00"), status="C"),
            fallback_currency="EUR",
        )
        assert result.as_of is None

    def test_fehlender_betrag_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_balance(SimpleNamespace(status="C"))

    def test_fehlende_waehrung_wirft_mapping_fehler(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_balance(SimpleNamespace(amount=Decimal("5.00"), status="C"))

    def test_float_saldo_wird_abgelehnt(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_balance(SimpleNamespace(amount=1.23, status="C"))

    def test_mehr_als_zwei_nachkommastellen_werden_abgelehnt(self) -> None:
        with pytest.raises(FinTSMappingError):
            map_balance(SimpleNamespace(amount=Decimal("1.005"), status="C"))
