"""Erzeugt synthetische Demo-Daten für die lokale Entwicklung.

Implementiert N-OPS-01 (Demo-Daten per Befehl für die lokale Entwicklung) und
den vorläufigen Fingerprint nach F-DUP-01 (SHA-256 über normalisierte Felder,
in M2 durch den finalen Dubletten-Mechanismus ersetzt).

Alle hier erzeugten Daten sind REIN ERFUNDEN. Es werden ausschließlich
offensichtlich fiktive Namen (z. B. "Max Mustermann", "Musterfirma GmbH"),
eine fiktive Test-Bankleitzahl (99999999) sowie öffentlich dokumentierte
IBAN-Testnummern verwendet. Es sind keine echten Personen, Institute oder
Konten zuordenbar.

`seed_demo` legt den Datenbestand deterministisch an (fester Zufallsseed),
`clear_demo` entfernt ihn wieder vollständig, damit `seed_demo` erneut
ausgeführt werden kann, ohne die Eindeutigkeitsbedingungen zu verletzen.
"""

import hashlib
import random
import re
import unicodedata
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.bank import Bank, BankAccess
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction
from finue.timeutil import utc_now

# Fiktive Stammdaten (rein erfunden, nicht auf echte Institute beziehbar).
DEMO_BANK_BLZ = "99999999"
DEMO_BANK_NAME = "Demo Musterbank"
DEMO_BANK_WEBSITE = "https://musterbank.example"
DEMO_BANK_LOGIN = "demo-user"
DEMO_GROUP_NAME = "Demo-Gruppe"
DEMO_ACCOUNT_NAMES = ("Demo Girokonto", "Demo Tagesgeld", "Demo Datei-Import")

# Öffentlich dokumentierte IBAN-Testnummern (keine echten Konten).
DEMO_IBANS = (
    "DE02120300000000202051",
    "DE89370400440532013000",
    "DE02200505501015871393",
)

# Rein erfundene Gegenparteien und Verwendungszwecke.
DEMO_COUNTERPARTIES = (
    "Max Mustermann",
    "Erika Musterfrau",
    "Musterfirma GmbH",
    "Beispiel Handel AG",
    "Test-Energie GmbH",
    "Stadtwerke Musterstadt",
    "Fiktiv Versicherung AG",
    "Demo Telekom GmbH",
    "Muster Shop",
    "Beispiel Bahn AG",
    "Musterbank Musterstadt",
)

DEMO_PURPOSES = (
    "Lebensmitteleinkauf",
    "Gehalt",
    "Stromabschlag",
    "Miete",
    "Versicherungsbeitrag",
    "Mobilfunk",
    "Onlineshop-Bestellung",
    "Bahnticket",
    "Geldautomat",
    "Dauerauftrag Miete",
)

# Zielgröße: ca. 200 Buchungen.
DEMO_TRANSACTION_COUNT = 200
DEMO_BASE_DATE = date(2024, 1, 1)


