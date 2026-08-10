"""Official public SeatGeek event API adapter for Aggies games at Kyle Field."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.domain.matching import event_match_score
from ticket_reviewer.services.retry import call_with_retry
from ticket_reviewer.services.secure_logging import install_http_log_redaction


_SEARCH_URL = "https://api.seatgeek.com/2/events"
_DETAIL_URL = "https://api.seatgeek.com/2/events/{event_id}"
_AGGIES_SLUG = "texas-a-m-aggies-football"
_MAX_EVENT_ID = 9_223_372_036_854_775_807
_MAX_EVENT_ID_DIGITS = 19
_MAX_LISTING_COUNT = 2_147_483_647
_PUBLIC_EVENT_HOSTS = frozenset({"seatgeek.com", "www.seatgeek.com"})
_PRODUCT_WORDS = frozenset(
    {"parking", "parkings", "tailgate", "tailgates", "tailgated", "tailgating", "pass", "passes"}
)
_AGGIES_TITLE = r"Texas\s+A\s*(?:&|and)?\s*M\s+Aggies(?:\s+Football)?"
_HOME_AT_TITLE = re.compile(
    rf"^\s*(?P<opponent>.+?)\s+at\s+{_AGGIES_TITLE}\s*$", re.IGNORECASE
)
_HOME_VS_TITLE = re.compile(
    rf"^\s*{_AGGIES_TITLE}\s+vs\.?\s+(?P<opponent>.+?)\s*$", re.IGNORECASE
)
_AUTH_MESSAGE = "SeatGeek authentication failed"
_RATE_MESSAGE = "SeatGeek rate limit reached"
_NETWORK_MESSAGE = "SeatGeek is temporarily unavailable"
_PARSE_MESSAGE = "SeatGeek response could not be parsed"


class SeatGeekConnector:
    """Read non-actionable Aggies event market signals from SeatGeek."""

    source = Source.SEATGEEK
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
        raw_client_id = settings.seatgeek_client_id
        client_id = (
            raw_client_id.get_secret_value().strip()
            if raw_client_id is not None
            else ""
        )
        if not client_id:
            raise ValueError("SeatGeek client ID is required")
        raw_secret = settings.seatgeek_client_secret
        self._client_id = client_id
        self._client_secret = (
            raw_secret.get_secret_value().strip() if raw_secret is not None else ""
        )
        self._client = client
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._owns_client = owns_client
        self._closed = False

    @property
    def client(self) -> httpx.Client:
        """Return the client for composition-root lifecycle inspection."""
        return self._client

    def discover(
        self, team: Team, starts_after: datetime, starts_before: datetime
    ) -> list[ExternalEvent]:
        after, before = _normalize_window(starts_after, starts_before)
        if team is not Team.AGGIES:
            return []
        params = self._auth_params()
        params.update(
            {
                "performers.slug": _AGGIES_SLUG,
                "datetime_utc.gte": _utc_parameter(after),
                "datetime_utc.lte": _utc_parameter(before),
                "venue.state": "TX",
                "per_page": "100",
            }
        )
        payload = self._get_json(_SEARCH_URL, params)
        raw_events = _search_events(payload)
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
        if event.source is not Source.SEATGEEK:
            raise ValueError("event must come from SeatGeek")
        event_id = _validated_event_id(event.external_id)
        if event_id is None:
            raise _parse_failure()
        payload = self._get_json(
            _DETAIL_URL.format(event_id=event_id), self._auth_params()
        )
        parsed = _parse_event(payload)
        if (
            parsed is None
            or parsed.external_id != event_id
            or parsed.starts_at != event.starts_at.astimezone(timezone.utc)
            or event_match_score(parsed, event) < Decimal("0.85")
        ):
            raise _parse_failure()
        if not isinstance(payload, dict):
            raise _parse_failure()
        raw_stats = payload.get("stats", {})
        if raw_stats is None:
            raw_stats = {}
        if not isinstance(raw_stats, dict):
            raise _parse_failure()

        lowest = _money(raw_stats, "lowest_price")
        average = _money(raw_stats, "average_price")
        highest = _money(raw_stats, "highest_price")
        listing_count = _listing_count(raw_stats.get("listing_count"))
        popularity = _score(payload.get("score"))
        floor_price = _pair_price(lowest)
        average_price = _pair_price(average)
        highest_price = _pair_price(highest)
        aggregate_price = average_price
        if aggregate_price is None:
            aggregate_price = highest_price

        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        observed_at = observed_at.astimezone(timezone.utc)
        public_url = _public_event_url(payload.get("url")) or _public_event_url(event.url)
        common = {
            "source": Source.SEATGEEK,
            "event_external_id": event_id,
            "observed_at": observed_at,
            "currency": "USD",
            "buyer_fees": None,
            "estimated_tax": None,
            "section": None,
            "row": None,
            "quantity_available": None,
            "can_buy_pair": None,
            "listing_id": None,
            "listing_url": public_url,
        }
        observations: list[SourceObservation] = []
        if floor_price is not None:
            observations.append(
                SourceObservation(
                    **common,
                    kind=ObservationKind.EVENT_FLOOR,
                    pair_price=floor_price,
                )
            )
        if aggregate_price is not None or listing_count is not None or popularity is not None:
            observations.append(
                SourceObservation(
                    **common,
                    kind=ObservationKind.EVENT_AGGREGATE,
                    pair_price=aggregate_price,
                    listing_count=listing_count,
                    popularity=popularity,
                )
            )
        return observations

    def close(self) -> None:
        """Close only a client owned by the application composition root."""
        if self._owns_client and not self._closed:
            self._client.close()
            self._closed = True

    def _auth_params(self) -> dict[str, str]:
        params = {"client_id": self._client_id}
        if self._client_secret:
            params["client_secret"] = self._client_secret
        return params

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


def _search_events(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise _parse_failure()
    if "events" not in payload:
        raise _parse_failure()
    events = payload["events"]
    if not isinstance(events, list):
        raise _parse_failure()
    return events


def _parse_event(raw: Any) -> ExternalEvent | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    performers = raw.get("performers")
    venue = raw.get("venue")
    if not isinstance(title, str) or not isinstance(performers, list) or not isinstance(venue, dict):
        return None
    performer_rows = [item for item in performers if isinstance(item, dict)]
    has_aggies_performer = any(_is_aggies_performer(item) for item in performer_rows)
    title_norm = _normalize_text(title)
    if not has_aggies_performer or "texas a m aggies" not in title_norm:
        return None
    signals = [title]
    signals.extend(
        item[key]
        for item in performer_rows
        for key in ("name", "slug")
        if isinstance(item.get(key), str)
    )
    signals.extend(value for value in venue.values() if isinstance(value, str))
    if _has_product_signal(signals):
        return None
    venue_name = venue.get("name")
    if not isinstance(venue_name, str) or _normalize_text(venue_name) != "kyle field":
        return None
    state = venue.get("state")
    if state is not None and (not isinstance(state, str) or _normalize_text(state) != "tx"):
        return None

    home_match = _HOME_AT_TITLE.fullmatch(title) or _HOME_VS_TITLE.fullmatch(title)
    if home_match is None:
        return None

    title_opponent = home_match.group("opponent").strip()
    opponent_performers = [
        item for item in performer_rows if not _is_aggies_performer(item)
    ]
    if opponent_performers:
        if len(opponent_performers) != 1:
            return None
        performer_name = opponent_performers[0].get("name")
        if (
            not isinstance(performer_name, str)
            or not performer_name.strip()
            or _opponent_identity(performer_name)
            != _opponent_identity(title_opponent)
        ):
            return None
    event_id = _validated_event_id(raw.get("id"))
    if event_id is None:
        raise _parse_failure()
    starts_at = _parse_datetime(raw.get("datetime_utc"))
    if starts_at is None:
        raise _parse_failure()
    return ExternalEvent(
        source=Source.SEATGEEK,
        external_id=event_id,
        team=Team.AGGIES,
        opponent=title_opponent,
        venue="Kyle Field",
        starts_at=starts_at,
        is_home=True,
        is_parking=False,
        url=_public_event_url(raw.get("url")),
    )


def _normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _is_aggies_performer(raw: dict[str, Any]) -> bool:
    return raw.get("slug") == _AGGIES_SLUG or (
        isinstance(raw.get("name"), str)
        and _normalize_text(raw["name"]) == "texas a m aggies football"
    )


def _opponent_identity(value: str) -> str:
    normalized = _normalize_text(value)
    return normalized.removesuffix(" football").strip()


def _has_product_signal(signals: list[str]) -> bool:
    return any(
        _is_product_token(token)
        for signal in signals
        for token in re.findall(r"[a-z0-9]+", signal.casefold())
    )


def _is_product_token(token: str) -> bool:
    if token in _PRODUCT_WORDS or "parking" in token or "tailgat" in token:
        return True
    return "pass" in token and not token.startswith(("compassion", "passenger"))


def _validated_event_id(raw: Any) -> str | None:
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str):
        digits = raw.strip()
        if (
            not digits
            or len(digits) > _MAX_EVENT_ID_DIGITS
            or not digits.isascii()
            or not digits.isdigit()
        ):
            return None
        try:
            value = int(digits)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if not 0 < value <= _MAX_EVENT_ID:
        return None
    return str(value)


def _parse_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _public_event_url(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = urlsplit(raw.strip())
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


def _money(stats: dict[str, Any], name: str) -> Decimal | None:
    raw = stats.get(name)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _parse_failure()
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise _parse_failure() from None
    if not value.is_finite() or value < 0:
        raise _parse_failure()
    return value


def _pair_price(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        pair = (value * 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except DecimalException:
        raise _parse_failure() from None
    return pair if pair > 0 else None


def _listing_count(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _parse_failure()
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise _parse_failure() from None
    if not value.is_finite() or value != value.to_integral_value():
        raise _parse_failure()
    count = int(value)
    if not 0 <= count <= _MAX_LISTING_COUNT:
        raise _parse_failure()
    return count


def _score(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _parse_failure()
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise _parse_failure() from None
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
        raise _parse_failure()
    return value


def _raise_for_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise ConnectorFailure(Source.SEATGEEK, FailureCategory.AUTH, _AUTH_MESSAGE, False)
    if status_code == 429:
        raise ConnectorFailure(Source.SEATGEEK, FailureCategory.RATE_LIMIT, _RATE_MESSAGE, True)
    if status_code >= 500:
        raise ConnectorFailure(Source.SEATGEEK, FailureCategory.NETWORK, _NETWORK_MESSAGE, True)
    raise _parse_failure()


def _parse_failure() -> ConnectorFailure:
    return ConnectorFailure(
        Source.SEATGEEK, FailureCategory.PARSE, _PARSE_MESSAGE, retryable=False
    )
