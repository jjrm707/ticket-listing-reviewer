from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from ticket_reviewer.services.ocr import parse_listing_text


def test_parses_pair_listing_text():
    draft = parse_listing_text(
        "Houston Texans vs Colts\nSection 123 Row G\n2 tickets\n"
        "$110 each\nFees $24\nTotal $244"
    )

    assert draft.event == "Houston Texans vs Colts"
    assert draft.team == "texans"
    assert draft.opponent == "Colts"
    assert draft.section == "123"
    assert draft.row == "G"
    assert draft.quantity == 2
    assert draft.per_ticket_price == Decimal("110.00")
    assert draft.fees == Decimal("24.00")
    assert draft.total == Decimal("244.00")


def test_parser_does_not_guess_missing_total():
    draft = parse_listing_text("Aggies vs Texas\nSection 401\n$175")

    assert draft.total is None
    assert "total cost" in draft.missing_fields
    assert draft.quantity is None
    assert draft.warnings


def test_common_ocr_spacing_and_case_remain_cent_exact():
    draft = parse_listing_text(
        "TEXAS A & M VS. LSU\nSECTION : 401 ROW : 12\n"
        "2 TICKETS\nPRICE EACH $ 1,234.56\nTAX $ 4.25\nTOTAL $400.00"
    )

    assert draft.team == "aggies"
    assert draft.opponent == "LSU"
    assert draft.per_ticket_price == Decimal("1234.56")
    assert draft.tax == Decimal("4.25")
    assert draft.total == Decimal("400.00")


@pytest.mark.parametrize(
    "text",
    [
        "Total -$1.00",
        "Total $0",
        "Total $1e2",
        "Total NaN",
        "Total Infinity",
        "Total $10000000000.00",
        "Total from $150",
        "Starting at $150 total",
        "4 payments of $50 Total",
        "~~Total $250~~",
    ],
)
def test_rejects_unsafe_or_non_authoritative_totals(text):
    draft = parse_listing_text(f"Texans vs Colts\n2 tickets\n{text}")

    assert draft.total is None
    assert "total cost" in draft.missing_fields
    assert draft.warnings


def test_conflicting_labels_and_quantity_range_fail_closed():
    draft = parse_listing_text(
        "Texans vs Colts\nSection 101\nSection 102\n2-4 tickets\n"
        "Total $300\nTotal $350"
    )

    assert draft.section is None
    assert draft.quantity is None
    assert draft.total is None
    assert len(draft.warnings) == len(set(draft.warnings))


@pytest.mark.parametrize(
    "malformed",
    ["Total -$99", "Total from $99", "Total $999999999999"],
)
def test_later_malformed_total_invalidates_an_earlier_valid_total(malformed):
    draft = parse_listing_text(
        f"Texans vs Colts\n2 tickets\nTotal $244\n{malformed}"
    )

    assert draft.total is None
    assert "total cost" in draft.missing_fields
    assert draft.warnings


def test_incomplete_kickoff_is_not_presented_as_a_confirmable_candidate():
    draft = parse_listing_text(
        "Texans vs Colts\nKickoff: Sep 13\n2 tickets\nTotal $244"
    )

    assert draft.kickoff_text is None
    assert "kickoff with timezone offset" in draft.missing_fields
    assert draft.warnings


@pytest.mark.parametrize(
    "title",
    [
        "Cowboys vs Eagles",
        "Texans at Colts",
        "Texans vs Colts - London neutral site",
        "Texans vs Colts parking pass",
    ],
)
def test_rejects_unsupported_away_neutral_and_non_ticket_products(title):
    draft = parse_listing_text(f"{title}\n2 tickets\nTotal $250")

    assert draft.team is None
    assert draft.opponent is None
    assert "supported home event" in draft.missing_fields
    assert draft.warnings


def test_output_is_immutable_bounded_and_does_not_leak_raw_input():
    secret = "client_secret=do-not-echo"
    draft = parse_listing_text(("\x00\ud800" + secret + "\n") * 5000)

    with pytest.raises(FrozenInstanceError):
        draft.total = Decimal("1.00")
    public = " ".join((*draft.missing_fields, *draft.warnings))
    assert secret not in public
    assert "client_secret" not in public
    assert len(draft.missing_fields) <= 12
    assert len(draft.warnings) <= 12
    assert all(len(message) <= 120 for message in (*draft.missing_fields, *draft.warnings))


def test_unicode_format_controls_are_removed_from_candidates():
    draft = parse_listing_text(
        "Texans vs Co\u202elts\n2 tickets\nTotal $244"
    )

    assert draft.opponent == "Colts"
    assert "\u202e" not in (draft.event or "")
