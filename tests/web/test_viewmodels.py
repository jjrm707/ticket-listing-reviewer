from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ticket_reviewer.web.viewmodels import (
    clean_text,
    EventEstimate,
    OpportunityCard,
    local_time,
    money_text,
    rate_text,
    roi_text,
    safe_error,
    sanitize_public_url,
)


def test_local_time_renders_correct_cst_and_cdt_labels():
    assert local_time(
        datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), "America/Chicago"
    ).endswith("12:00 PM CST")
    assert local_time(
        datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc), "America/Chicago"
    ).endswith("12:00 PM CDT")


def test_invalid_timezone_fails_closed():
    with pytest.raises(ValueError, match="invalid dashboard timezone"):
        local_time(datetime.now(timezone.utc), "Not/A_Real_Zone")


@pytest.mark.parametrize(
    ("url", "source", "expected"),
    [
        (
            "https://www.stubhub.com/path?token=secret#account",
            "stubhub",
            "https://www.stubhub.com/path",
        ),
        ("http://www.stubhub.com/path", "stubhub", None),
        ("https://user:pass@www.stubhub.com/path", "stubhub", None),
        ("https://www.stubhub.com:443/path", "stubhub", None),
        ("https://api.stubhub.com/path", "stubhub", None),
        ("https://127.0.0.1/path", "stubhub", None),
        ("https://www.ticketmaster.com/path", "stubhub", None),
    ],
)
def test_public_source_url_sanitization(url, source, expected):
    assert sanitize_public_url(url, source) == expected


def test_safe_error_strips_secret_names_values_markup_controls_and_bounds():
    secret = "private-value"
    output = safe_error(
        f"<script>bad</script>\x01 authentication failed Authorization=Bearer-{secret} "
        f"client_secret={secret} Cookie=session={secret}" + ("x" * 1000)
    )

    assert "authentication failed" in output
    assert secret not in output
    assert "Authorization" not in output
    assert "client_secret" not in output
    assert "Cookie" not in output
    assert "<" not in output
    assert len(output) <= 320


@pytest.mark.parametrize(
    "unsafe",
    [
        "Traceback (most recent call last): private internals",
        "sqlite:///C:/Users/private/dashboard.db",
        "mongodb://user:pass@host/private-db",
        r"connector failed at C:\\Users\\private\\secrets.txt",
        r"connector failed at \\server\share\private.db",
        "connector failed at /home/private/secrets.txt",
        "https://account:password@example.test/resource",
        'response payload={"private":"value"}',
        "Bearer private-token-without-a-header-name",
    ],
)
def test_safe_error_fails_closed_for_internal_or_structured_details(unsafe):
    assert safe_error(unsafe) == "unexpected connector error"


@pytest.mark.parametrize("formatter", [money_text, roi_text])
def test_numeric_formatters_fail_closed_for_huge_finite_decimals(formatter):
    assert formatter(Decimal("1E+1000")) == "Unavailable"


def test_rate_formatter_fails_closed_for_huge_finite_decimals():
    assert rate_text(Decimal("1E+1000")) == "Unavailable"


def test_display_text_and_urls_strip_or_reject_malformed_unicode():
    cleaned = clean_text("opponent\ud800name")

    assert cleaned == "opponent name"
    assert sanitize_public_url("https://www.stubhub.com/path/\ud800", "stubhub") is None
    cleaned.encode("utf-8")


def test_estimate_viewmodels_fail_closed_before_extreme_decimal_arithmetic():
    opportunity = SimpleNamespace(
        id=1,
        event_id=1,
        acquisition_total=Decimal("225.00"),
        projected_resale_gross=Decimal("1E+1000000"),
        exit_source="stubhub",
        seller_fee_rate=Decimal("0.15"),
        projected_proceeds=Decimal("1.00"),
        estimated_net_profit=Decimal("1E+1000000"),
        roi=Decimal("1E+1000000"),
        confidence="low",
        risk_reasons=[],
        status="new",
        scenarios=[],
    )
    event = SimpleNamespace(
        id=1,
        team="texans",
        opponent="Colts",
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
    )
    observation = SimpleNamespace(
        source="stubhub",
        section="101",
        row="12",
        quantity_available=2,
        observed_at=datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc),
        freshness_at=datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc),
        listing_url=None,
    )

    card = OpportunityCard.from_row(
        (opportunity, event, observation),
        "America/Chicago",
        now=datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc),
        freshness_minutes=120,
    )
    estimate = EventEstimate.from_row(opportunity)

    assert card.projected_resale_gross == "Unavailable"
    assert card.seller_fee_amount == "Unavailable"
    assert card.estimated_net_profit == "Unavailable"
    assert estimate.projected_resale_gross == "Unavailable"
    assert estimate.seller_fee_amount == "Unavailable"
    assert estimate.estimated_net_profit == "Unavailable"
