"""Official StubHub application OAuth and Catalog event-floor adapter."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.domain.matching import event_match_score
from ticket_reviewer.services.retry import call_with_retry
from ticket_reviewer.services.secure_logging import install_http_log_redaction


_TOKEN_URL = "https://account.stubhub.com/oauth2/token"
_SEARCH_URL = "https://api.stubhub.net/catalog/events/search"
_DETAIL_URL = "https://api.stubhub.net/catalog/events/{event_id}"
_CHICAGO = ZoneInfo("America/Chicago")
_MAX_EVENT_ID = 2_147_483_647
_MAX_EVENT_ID_DIGITS = 10
_MAX_TOKEN_BYTES = 4096
_MAX_CREDENTIAL_BYTES = 1024
_MAX_TOKEN_LIFETIME_SECONDS = 31_536_000
_DETAIL_START_TOLERANCE = timedelta(minutes=5)
_PUBLIC_EVENT_HOSTS = frozenset({"stubhub.com", "www.stubhub.com"})
_PRODUCT_WORDS = frozenset(
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
_INACTIVE_STATUSES = frozenset(
    {"cancelled", "canceled", "deleted", "draft", "contingent"}
)
_GENERIC_CATEGORIES = frozenset(
    {
        "event",
        "events",
        "football",
        "nfl",
        "ncaa football",
        "college football",
        "sports",
    }
)
_TEXANS_NAMES = frozenset({"houston texans", "texans"})
_AGGIES_NAMES = frozenset(
    {"texas a m aggies", "texas a m aggies football", "aggies"}
)
_TEXANS_VENUES = frozenset({"nrg stadium", "reliant stadium"})
_AGGIES_VENUES = frozenset({"kyle field"})
_TEXANS_HOME_AT = re.compile(
    r"^\s*(?P<opponent>.+?)\s+at\s+Houston\s+Texans\s*$", re.IGNORECASE
)
_TEXANS_HOME_VS = re.compile(
    r"^\s*Houston\s+Texans\s+vs\.?\s+(?P<opponent>.+?)\s*$", re.IGNORECASE
)
_AGGIES_HOME_AT = re.compile(
    r"^\s*(?P<opponent>.+?)\s+at\s+Texas\s+A\s*(?:&|and)?\s*M\s+Aggies(?:\s+Football)?\s*$",
    re.IGNORECASE,
)
_AGGIES_HOME_VS = re.compile(
    r"^\s*Texas\s+A\s*(?:&|and)?\s*M\s+Aggies(?:\s+Football)?\s+vs\.?\s+(?P<opponent>.+?)\s*$",
    re.IGNORECASE,
)
_AUTH_MESSAGE = "StubHub authentication failed"
_RATE_MESSAGE = "StubHub rate limit reached"
_NETWORK_MESSAGE = "StubHub is temporarily unavailable"
_PARSE_MESSAGE = "StubHub response could not be parsed"


class _CatalogUnauthorized(Exception):
    """Internal signal used to bound the one-time token refresh path."""


class StubHubTokenProvider:
    """Acquire and cache application-only StubHub OAuth tokens."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client,
        *,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        install_http_log_redaction()
        client_id = _credential_value(settings.stubhub_client_id)
        client_secret = _credential_value(settings.stubhub_client_secret)
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client
        self._sleep = sleep
        self._condition = threading.Condition()
        self._token: str | None = None
        self._reuse_before: datetime | None = None
        self._refreshing = False
        self._generation = 0
        self._generation_waiters: dict[int, int] = {}
        self._generation_results: dict[
            int, tuple[str | None, datetime | None, BaseException | None]
        ] = {}

    def get_token(self, now: datetime) -> str:
        """Return a cached bearer token or acquire one using client credentials."""
        normalized_now = _aware_utc(now, "now must be timezone-aware")
        while True:
            with self._condition:
                if (
                    self._token is not None
                    and self._reuse_before is not None
                    and normalized_now < self._reuse_before
                ):
                    return self._token
                if self._refreshing:
                    generation = self._generation
                    self._generation_waiters[generation] = (
                        self._generation_waiters.get(generation, 0) + 1
                    )
                    try:
                        while (
                            self._refreshing and self._generation == generation
                        ):
                            self._condition.wait()
                        token, reuse_before, failure = self._generation_results[
                            generation
                        ]
                    finally:
                        remaining = self._generation_waiters[generation] - 1
                        if remaining:
                            self._generation_waiters[generation] = remaining
                        else:
                            self._generation_waiters.pop(generation, None)
                            self._generation_results.pop(generation, None)
                    if failure is not None:
                        raise failure
                    if token is None:
                        raise AssertionError("token refresh completed without a result")
                    if reuse_before is not None and normalized_now < reuse_before:
                        return token
                    continue
                self._generation += 1
                generation = self._generation
                self._refreshing = True
                break

        try:
            token, expires_in = call_with_retry(self._acquire_token, self._sleep)
        except BaseException as error:
            with self._condition:
                if self._generation_waiters.get(generation, 0):
                    self._generation_results[generation] = (None, None, error)
                self._refreshing = False
                self._condition.notify_all()
            raise

        with self._condition:
            reuse_before = _safe_reuse_deadline(normalized_now, expires_in)
            self._token = token
            self._reuse_before = reuse_before
            if self._generation_waiters.get(generation, 0):
                self._generation_results[generation] = (
                    token,
                    reuse_before,
                    None,
                )
            self._refreshing = False
            self._condition.notify_all()
            return token

    def invalidate(self, token: str | None = None) -> None:
        """Invalidate all cached state, or only state matching a rejected token."""
        with self._condition:
            if token is not None and token != self._token:
                return
            self._token = None
            self._reuse_before = None

    def _acquire_token(self) -> tuple[str, int]:
        try:
            auth = httpx.BasicAuth(self._client_id, self._client_secret)
        except Exception:
            raise _auth_failure() from None
        try:
            response = self._client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": "read:events"},
                auth=auth,
            )
        except httpx.RequestError:
            raise _network_failure() from None
        _raise_token_status(response.status_code)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise _parse_failure() from None
        return _parse_token_payload(payload)


