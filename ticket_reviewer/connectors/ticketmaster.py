"""Official public Ticketmaster Discovery API adapter."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import (
    Capability,
    ConnectorFailure,
    FailureCategory,
)
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.domain.matching import event_match_score
from ticket_reviewer.services.retry import call_with_retry
from ticket_reviewer.services.secure_logging import install_http_log_redaction


_SEARCH_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
_DETAIL_URL = "https://app.ticketmaster.com/discovery/v2/events/{event_id}.json"
_MAX_EXTERNAL_ID_BYTES = 256
_CHICAGO = ZoneInfo("America/Chicago")
_HOME_NAME = re.compile(r"^\s*Houston\s+Texans\s+vs\.?\s+(.+?)\s*$", re.IGNORECASE)
_PARKING_PRODUCT_TOKENS = frozenset(
    {
        "parking",
        "parkings",
        "tailgate",
        "tailgates",
        "tailgated",
        "tailgating",
        "pass",
        "passes",
    }
)
_VENUE_ALIASES = frozenset({"nrg stadium", "reliant stadium"})
# Public event links may use only these exact HTTPS hosts.
_PUBLIC_EVENT_HOSTS = frozenset({"ticketmaster.com", "www.ticketmaster.com"})
_NFL_OPPONENTS = {
    "Arizona Cardinals": "Cardinals",
    "Atlanta Falcons": "Falcons",
    "Baltimore Ravens": "Ravens",
    "Buffalo Bills": "Bills",
    "Carolina Panthers": "Panthers",
    "Chicago Bears": "Bears",
    "Cincinnati Bengals": "Bengals",
    "Cleveland Browns": "Browns",
    "Dallas Cowboys": "Cowboys",
    "Denver Broncos": "Broncos",
    "Detroit Lions": "Lions",
    "Green Bay Packers": "Packers",
    "Indianapolis Colts": "Colts",
    "Jacksonville Jaguars": "Jaguars",
    "Kansas City Chiefs": "Chiefs",
    "Las Vegas Raiders": "Raiders",
    "Los Angeles Chargers": "Chargers",
    "Los Angeles Rams": "Rams",
    "Miami Dolphins": "Dolphins",
    "Minnesota Vikings": "Vikings",
    "New England Patriots": "Patriots",
    "New Orleans Saints": "Saints",
    "New York Giants": "Giants",
    "New York Jets": "Jets",
    "Philadelphia Eagles": "Eagles",
    "Pittsburgh Steelers": "Steelers",
    "San Francisco 49ers": "49ers",
    "Seattle Seahawks": "Seahawks",
    "Tampa Bay Buccaneers": "Buccaneers",
    "Tennessee Titans": "Titans",
    "Washington Commanders": "Commanders",
}
_AUTH_MESSAGE = "Ticketmaster authentication failed"
_RATE_MESSAGE = "Ticketmaster rate limit reached"
_NETWORK_MESSAGE = "Ticketmaster is temporarily unavailable"
_PARSE_MESSAGE = "Ticketmaster response could not be parsed"
_DETAIL_START_TOLERANCE = timedelta(minutes=5)


class TicketmasterConnector:
    """Read Texans event and event-floor evidence from Discovery."""

    source = Source.TICKETMASTER
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client,
        *,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], datetime] | None = None,
        owns_client: bool = False,
    ) -> None:
        install_http_log_redaction()
        secret = settings.ticketmaster_api_key
        api_key = secret.get_secret_value().strip() if secret is not None else ""
        if not api_key:
            raise ValueError("Ticketmaster Discovery API key is required")
        self._api_key = api_key
        self._client = client
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._owns_client = owns_client
        self._closed = False

    @property
    def client(self) -> httpx.Client:
        """Return the HTTP client for lifecycle inspection by the composition root."""
        return self._client

    def discover(
        self, team: Team, starts_after: datetime, starts_before: datetime
    ) -> list[ExternalEvent]:
        after, before = _normalize_window(starts_after, starts_before)
        if team is Team.AGGIES:
            return []
        if team is not Team.TEXANS:
            return []

        payload = self._get_json(
            _SEARCH_URL,
            {
                "apikey": self._api_key,
                "keyword": "Houston Texans",
                "countryCode": "US",
                "startDateTime": _utc_parameter(after),
                "endDateTime": _utc_parameter(before),
                "size": "100",
            },
        )
        raw_events = _discovery_events(payload)
        accepted: dict[str, ExternalEvent] = {}
        for raw_event in raw_events:
            event = _parse_event(raw_event)
            if event is None or not after <= event.starts_at < before:
                continue
            accepted.setdefault(event.external_id, event)
        return sorted(
            accepted.values(), key=lambda event: (event.starts_at, event.external_id)
        )

    def fetch_observations(self, event: ExternalEvent) -> list[SourceObservation]:
        if event.source is not Source.TICKETMASTER:
            raise ValueError("event must come from Ticketmaster")
        event_id = _validated_external_id(event.external_id)
        if event_id is None:
            raise _parse_failure()

        url = _DETAIL_URL.format(event_id=quote(event_id, safe=""))
        payload = self._get_json(url, {"apikey": self._api_key})
        if not isinstance(payload, dict):
            raise _parse_failure()
        returned = _parse_event(payload)
        if (
            returned is None
            or returned.external_id != event_id
            or abs(returned.starts_at - event.starts_at) > _DETAIL_START_TOLERANCE
            or event_match_score(returned, event) < Decimal("0.85")
        ):
            raise _parse_failure()
        price_ranges = payload.get("priceRanges")
        if price_ranges is None:
            return []
        if not isinstance(price_ranges, list):
            raise _parse_failure()

        minimum = _lowest_usd_minimum(price_ranges)
        if minimum is None:
            return []
        try:
            pair_price = (minimum * 2).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except DecimalException:
            return []
        if pair_price <= 0:
            return []
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        observed_at = observed_at.astimezone(timezone.utc)
        public_url = _public_event_url(payload.get("url"))
        if public_url is None:
            public_url = _public_event_url(event.url)
        return [
            SourceObservation(
                source=Source.TICKETMASTER,
                event_external_id=event_id,
                observed_at=observed_at,
                kind=ObservationKind.EVENT_FLOOR,
                currency="USD",
                pair_price=pair_price,
                buyer_fees=None,
                estimated_tax=None,
                section=None,
                row=None,
                quantity_available=None,
                can_buy_pair=None,
                listing_id=None,
                listing_url=public_url,
            )
        ]

    def close(self) -> None:
        """Close a bootstrap-owned client; injected clients remain caller-owned."""
        if self._owns_client and not self._closed:
            self._client.close()
            self._closed = True

    def _get_json(self, url: str, params: dict[str, str]) -> Any:
        def request() -> Any:
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError:
                raise ConnectorFailure(
                    self.source,
                    FailureCategory.NETWORK,
                    _NETWORK_MESSAGE,
                    retryable=True,
                ) from None
            _raise_for_status(response.status_code)
            try:
                return response.json()
            except (ValueError, TypeError):
                raise _parse_failure() from None

        return call_with_retry(request, self._sleep)


def _normalize_window(
    starts_after: datetime, starts_before: datetime
) -> tuple[datetime, datetime]:
    if (
        starts_after.tzinfo is None
        or starts_after.utcoffset() is None
        or starts_before.tzinfo is None
        or starts_before.utcoffset() is None
    ):
        raise ValueError("time window must use timezone-aware datetimes")
    after = starts_after.astimezone(timezone.utc)
    before = starts_before.astimezone(timezone.utc)
    if after >= before:
        raise ValueError("time window start must be before end")
    return after, before


def _utc_parameter(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _raise_for_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise ConnectorFailure(
            Source.TICKETMASTER,
            FailureCategory.AUTH,
            _AUTH_MESSAGE,
            retryable=False,
        )
    if status_code == 429:
        raise ConnectorFailure(
            Source.TICKETMASTER,
            FailureCategory.RATE_LIMIT,
            _RATE_MESSAGE,
            retryable=True,
        )
    if status_code >= 500:
        raise ConnectorFailure(
            Source.TICKETMASTER,
            FailureCategory.NETWORK,
            _NETWORK_MESSAGE,
            retryable=True,
        )
    raise _parse_failure()


def _parse_failure() -> ConnectorFailure:
    return ConnectorFailure(
        Source.TICKETMASTER,
        FailureCategory.PARSE,
        _PARSE_MESSAGE,
        retryable=False,
    )


def _discovery_events(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise _parse_failure()
    embedded = payload.get("_embedded")
    if embedded is None:
        return []
    if not isinstance(embedded, dict):
        raise _parse_failure()
    events = embedded.get("events", [])
    if not isinstance(events, list):
        raise _parse_failure()
    return events


def _parse_event(raw: Any) -> ExternalEvent | None:
    if not isinstance(raw, dict):
        return None
    event_id = _validated_external_id(raw.get("id"))
    name = raw.get("name")
    if event_id is None:
        return None
    if not isinstance(name, str):
        return None
    match = _HOME_NAME.fullmatch(name)
    if match is None or _has_parking_signal(raw):
        return None

    embedded = raw.get("_embedded")
    if not isinstance(embedded, dict):
        return None
    attraction_names = _embedded_names(embedded.get("attractions"))
    venue_names = _embedded_names(embedded.get("venues"))
    venue = next(
        (value.strip() for value in venue_names if _normalize_text(value) in _VENUE_ALIASES),
        None,
    )
    if venue is None:
        return None
    starts_at = _parse_start(raw.get("dates"))
    if starts_at is None:
        return None

    opponent_name = next(
        (
            value.strip()
            for value in attraction_names
            if _normalize_text(value) != "houston texans"
        ),
        match.group(1).strip(),
    )
    opponent = _NFL_OPPONENTS.get(opponent_name, opponent_name)
    if not opponent:
        return None
    public_url = _public_event_url(raw.get("url"))
    return ExternalEvent(
        source=Source.TICKETMASTER,
        external_id=event_id,
        team=Team.TEXANS,
        opponent=opponent,
        venue=venue,
        starts_at=starts_at,
        is_home=True,
        is_parking=False,
        url=public_url,
    )


def _embedded_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [
        value["name"]
        for value in raw
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    ]


def _validated_external_id(raw_id: Any) -> str | None:
    if not isinstance(raw_id, str):
        return None
    event_id = raw_id.strip()
    if not event_id:
        return None
    try:
        encoded = event_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_EXTERNAL_ID_BYTES:
        return None
    return event_id


def _normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _public_event_url(raw_url: Any) -> str | None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    try:
        parsed = urlsplit(raw_url.strip())
        host = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if (
        parsed.scheme.casefold() != "https"
        or host not in _PUBLIC_EVENT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _has_parking_signal(raw: dict[str, Any]) -> bool:
    signals: list[str] = []
    for key in ("name", "type"):
        value = raw.get(key)
        if isinstance(value, str):
            signals.append(value)
    embedded = raw.get("_embedded")
    if isinstance(embedded, dict):
        signals.extend(_embedded_names(embedded.get("venues")))
    classifications = raw.get("classifications")
    if isinstance(classifications, list):
        for classification in classifications:
            if not isinstance(classification, dict):
                continue
            for value in classification.values():
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    signals.append(value["name"])
    return any(
        token in _PARKING_PRODUCT_TOKENS
        for signal in signals
        for token in re.findall(r"[a-z0-9]+", signal.casefold())
    )


def _parse_start(raw_dates: Any) -> datetime | None:
    if not isinstance(raw_dates, dict):
        return None
    raw_start = raw_dates.get("start")
    if not isinstance(raw_start, dict):
        return None
    date_time = raw_start.get("dateTime")
    if isinstance(date_time, str):
        try:
            parsed = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    local_date = raw_start.get("localDate")
    local_time = raw_start.get("localTime")
    if not isinstance(local_date, str) or not isinstance(local_time, str):
        return None
    try:
        local = datetime.fromisoformat(f"{local_date}T{local_time}").replace(
            tzinfo=_CHICAGO
        )
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _lowest_usd_minimum(price_ranges: list[Any]) -> Decimal | None:
    usable: list[Decimal] = []
    for price_range in price_ranges:
        if not isinstance(price_range, dict):
            continue
        currency = price_range.get("currency")
        if not isinstance(currency, str) or currency.upper() != "USD":
            continue
        try:
            minimum = Decimal(str(price_range.get("min")))
        except (InvalidOperation, ValueError):
            continue
        if minimum.is_finite() and minimum > 0:
            usable.append(minimum)
    return min(usable) if usable else None
