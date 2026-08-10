"""Immutable, display-only dashboard models with fail-closed validation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ticket_reviewer.domain.enums import Confidence, ObservationKind, OpportunityStatus, Source, Team
from ticket_reviewer.domain.matching import normalize_label


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_MARKUP = re.compile(r"<[^>]*>")
_SECRET = re.compile(
    r"(?i)(authorization|(?:api[_-]?key)|(?:access[_-]?token)|"
    r"(?:client[_-]?(?:id|secret))|password|token|(?:ntfy[_-]?topic)|"
    r"cookie|account[_-]?id|database[_-]?url)\s*[:=]\s*[^\s,;&]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|access[_-]?token|client[_-]?secret)=)[^&#\s]+"
)
_SENSITIVE_START = re.compile(
    r"(?i)\b(?:authorization|(?:api[_-]?key)|(?:access[_-]?token)|"
    r"(?:client[_-]?(?:id|secret))|password|token|(?:ntfy[_-]?topic)|"
    r"(?:set-)?cookie|account[_-]?id|database[_-]?url)\b"
)
_INTERNAL_ERROR = re.compile(
    r"(?i)(?:\btraceback\b|\b[a-z][a-z0-9+.-]*://|"
    r"\b[a-z]:[\\/]|(?:^|\s)/(?:users|home|var|tmp|etc|opt)/|"
    r"(?:^|\s)\\\\[^\s]+|\b(?:request|response)?\s*"
    r"(?:body|payload)\s*[:=])"
)
_BARE_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_STRUCTURED_ERROR = re.compile(r"[{}\[\]]")
_MAX_MONEY = Decimal("9999999999.99")
_MAX_ROI = Decimal("1000000")
_PUBLIC_HOSTS = {
    Source.STUBHUB: frozenset({"stubhub.com", "www.stubhub.com"}),
    Source.TICKETMASTER: frozenset({"ticketmaster.com", "www.ticketmaster.com"}),
    Source.SEATGEEK: frozenset({"seatgeek.com", "www.seatgeek.com"}),
    Source.TICKPICK: frozenset({"tickpick.com", "www.tickpick.com"}),
}


def clean_text(value: object, *, maximum: int = 255, fallback: str = "Unknown") -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = _CONTROL.sub(" ", value)
    cleaned = _SURROGATE.sub(" ", cleaned).replace("\ufffd", " ").strip()
    cleaned = " ".join(cleaned.split())
    return cleaned[:maximum] or fallback


def safe_error(value: object) -> str:
    if not isinstance(value, str):
        return "unexpected connector error"
    cleaned = _CONTROL.sub(" ", value)
    cleaned = _MARKUP.sub(" ", cleaned)
    if _INTERNAL_ERROR.search(cleaned) is not None:
        return "unexpected connector error"
    sensitive = _SENSITIVE_START.search(cleaned)
    if sensitive is not None:
        cleaned = cleaned[: sensitive.start()]
    if (
        _BARE_BEARER.search(cleaned) is not None
        or _STRUCTURED_ERROR.search(cleaned) is not None
    ):
        return "unexpected connector error"
    cleaned = _QUERY_SECRET.sub(r"\1[REDACTED]", cleaned)
    cleaned = _SECRET.sub("[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.split()).strip()[:320]
    cleaned = cleaned.replace("[REDACTED]", "")
    cleaned = re.sub(r"\s*[:=]\s*$", "", cleaned).strip()
    return cleaned or "unexpected connector error"


def strict_decimal(
    value: object,
    *,
    non_negative: bool = False,
    maximum: Decimal | None = None,
) -> Decimal | None:
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    if non_negative and value < 0:
        return None
    if maximum is not None and value.copy_abs() > maximum:
        return None
    return value


def money_text(value: object) -> str:
    parsed = strict_decimal(value, maximum=_MAX_MONEY)
    if parsed is None:
        return "Unavailable"
    try:
        return f"${parsed.quantize(Decimal('0.01')):,.2f}"
    except DecimalException:
        return "Unavailable"


def rate_text(value: object) -> str:
    parsed = strict_decimal(value, non_negative=True)
    if parsed is None or parsed > 1:
        return "Unavailable"
    try:
        return f"{(parsed * 100).quantize(Decimal('0.01'))}%"
    except DecimalException:
        return "Unavailable"


def roi_text(value: object) -> str:
    parsed = strict_decimal(value, maximum=_MAX_ROI)
    if parsed is None:
        return "Unavailable"
    try:
        return f"{(parsed * 100).quantize(Decimal('0.01'))}%"
    except DecimalException:
        return "Unavailable"


def fee_amount(gross_value: object, proceeds_value: object) -> Decimal | None:
    gross = strict_decimal(
        gross_value, non_negative=True, maximum=_MAX_MONEY
    )
    proceeds = strict_decimal(
        proceeds_value, non_negative=True, maximum=_MAX_MONEY
    )
    if gross is None or proceeds is None:
        return None
    try:
        amount = gross - proceeds
    except DecimalException:
        return None
    return strict_decimal(amount, non_negative=True, maximum=_MAX_MONEY)


def local_time(value: object, timezone_name: str) -> str:
    try:
        local_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("invalid dashboard timezone") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return "Unknown"
    return value.astimezone(local_zone).strftime("%b %d, %Y %I:%M %p %Z")


def utc_iso(value: object) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def source_label(value: Source | str) -> str:
    try:
        source = Source(value)
    except (TypeError, ValueError):
        return "Unknown source"
    return {
        Source.STUBHUB: "StubHub",
        Source.TICKETMASTER: "Ticketmaster",
        Source.SEATGEEK: "SeatGeek",
        Source.TICKPICK: "TickPick",
        Source.MANUAL: "Manual review",
    }[source]


def sanitize_public_url(value: object, source_value: object) -> str | None:
    if not isinstance(value, str) or _SURROGATE.search(value) is not None:
        return None
    try:
        source = Source(source_value)
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.hostname.lower() not in _PUBLIC_HOSTS.get(source, frozenset())
    ):
        return None
    return urlunsplit(("https", parsed.hostname.lower(), parsed.path or "/", "", ""))


def freshness_text(value: object, now: datetime, minutes: int) -> tuple[str, str]:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("dashboard clock must be timezone-aware")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return "Freshness unknown", "unknown"
    age = now - value
    if age.total_seconds() < -300:
        return "Freshness unknown", "unknown"
    age_minutes = max(0, int(age.total_seconds() // 60))
    if age_minutes > minutes:
        return f"Stale — last seen {age_minutes} minutes ago", "stale"
    return f"Fresh — last seen {age_minutes} minutes ago", "fresh"


@dataclass(frozen=True, slots=True)
class OpportunityCard:
    id: int
    event_id: int
    team: str
    opponent: str
    venue: str
    kickoff: str
    candidate_source: str
    section: str
    row: str
    quantity: str
    acquisition_total: str
    projected_resale_gross: str
    exit_source: str
    seller_fee_rate: str
    seller_fee_amount: str
    projected_proceeds: str
    estimated_net_profit: str
    profit_sort: Decimal | None
    roi: str
    confidence: str
    confidence_label: str
    risk_reasons: tuple[str, ...]
    status: str
    first_seen: str
    last_seen: str
    freshness: str
    freshness_state: str
    source_url: str | None

    @classmethod
    def from_row(
        cls,
        row: object,
        timezone_name: str,
        *,
        now: datetime,
        freshness_minutes: int,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
    ) -> "OpportunityCard":
        opportunity, event, observation = row
        if type(opportunity.id) is not int or opportunity.id <= 0:
            raise ValueError("invalid opportunity identifier")
        if type(event.id) is not int or event.id <= 0:
            raise ValueError("invalid event identifier")
        try:
            team = Team(event.team)
            confidence = Confidence(opportunity.confidence)
            status = OpportunityStatus(opportunity.status)
            source = Source(observation.source)
        except (TypeError, ValueError):
            raise ValueError("invalid opportunity enum") from None
        risks = opportunity.risk_reasons
        if not isinstance(risks, list):
            risks = ["Historical risk details unavailable"]
        safe_risks = tuple(
            clean_text(item, maximum=240, fallback="Historical risk detail unavailable")
            for item in risks[:24]
        )
        freshness, freshness_state = freshness_text(
            observation.freshness_at, now, freshness_minutes
        )
        gross = strict_decimal(
            opportunity.projected_resale_gross,
            non_negative=True,
            maximum=_MAX_MONEY,
        )
        proceeds = strict_decimal(
            opportunity.projected_proceeds,
            non_negative=True,
            maximum=_MAX_MONEY,
        )
        fee = fee_amount(gross, proceeds)
        profit = strict_decimal(
            opportunity.estimated_net_profit, maximum=_MAX_MONEY
        )
        return cls(
            id=opportunity.id,
            event_id=event.id,
            team="Houston Texans" if team is Team.TEXANS else "Texas A&M Aggies",
            opponent=clean_text(event.opponent),
            venue=clean_text(event.venue),
            kickoff=local_time(event.starts_at, timezone_name),
            candidate_source=source_label(source),
            section=clean_text(observation.section, maximum=128, fallback="Unknown"),
            row=clean_text(observation.row, maximum=128, fallback="Unknown"),
            quantity=str(observation.quantity_available)
            if type(observation.quantity_available) is int and observation.quantity_available > 0
            else "Unknown",
            acquisition_total=money_text(opportunity.acquisition_total),
            projected_resale_gross=money_text(gross),
            exit_source=source_label(opportunity.exit_source)
            if opportunity.exit_source is not None
            else "Unavailable",
            seller_fee_rate=rate_text(opportunity.seller_fee_rate),
            seller_fee_amount=money_text(fee),
            projected_proceeds=money_text(proceeds),
            estimated_net_profit=money_text(profit),
            profit_sort=profit,
            roi=roi_text(opportunity.roi),
            confidence=confidence.value,
            confidence_label=f"{confidence.value.title()} confidence",
            risk_reasons=safe_risks,
            status=status.value.title(),
            first_seen=local_time(first_seen_at or observation.observed_at, timezone_name),
            last_seen=local_time(last_seen_at or observation.freshness_at, timezone_name),
            freshness=freshness,
            freshness_state=freshness_state,
            source_url=sanitize_public_url(observation.listing_url, source),
        )


@dataclass(frozen=True, slots=True)
class EventEstimate:
    id: int
    acquisition_total: str
    projected_resale_gross: str
    exit_source: str
    seller_fee_rate: str
    seller_fee_amount: str
    projected_proceeds: str
    estimated_net_profit: str
    roi: str
    confidence_label: str
    risk_reasons: tuple[str, ...]
    comparables: tuple[str, ...]

    @classmethod
    def from_row(cls, row: object) -> "EventEstimate":
        if type(row.id) is not int or row.id <= 0:
            raise ValueError("invalid estimate identifier")
        try:
            confidence = Confidence(row.confidence)
        except (TypeError, ValueError):
            raise ValueError("invalid estimate confidence") from None
        gross = strict_decimal(
            row.projected_resale_gross,
            non_negative=True,
            maximum=_MAX_MONEY,
        )
        proceeds = strict_decimal(
            row.projected_proceeds,
            non_negative=True,
            maximum=_MAX_MONEY,
        )
        fee = fee_amount(gross, proceeds)
        risks = row.risk_reasons if isinstance(row.risk_reasons, list) else []
        safe_risks = tuple(
            clean_text(item, maximum=240, fallback="Historical risk detail unavailable")
            for item in risks[:24]
        ) or ("No scoring adjustments recorded",)
        return cls(
            id=row.id,
            acquisition_total=money_text(row.acquisition_total),
            projected_resale_gross=money_text(gross),
            exit_source=source_label(row.exit_source)
            if row.exit_source is not None
            else "Unavailable",
            seller_fee_rate=rate_text(row.seller_fee_rate),
            seller_fee_amount=money_text(fee),
            projected_proceeds=money_text(proceeds),
            estimated_net_profit=money_text(row.estimated_net_profit),
            roi=roi_text(row.roi),
            confidence_label=f"{confidence.value.title()} confidence",
            risk_reasons=safe_risks,
            comparables=exact_comparable_text(row.scenarios),
        )


def observation_kind_text(kind_value: object) -> str:
    try:
        kind = ObservationKind(kind_value)
    except (TypeError, ValueError):
        return "Unknown evidence"
    if kind is ObservationKind.LISTING:
        return "Confirmed pair listing"
    if kind is ObservationKind.EVENT_FLOOR:
        return "Event floor — event-level signal; pair not confirmed"
    return "Event aggregate — evidence only; pair not confirmed"


def seating_key(row: object) -> str:
    try:
        kind = ObservationKind(row.kind)
    except (TypeError, ValueError):
        return "unknown evidence"
    if kind is not ObservationKind.LISTING:
        return "event-level signal"
    section = normalize_label(row.section) if isinstance(row.section, str) else ""
    seat_row = normalize_label(row.row) if isinstance(row.row, str) else ""
    if section and seat_row:
        return f"section {section}, row {seat_row}"
    if section:
        return f"section {section}, row unknown"
    return "unknown seat"


def exact_comparable_text(scenarios: object) -> tuple[str, ...]:
    if not isinstance(scenarios, list):
        return ("Exact comparable IDs were not recorded for this earlier estimate",)
    lines: list[str] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        ids = scenario.get("comparable_observation_ids")
        if not isinstance(ids, list):
            continue
        if any(type(value) is not int or value <= 0 for value in ids):
            continue
        marketplace = source_label(scenario.get("marketplace"))
        lines.append(
            f"{marketplace}: observation IDs {', '.join(str(value) for value in ids)}"
            if ids
            else f"{marketplace}: no usable comparable observations"
        )
    return tuple(lines) or (
        "Exact comparable IDs were not recorded for this earlier estimate",
    )