class StubHubConnector:
    """Read home-game event floors from the official StubHub Catalog API."""

    source = Source.STUBHUB
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})
    health_note = (
        "Detailed StubHub buyer inventory is unavailable to this key; "
        "event floors cannot trigger actionable alerts."
    )

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client,
        *,
        token_provider: StubHubTokenProvider | None = None,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], datetime] | None = None,
        owns_client: bool = False,
    ) -> None:
        install_http_log_redaction()
        self._client = client
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tokens = token_provider or StubHubTokenProvider(
            settings, client, sleep=sleep
        )
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
        if team is Team.TEXANS:
            query = "Houston Texans"
        elif team is Team.AGGIES:
            query = "Texas A&M Aggies Football"
        else:
            return []
        params = {
            "q": query,
            "page_size": "100",
            "country_code": "US",
            "exclude_parking_passes": "true",
        }
        after_local = after.astimezone(_CHICAGO)
        before_local = before.astimezone(_CHICAGO)
        if after_local.date() == before_local.date():
            params["dateLocal"] = after_local.date().isoformat()

        payload = self._catalog_json(_SEARCH_URL, params)
        raw_events = _hal_events(payload)
        accepted: dict[str, ExternalEvent] = {}
        for raw_event in raw_events:
            event = _parse_event(raw_event, team)
            if event is None or not after <= event.starts_at < before:
                continue
            accepted.setdefault(event.external_id, event)
        return sorted(
            accepted.values(), key=lambda event: (event.starts_at, event.external_id)
        )

    def fetch_observations(self, event: ExternalEvent) -> list[SourceObservation]:
        if event.source is not Source.STUBHUB:
            raise ValueError("event must come from StubHub")
        event_id = _validated_event_id(event.external_id)
        if event_id is None:
            raise _parse_failure()
        payload = self._catalog_json(_DETAIL_URL.format(event_id=event_id), {})
        returned = _parse_event(payload, event.team)
        if (
            returned is None
            or event_match_score(returned, event) < Decimal("0.85")
            or abs(returned.starts_at - event.starts_at) > _DETAIL_START_TOLERANCE
        ):
            raise _parse_failure()

        if "min_ticket_price" not in payload:
            return []
        minimum = _minimum_price(payload["min_ticket_price"])
        try:
            pair_price = (minimum * 2).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except DecimalException:
            raise _parse_failure() from None
        if pair_price == 0:
            return []
        observed_at = _aware_utc(
            self._clock(), "clock must return a timezone-aware datetime"
        )
        return [
            SourceObservation(
                source=Source.STUBHUB,
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
                listing_url=returned.url or _public_event_url(event.url),
            )
        ]

    def close(self) -> None:
        """Close only a client owned by the application composition root."""
        if self._owns_client and not self._closed:
            self._client.close()
            self._closed = True

    def _catalog_json(self, url: str, params: dict[str, str]) -> Any:
        now = _aware_utc(self._clock(), "clock must return a timezone-aware datetime")
        token = self._tokens.get_token(now)
        try:
            return self._catalog_json_with_token(url, params, token)
        except _CatalogUnauthorized:
            self._tokens.invalidate(token)
            refreshed = self._tokens.get_token(now)
            try:
                return self._catalog_json_with_token(url, params, refreshed)
            except _CatalogUnauthorized:
                raise _auth_failure() from None

    def _catalog_json_with_token(
        self, url: str, params: dict[str, str], token: str
    ) -> Any:
        def request() -> Any:
            try:
                response = self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.RequestError:
                raise _network_failure() from None
            if response.status_code == 401:
                raise _CatalogUnauthorized()
            _raise_catalog_status(response.status_code)
            try:
                return response.json()
            except (ValueError, TypeError):
                raise _parse_failure() from None

        return call_with_retry(request, self._sleep)