def _normalize_for_fingerprint(value: str | None) -> str:
    """Normalisiert Text für den Fingerprint nach F-DUP-01.

    Unicode NFC, Zeilenumbrüche entfernen, Mehrfach-Leerzeichen zusammenfassen,
    trimmen und in Großbuchstaben wandeln.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().upper()


def _fingerprint(
    account_id: int,
    booking_date: date,
    value_date: date | None,
    amount_cents: int,
    currency: str,
    counterparty: str | None,
    counterparty_iban: str | None,
    purpose: str | None,
) -> str:
    """Vorläufiger SHA-256-Fingerprint über die relevanten Buchungsfelder.

    Dies ist der vorläufige Fingerprint (F-DUP-01); in M2 wird er durch den
    finalen Dubletten-Mechanismus ersetzt. Die Feldauswahl entspricht F-DUP-01:
    Konto, Buchungstag, Wertstellung, Betrag, Währung, Gegenpartei,
    Gegenpartei-IBAN und Verwendungszweck.
    """
    fields = (
        str(account_id),
        booking_date.isoformat(),
        value_date.isoformat() if value_date is not None else "",
        str(amount_cents),
        currency,
        _normalize_for_fingerprint(counterparty),
        _normalize_for_fingerprint(counterparty_iban),
        _normalize_for_fingerprint(purpose),
    )
    payload = "\x1f".join(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clear_demo(session: Session) -> None:
    """Entfernt alle durch `seed_demo` angelegten Demo-Daten.

    Macht `seed_demo` wiederholbar, ohne die Eindeutigkeitsbedingungen von
    `transaction` oder `account` zu verletzen. Es werden ausschließlich die
    anhand fester Demo-Merkmale erkennbaren Datensätze entfernt, keine
    Fremddaten. Kein rohes SQL (N-DB-01).
    """
    account_ids = list(
        session.scalars(select(Account.id).where(Account.name.in_(DEMO_ACCOUNT_NAMES)))
    )
    if account_ids:
        session.execute(delete(Transaction).where(Transaction.account_id.in_(account_ids)))
        session.execute(delete(StandingOrder).where(StandingOrder.account_id.in_(account_ids)))
        session.execute(delete(Account).where(Account.id.in_(account_ids)))

    bank_ids = list(session.scalars(select(Bank.id).where(Bank.blz == DEMO_BANK_BLZ)))
    if bank_ids:
        session.execute(delete(BankAccess).where(BankAccess.bank_id.in_(bank_ids)))
        session.execute(delete(Bank).where(Bank.id.in_(bank_ids)))

    session.execute(delete(AccountGroup).where(AccountGroup.name == DEMO_GROUP_NAME))
    session.commit()


def seed_demo(session: Session, seed: int = 42) -> None:
    """Legt deterministische synthetische Demo-Daten an.

    Erzeugt 1 Bank mit 1 Bankzugang, 3 Konten (2 FinTS-Konten, 1 Datei-Konto
    ohne Bankzugang), 1 Kontengruppe, ca. 200 Buchungen (darunter vorgemerkte,
    geprüfte und abgestimmte sowie ein Paar identischer Buchungen am selben Tag
    mit occurrence_index 0 und 1) und 2 Aufträge (Dauer- und Terminauftrag).

    Args:
        session: SQLAlchemy-Session.
        seed: Zufallsseed für den reproduzierbaren Generator (Standard 42).

    Hinweis: Alle Daten sind rein erfunden (siehe Modul-Docstring).
    """
    rng = random.Random(seed)

    # Vorkommens-Index je (Konto, Fingerprint) als Multiset gemäß F-DUP-02.
    counters: dict[tuple[int, str], int] = {}

    def reserve_index(account_id: int, fingerprint: str, start: int) -> int:
        """Reserviert den nächsten Vorkommens-Index für (Konto, Fingerprint)."""
        key = (account_id, fingerprint)
        index = counters.get(key, start)
        counters[key] = index + 1
        return index

    # Bank und Bankzugang (nur für FinTS-Konten). Keine PIN im Klartext (N-SEC-01).
    bank = Bank(blz=DEMO_BANK_BLZ, name=DEMO_BANK_NAME, website=DEMO_BANK_WEBSITE)
    session.add(bank)
    session.flush()
    bank_access = BankAccess(
        bank_id=bank.id,
        login_name=DEMO_BANK_LOGIN,
        pin_encrypted=None,
        tan_method=None,
    )
    session.add(bank_access)
    session.flush()

    group = AccountGroup(name=DEMO_GROUP_NAME, sort_order=0)
    session.add(group)
    session.flush()

    giro = Account(
        name=DEMO_ACCOUNT_NAMES[0],
        iban=DEMO_IBANS[0],
        currency="EUR",
        source="fints",
        account_type="Girokonto",
        include_in_total=True,
        bank_access_id=bank_access.id,
        group_id=group.id,
        opening_balance_cents=150_000,
    )
    tagesgeld = Account(
        name=DEMO_ACCOUNT_NAMES[1],
        iban=DEMO_IBANS[1],
        currency="EUR",
        source="fints",
        account_type="Tagesgeld",
        include_in_total=True,
        bank_access_id=bank_access.id,
        group_id=group.id,
        opening_balance_cents=500_000,
    )
    datei_konto = Account(
        name=DEMO_ACCOUNT_NAMES[2],
        iban=DEMO_IBANS[2],
        currency="EUR",
        source="datei",
        account_type="Importkonto",
        include_in_total=False,
        bank_access_id=None,
        group_id=group.id,
        opening_balance_cents=0,
    )
    demo_accounts = [giro, tagesgeld, datei_konto]
    session.add_all(demo_accounts)
    session.flush()

    account_ids = [account.id for account in demo_accounts]
    totals: dict[int, int] = {account_id: 0 for account_id in account_ids}

    def add_transaction(transaction: Transaction) -> None:
        """Fügt eine Buchung hinzu und summiert ihren Betrag je Konto."""
        session.add(transaction)
        totals[transaction.account_id] += transaction.amount_cents

    # Duplikat-Testfall: zwei identische Buchungen am selben Tag auf dem
    # Girokonto mit occurrence_index 0 und 1 (F-DUP-02). Zuerst anlegen, damit
    # der Vorkommens-Zähler anschließend bei 2 weiterläuft.
    pair_date = date(2024, 3, 15)
    pair_amount = -4_999
    pair_cpf = "Max Mustermann"
    pair_iban = DEMO_IBANS[0]
    pair_purpose = "Doppelbuchung Demo"
    pair_fingerprint = _fingerprint(
        account_ids[0],
        pair_date,
        pair_date,
        pair_amount,
        "EUR",
        pair_cpf,
        pair_iban,
        pair_purpose,
    )
    for offset in (0, 1):
        add_transaction(
            Transaction(
                account_id=account_ids[0],
                status="gebucht",
                booking_date=pair_date,
                value_date=pair_date,
                amount_cents=pair_amount,
                currency="EUR",
                counterparty=pair_cpf,
                counterparty_iban=pair_iban,
                purpose=pair_purpose,
                search_text=f"{pair_cpf} {pair_purpose}".lower(),
                origin="fints",
                external_ref=f"demo-duplikat-{offset}",
                fingerprint=pair_fingerprint,
                occurrence_index=reserve_index(account_ids[0], pair_fingerprint, 0),
            )
        )

    normal_count = DEMO_TRANSACTION_COUNT - 2
    for i in range(normal_count):
        account_index = i % len(demo_accounts)
        account = demo_accounts[account_index]
        account_id = account_ids[account_index]

        booking_date = DEMO_BASE_DATE + timedelta(days=rng.randrange(0, 365))
        value_date = booking_date + timedelta(days=1)
        amount_cents = rng.randint(-40_000, 260_000)
        counterparty = rng.choice(DEMO_COUNTERPARTIES)
        purpose = rng.choice(DEMO_PURPOSES)
        counterparty_iban = rng.choice(DEMO_IBANS) if rng.random() < 0.6 else None
        origin = "fints" if account.source == "fints" else rng.choice(("csv", "json"))

        pending = i % 15 == 0
        status = "vorgemerkt" if pending else "gebucht"
        checked = not pending and i % 4 == 0
        reconciled = checked and i % 8 == 0

        fingerprint = _fingerprint(
            account_id,
            booking_date,
            value_date,
            amount_cents,
            "EUR",
            counterparty,
            counterparty_iban,
            purpose,
        )
        add_transaction(
            Transaction(
                account_id=account_id,
                status=status,
                booking_date=booking_date,
                value_date=value_date,
                amount_cents=amount_cents,
                currency="EUR",
                counterparty=counterparty,
                counterparty_iban=counterparty_iban,
                purpose=purpose,
                search_text=f"{counterparty} {purpose}".lower(),
                note="Demo-Notiz" if i % 20 == 0 else None,
                checked=checked,
                checked_at=utc_now() if checked else None,
                reconciled=reconciled,
                reconciled_at=utc_now() if reconciled else None,
                origin=origin,
                external_ref=f"demo-{i:04d}",
                fingerprint=fingerprint,
                occurrence_index=reserve_index(account_id, fingerprint, 1),
            )
        )

    # Saldo je Konto fortschreiben.
    for account in demo_accounts:
        account.balance_cents = account.opening_balance_cents + totals[account.id]

    # 2 Aufträge: ein Dauerauftrag und ein Terminauftrag (F-AUF-02).
    session.add(
        StandingOrder(
            account_id=account_ids[0],
            kind="dauerauftrag",
            counterparty="Musterfirma GmbH",
            counterparty_iban=DEMO_IBANS[0],
            purpose="Miete Demo",
            amount_cents=95_000,
            currency="EUR",
            interval="monatlich",
            next_execution=date(2024, 4, 1),
        )
    )
    session.add(
        StandingOrder(
            account_id=account_ids[0],
            kind="terminauftrag",
            counterparty="Beispiel Handel AG",
            counterparty_iban=DEMO_IBANS[1],
            purpose="Einmalzahlung Demo",
            amount_cents=12_500,
            currency="EUR",
            interval=None,
            next_execution=date(2024, 5, 15),
        )
    )

    session.commit()