def _credential_value(secret: Any) -> str:
    if secret is None:
        raise ValueError("StubHub OAuth credentials are required")
    value = secret.get_secret_value()
    if not value or not value.strip():
        raise ValueError("StubHub OAuth credentials are required")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError, OverflowError):
        raise ValueError("StubHub OAuth credentials are invalid") from None
    if len(encoded) > _MAX_CREDENTIAL_BYTES or any(
        ord(character) < 32 or 127 <= ord(character) <= 159
        for character in value
    ):
        raise ValueError("StubHub OAuth credentials are invalid")
    return value


def _aware_utc(value: datetime, message: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(message)
    return value.astimezone(timezone.utc)


def _safe_reuse_deadline(issued_at: datetime, expires_in: int) -> datetime | None:
    """Return a cache deadline, or disable reuse when it cannot be represented."""
    if expires_in <= 60:
        return None
    try:
        return issued_at + timedelta(seconds=expires_in - 60)
    except OverflowError:
        return None


def _parse_token_payload(payload: Any) -> tuple[str, int]:
    if not isinstance(payload, dict):
        raise _parse_failure()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise _parse_failure()
    try:
        encoded = token.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise _parse_failure() from None
    if len(encoded) > _MAX_TOKEN_BYTES or any(
        ord(character) < 33 or ord(character) > 126 for character in token
    ):
        raise _parse_failure()
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise _parse_failure()
    expires_in = payload.get("expires_in")
    if (
        type(expires_in) is not int
        or expires_in <= 0
        or expires_in > _MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise _parse_failure()
    scope = payload.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or "read:events" not in scope.split():
            raise _parse_failure()
    return token, expires_in


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


def _hal_events(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise _parse_failure()
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict) or "events" not in embedded:
        raise _parse_failure()
    events = embedded["events"]
    if not isinstance(events, list):
        raise _parse_failure()
    return events


def _parse_event(raw: Any, team: Team) -> ExternalEvent | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    venue = raw.get("venue")
    categories = raw.get("categories")
    if not isinstance(name, str) or not isinstance(venue, dict) or not isinstance(categories, list):
        return None
    venue_name = venue.get("name")
    if not isinstance(venue_name, str):
        return None
    if _is_rejected_product(raw, name, venue_name, categories):
        return None
    status = raw.get("status")
    if isinstance(status, str) and _normalize_text(status) in _INACTIVE_STATUSES:
        return None
    if raw.get("date_confirmed") is False or raw.get("time_confirmed") is False:
        return None

    if team is Team.TEXANS:
        match = _TEXANS_HOME_AT.fullmatch(name) or _TEXANS_HOME_VS.fullmatch(name)
        team_names = _TEXANS_NAMES
        allowed_venues = _TEXANS_VENUES
        canonical_venue = "Reliant Stadium" if _normalize_text(venue_name) == "reliant stadium" else "NRG Stadium"
    elif team is Team.AGGIES:
        match = _AGGIES_HOME_AT.fullmatch(name) or _AGGIES_HOME_VS.fullmatch(name)
        team_names = _AGGIES_NAMES
        allowed_venues = _AGGIES_VENUES
        canonical_venue = "Kyle Field"
    else:
        return None
    if match is None or _normalize_text(venue_name) not in allowed_venues:
        return None
    opponent = match.group("opponent").strip()
    if not opponent:
        return None
    category_names = [
        item["name"]
        for item in categories
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    normalized_categories = {_normalize_text(value) for value in category_names}
    if not normalized_categories.intersection(team_names):
        return None
    relevant_others = normalized_categories - team_names - _GENERIC_CATEGORIES
    if relevant_others and _opponent_identity(opponent) not in {
        _opponent_identity(value) for value in relevant_others
    }:
        return None
    event_id = _validated_event_id(raw.get("id"))
    starts_at = _parse_start(raw.get("start_date"))
    if event_id is None or starts_at is None:
        raise _parse_failure()
    return ExternalEvent(
        source=Source.STUBHUB,
        external_id=event_id,
        team=team,
        opponent=opponent,
        venue=canonical_venue,
        starts_at=starts_at,
        is_home=True,
        is_parking=False,
        url=_public_event_url_from_links(raw.get("_links")),
    )


def _is_rejected_product(
    raw: dict[str, Any], name: str, venue_name: str, categories: list[Any]
) -> bool:
    raw_type = raw.get("type")
    if isinstance(raw_type, str) and _normalize_text(raw_type) == "parking":
        return True
    signals = [name, venue_name]
    signals.extend(
        item["name"]
        for item in categories
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    return any(
        _is_product_token(token)
        for signal in signals
        for token in re.findall(r"[a-z0-9]+", signal.casefold())
    )


def _is_product_token(token: str) -> bool:
    if token in _PRODUCT_WORDS or "parking" in token or "tailgat" in token:
        return True
    return "pass" in token and not token.startswith(("compassion", "passenger"))


def _normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _opponent_identity(value: str) -> str:
    return _normalize_text(value).removesuffix(" football").strip()


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


def _parse_start(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _public_event_url_from_links(raw_links: Any) -> str | None:
    if not isinstance(raw_links, dict):
        return None
    webpage = raw_links.get("event:webpage")
    if not isinstance(webpage, dict):
        return None
    return _public_event_url(webpage.get("href"))


def _public_event_url(raw_url: Any) -> str | None:
    if (
        not isinstance(raw_url, str)
        or not raw_url.strip()
        or _has_unsafe_url_text(raw_url)
    ):
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
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
        return None
    try:
        decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    if _has_unsafe_url_text(decoded_path):
        return None
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _has_unsafe_url_text(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return True
    return any(
        ord(character) < 32 or 127 <= ord(character) <= 159
        for character in value
    )


def _minimum_price(raw: Any) -> Decimal:
    if not isinstance(raw, dict):
        raise _parse_failure()
    currency = raw.get("currency_code")
    amount = raw.get("amount")
    if not isinstance(currency, str) or currency.upper() != "USD" or isinstance(amount, bool):
        raise _parse_failure()
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise _parse_failure() from None
    if not value.is_finite() or value < 0:
        raise _parse_failure()
    return value


def _raise_token_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise _auth_failure()
    if status_code == 429:
        raise _rate_failure()
    if status_code >= 500:
        raise _network_failure()
    raise _parse_failure()


def _raise_catalog_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code == 403:
        raise _auth_failure()
    if status_code == 429:
        raise _rate_failure()
    if status_code >= 500:
        raise _network_failure()
    raise _parse_failure()


def _auth_failure() -> ConnectorFailure:
    return ConnectorFailure(Source.STUBHUB, FailureCategory.AUTH, _AUTH_MESSAGE, False)


def _rate_failure() -> ConnectorFailure:
    return ConnectorFailure(
        Source.STUBHUB, FailureCategory.RATE_LIMIT, _RATE_MESSAGE, True
    )


def _network_failure() -> ConnectorFailure:
    return ConnectorFailure(
        Source.STUBHUB, FailureCategory.NETWORK, _NETWORK_MESSAGE, True
    )


def _parse_failure() -> ConnectorFailure:
    return ConnectorFailure(Source.STUBHUB, FailureCategory.PARSE, _PARSE_MESSAGE, False)
