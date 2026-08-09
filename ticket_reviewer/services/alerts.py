"""Fail-closed opportunity alert policy and ntfy publishing boundary."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import logging
import math
import re
from threading import Event, Lock
from typing import Protocol
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ticket_reviewer.config import RuntimeSettings, Settings
from ticket_reviewer.data.repositories import AlertRepository, SettingRepository
from ticket_reviewer.data.schema import (
    EventRow,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
    SourceEventRow,
)
from ticket_reviewer.domain.enums import (
    Confidence,
    ObservationKind,
    OpportunityStatus,
    Source,
    Team,
)


_CENT = Decimal("0.01")
_CLOCK_SKEW = timedelta(minutes=5)
_HEX_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_TOPIC = re.compile(r"[A-Za-z0-9_-]{24,128}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]{1,2048}={0,2}\Z")
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PUBLIC_HOSTS = {
    Source.STUBHUB: frozenset({"stubhub.com", "www.stubhub.com"}),
    Source.SEATGEEK: frozenset({"seatgeek.com", "www.seatgeek.com"}),
    Source.TICKETMASTER: frozenset(
        {"ticketmaster.com", "www.ticketmaster.com"}
    ),
    Source.TICKPICK: frozenset({"tickpick.com", "www.tickpick.com"}),
}
_TEAM_LABELS = {Team.TEXANS: "Texans", Team.AGGIES: "Texas A&M"}


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    source: object
    event_external_id: object
    listing_identity: object
    observed_at: object
    team: object
    opponent: object
    acquisition_total: object
    estimated_net_profit: object
    roi: object
    confidence: object
    actionable: object
    status: object
    kind: object
    currency: object
    pair_price: object
    section: object
    row: object
    quantity_available: object
    can_buy_pair: object
    listing_url: object


@dataclass(frozen=True, slots=True)
class PreviousAlert:
    profit_at_send: Decimal
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            not _finite_decimal(self.profit_at_send)
            or not isinstance(self.fingerprint, str)
            or _HEX_FINGERPRINT.fullmatch(self.fingerprint) is None
        ):
            raise ValueError("invalid previous alert")


@dataclass(frozen=True, slots=True)
class AlertDecision:
    should_send: bool
    reason: str
    fingerprint: str | None

    def __post_init__(self) -> None:
        valid_fingerprint = self.fingerprint is None or (
            isinstance(self.fingerprint, str)
            and _HEX_FINGERPRINT.fullmatch(self.fingerprint) is not None
        )
        if (
            type(self.should_send) is not bool
            or not _safe_ascii(self.reason, 128)
            or not valid_fingerprint
        ):
            raise ValueError("invalid alert decision")


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    priority: str
    tags: tuple[str, ...]
    click_url: str | None

    def __post_init__(self) -> None:
        if (
            not _safe_ascii(self.title, 160)
            or not _safe_ascii(self.body, 1024, allow_newline=True)
            or self.priority not in {"min", "low", "default", "high", "max"}
            or not isinstance(self.tags, tuple)
            or not self.tags
            or any(re.fullmatch(r"[a-z0-9_-]{1,32}", tag) is None for tag in self.tags)
            or (
                self.click_url is not None
                and not any(
                    sanitize_click_url(source, self.click_url) == self.click_url
                    for source in _PUBLIC_HOSTS
                )
            )
        ):
            raise ValueError("invalid push message")


class NotificationFailure(RuntimeError):
    """A transport-safe notification error with no request or secret context."""

    def __init__(self, *, retryable: bool) -> None:
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        self.retryable = retryable
        super().__init__(
            "notification temporarily unavailable"
            if retryable
            else "notification rejected"
        )


@dataclass(frozen=True, slots=True)
class NotificationTestResult:
    """Nonsecret, fixed-code result for an explicit local notification test."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in {
            "dry-run",
            "sent",
            "missing-topic",
            "temporarily-unavailable",
            "rejected",
        }:
            raise ValueError("invalid notification test result")


class Publisher(Protocol):
    def publish(self, message: PushMessage) -> str: ...


def _safe_ascii(value: object, limit: int, *, allow_newline: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > limit:
        return False
    try:
        value.encode("ascii")
    except (UnicodeEncodeError, UnicodeError):
        return False
    for character in value:
        code = ord(character)
        if character == "\n" and allow_newline:
            continue
        if code < 32 or code == 127:
            return False
    return True


def _finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _bounded_money(value: object) -> bool:
    return _finite_decimal(value) and abs(value) <= Decimal("9999999999.99")


def _cents(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    try:
        ascii_value = value.encode("ascii", "ignore").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    cleaned = "".join(
        character
        if character.isalnum() or character in " -_/&().'"
        else " "
        for character in ascii_value
    )
    return " ".join(cleaned.split())[:limit].rstrip()


def _contains_decoded_control(path: str) -> bool:
    if _BAD_PERCENT.search(path):
        return True
    try:
        decoded = unquote_to_bytes(path)
    except (UnicodeError, ValueError):
        return True
    return any(value < 32 or value == 127 for value in decoded)


def sanitize_click_url(source: object, value: object) -> str | None:
    """Return a query-free public marketplace URL or fail closed."""
    if not isinstance(source, Source) or source not in _PUBLIC_HOSTS:
        return None
    if not _safe_ascii(value, 2048):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "https"
        or host not in _PUBLIC_HOSTS[source]
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or _contains_decoded_control(parsed.path)
    ):
        return None
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


class AlertPolicy:
    def __init__(self, settings: RuntimeSettings) -> None:
        if not isinstance(settings, RuntimeSettings):
            raise TypeError("settings must be RuntimeSettings")
        self.settings = settings

    def is_stale(self, observed_at: object, now: object) -> bool:
        if not _aware_datetime(observed_at) or not _aware_datetime(now):
            return True
        try:
            age = now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return True
        freshness = timedelta(minutes=self.settings.observation_freshness_minutes)
        return not (-_CLOCK_SKEW <= age <= freshness)

    def decide(
        self,
        opportunity: AlertCandidate,
        previous_alert: PreviousAlert | None,
        stale: bool,
    ) -> AlertDecision:
        if not isinstance(opportunity, AlertCandidate):
            return _reject("invalid opportunity data")
        if stale is not False:
            return _reject("stale observation")
        if opportunity.actionable is not True:
            return _reject("not actionable")
        if not self._valid_values(opportunity):
            return _reject("invalid opportunity data")
        if not Decimal("0") < opportunity.acquisition_total <= self.settings.budget_cap:
            return _reject("outside budget")
        if opportunity.estimated_net_profit < self.settings.alert_profit_threshold:
            return _reject("below profit threshold")
        if opportunity.confidence is Confidence.LOW:
            return _reject("low confidence")
        if opportunity.status not in {
            OpportunityStatus.NEW,
            OpportunityStatus.WATCHING,
        }:
            return _reject("ineligible status")
        if not self._confirmed_pair(opportunity):
            return _reject("not a confirmed listing")

        fingerprint = self.fingerprint(opportunity)
        if previous_alert is not None:
            if not isinstance(previous_alert, PreviousAlert):
                return _reject("invalid opportunity data")
            if previous_alert.fingerprint == fingerprint:
                return _reject("already sent", fingerprint)
            improvement = opportunity.estimated_net_profit - previous_alert.profit_at_send
            if improvement < self.settings.profit_improvement_threshold:
                return _reject("repeat improvement required", fingerprint)
        return AlertDecision(True, "qualifying opportunity", fingerprint)

    def fingerprint(self, opportunity: AlertCandidate) -> str:
        payload = {
            "acquisition": _cents(opportunity.acquisition_total),
            "event": opportunity.event_external_id,
            "listing": opportunity.listing_identity,
            "observed_at": opportunity.observed_at.astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "profit": _cents(opportunity.estimated_net_profit),
            "source": opportunity.source.value,
        }
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()

    def message(self, opportunity: AlertCandidate) -> PushMessage:
        team = _TEAM_LABELS.get(opportunity.team, "")
        opponent = _clean_text(opportunity.opponent, 80)
        event_label = (
            f"{team} vs {opponent}" if team and opponent else team or opponent or "Ticket"
        )
        title = f"{event_label[:140].rstrip()} - Pair opportunity"
        lines: list[str] = []
        section = _clean_text(opportunity.section, 64)
        row = _clean_text(opportunity.row, 64)
        if section or row:
            seat = []
            if section:
                seat.append(f"Section {section}")
            if row:
                seat.append(f"Row {row}")
            lines.append(", ".join(seat))
        money = (
            f"Buy: ${_cents(opportunity.acquisition_total)} all-in"
            f" | Est. net: ${_cents(opportunity.estimated_net_profit)}"
        )
        if _finite_decimal(opportunity.roi):
            percent = (opportunity.roi * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            money += f" | ROI: {percent}%"
        lines.append(money)
        lines.append(f"Confidence: {opportunity.confidence.value}")
        return PushMessage(
            title=title,
            body="\n".join(lines)[:1024],
            priority="high",
            tags=("ticket", "moneybag"),
            click_url=sanitize_click_url(opportunity.source, opportunity.listing_url),
        )

    @staticmethod
    def _valid_values(opportunity: AlertCandidate) -> bool:
        return (
            isinstance(opportunity.source, Source)
            and _safe_ascii(opportunity.event_external_id, 255)
            and _safe_ascii(opportunity.listing_identity, 255)
            and _aware_datetime(opportunity.observed_at)
            and isinstance(opportunity.team, Team)
            and isinstance(opportunity.opponent, str)
            and _bounded_money(opportunity.acquisition_total)
            and _bounded_money(opportunity.estimated_net_profit)
            and (opportunity.roi is None or _finite_decimal(opportunity.roi))
            and isinstance(opportunity.confidence, Confidence)
            and isinstance(opportunity.status, OpportunityStatus)
            and isinstance(opportunity.kind, ObservationKind)
            and isinstance(opportunity.currency, str)
            and _bounded_money(opportunity.pair_price)
            and (
                opportunity.section is None
                or isinstance(opportunity.section, str)
            )
            and (opportunity.row is None or isinstance(opportunity.row, str))
            and (
                opportunity.listing_url is None
                or isinstance(opportunity.listing_url, str)
            )
        )

    @staticmethod
    def _confirmed_pair(opportunity: AlertCandidate) -> bool:
        return (
            opportunity.kind is ObservationKind.LISTING
            and opportunity.listing_identity.startswith("listing:")
            and bool(opportunity.listing_identity.removeprefix("listing:").strip())
            and opportunity.can_buy_pair is True
            and type(opportunity.quantity_available) is int
            and opportunity.quantity_available >= 2
            and opportunity.currency == "USD"
            and opportunity.pair_price > 0
        )


def _aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _reject(reason: str, fingerprint: str | None = None) -> AlertDecision:
    return AlertDecision(False, reason, fingerprint)


class _HttpxSecrecyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "HTTP notification request"
        record.args = ()
        return True


class NtfyPublisher:
    """Small synchronous ntfy client whose errors never expose request context."""

    def __init__(
        self,
        topic: str,
        access_token: str | None,
        timeout_seconds: float,
        *,
        client: object | None = None,
    ) -> None:
        self.validate_configuration(topic, access_token, timeout_seconds)
        self.__topic = topic
        self.__access_token = access_token
        self.__client = (
            client
            if client is not None
            else httpx.Client(timeout=float(timeout_seconds))
        )
        self.__owns_client = client is None
        self.__closed = False
        self.__log_filter = _HttpxSecrecyFilter()
        self.__loggers = tuple(
            logging.getLogger(name)
            for name in (
                "httpx",
                "httpcore",
                "httpcore.connection",
                "httpcore.http11",
                "httpcore.http2",
                "httpcore.proxy",
            )
        )
        for logger in self.__loggers:
            logger.addFilter(self.__log_filter)

    @staticmethod
    def validate_configuration(
        topic: str, access_token: str | None, timeout_seconds: float
    ) -> None:
        if not _valid_topic(topic) or (
            access_token is not None
            and (
                len(access_token) > 2048
                or _TOKEN.fullmatch(access_token) is None
            )
        ):
            raise ValueError("invalid notification configuration")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0 < float(timeout_seconds) <= 120
        ):
            raise ValueError("invalid notification configuration")

    def publish(self, message: PushMessage) -> str:
        if not isinstance(message, PushMessage):
            raise NotificationFailure(retryable=False)
        headers = {
            "Title": message.title,
            "Priority": message.priority,
            "Tags": ",".join(message.tags),
        }
        if message.click_url is not None:
            headers["Click"] = message.click_url
        if self.__access_token is not None:
            headers["Authorization"] = f"Bearer {self.__access_token}"
        try:
            response = self.__client.post(
                f"https://ntfy.sh/{self.__topic}",
                content=message.body.encode("utf-8"),
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError):
            raise NotificationFailure(retryable=True) from None
        except Exception:
            raise NotificationFailure(retryable=True) from None

        if response.status_code == 429 or response.status_code >= 500:
            raise NotificationFailure(retryable=True)
        if not 200 <= response.status_code < 300:
            raise NotificationFailure(retryable=False)
        try:
            payload = response.json()
        except Exception:
            raise NotificationFailure(retryable=False) from None
        provider_id = payload.get("id") if isinstance(payload, dict) else None
        secrets = (self.__topic, self.__access_token)
        if (
            not isinstance(provider_id, str)
            or _PROVIDER_ID.fullmatch(provider_id) is None
            or any(
                secret is not None
                and (provider_id in secret or secret in provider_id)
                for secret in secrets
            )
        ):
            raise NotificationFailure(retryable=False)
        return provider_id

    def close(self) -> None:
        if self.__closed:
            return
        self.__closed = True
        for logger in self.__loggers:
            logger.removeFilter(self.__log_filter)
        if self.__owns_client:
            self.__client.close()


def _valid_topic(topic: object) -> bool:
    return (
        isinstance(topic, str)
        and _TOPIC.fullmatch(topic) is not None
        and len(set(topic)) >= 12
    )


@dataclass(slots=True)
class _DeliveryFlight:
    completed: Event
    decision: AlertDecision | None = None


@dataclass(slots=True)
class _NotificationTestFlight:
    completed: Event
    result: NotificationTestResult | None = None


class AlertService:
    """Load, decide, publish, and persist using fresh caller-owned sessions."""

    def __init__(
        self,
        settings: Settings,
        session_factory,
        *,
        publisher: Publisher | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.publisher = publisher
        self._state_lock = Lock()
        self._active_evaluations = 0
        self._delivery_flights: dict[str, _DeliveryFlight] = {}
        self._active_notification_tests = 0
        self._notification_test_flight: _NotificationTestFlight | None = None

    def test_notification(self) -> NotificationTestResult:
        """Publish one static test through the owned publisher without persistence."""
        with self._state_lock:
            self._active_notification_tests += 1
            flight = self._notification_test_flight
            leader = flight is None
            if flight is None:
                flight = _NotificationTestFlight(Event())
                self._notification_test_flight = flight
        try:
            if not leader:
                flight.completed.wait()
                with self._state_lock:
                    shared = flight.result
                return shared or NotificationTestResult("temporarily-unavailable")
            result = self._perform_notification_test()
            with self._state_lock:
                flight.result = result
                flight.completed.set()
            return result
        finally:
            with self._state_lock:
                self._active_notification_tests -= 1
                if self._active_notification_tests == 0:
                    self._notification_test_flight = None

    def _perform_notification_test(self) -> NotificationTestResult:
        if self.settings.dry_run:
            return NotificationTestResult("dry-run")
        if self.publisher is None:
            return NotificationTestResult("missing-topic")
        message = PushMessage(
            title="Ticket Reviewer test",
            body="This is a local Ticket Reviewer test notification.",
            priority="default",
            tags=("test_tube",),
            click_url=None,
        )
        try:
            self.publisher.publish(message)
        except NotificationFailure as error:
            return NotificationTestResult(
                "temporarily-unavailable" if error.retryable else "rejected"
            )
        except Exception:
            return NotificationTestResult("temporarily-unavailable")
        return NotificationTestResult("sent")

    def evaluate_and_send(self, opportunity_id: int, now: datetime) -> AlertDecision:
        if type(opportunity_id) is not int or opportunity_id <= 0 or not _aware_datetime(now):
            return _reject("invalid opportunity context")
        with self._state_lock:
            self._active_evaluations += 1
        try:
            return self._evaluate(opportunity_id, now.astimezone(timezone.utc))
        finally:
            with self._state_lock:
                self._active_evaluations -= 1
                if self._active_evaluations == 0:
                    self._delivery_flights.clear()

    def _evaluate(self, opportunity_id: int, now: datetime) -> AlertDecision:
        try:
            candidate, previous, effective = self._load(opportunity_id)
            policy = AlertPolicy(effective)
            decision = policy.decide(
                candidate, previous, policy.is_stale(candidate.observed_at, now)
            )
        except Exception:
            return _reject("invalid opportunity context")
        if not decision.should_send or decision.fingerprint is None:
            return decision
        fingerprint = decision.fingerprint
        with self._state_lock:
            flight = self._delivery_flights.get(fingerprint)
            leader = flight is None
            if flight is None:
                flight = _DeliveryFlight(Event())
                self._delivery_flights[fingerprint] = flight
        if not leader:
            flight.completed.wait()
            with self._state_lock:
                shared = flight.decision
            if shared is None:
                return _reject("notification temporarily unavailable", fingerprint)
            if shared.should_send:
                return _reject("already sent", fingerprint)
            return shared

        try:
            delivered = self._deliver(
                opportunity_id, now, candidate, policy, decision
            )
        except BaseException:
            with self._state_lock:
                flight.decision = _reject(
                    "notification temporarily unavailable", fingerprint
                )
                flight.completed.set()
            raise
        with self._state_lock:
            flight.decision = delivered
            flight.completed.set()
        return delivered

    def _deliver(
        self,
        opportunity_id: int,
        now: datetime,
        candidate: AlertCandidate,
        policy: AlertPolicy,
        decision: AlertDecision,
    ) -> AlertDecision:
        fingerprint = decision.fingerprint
        if fingerprint is None:
            return _reject("invalid opportunity context")
        if self._already_sent(fingerprint):
            return _reject("already sent", fingerprint)
        if self.settings.dry_run:
            provider_id = "dry-run"
        else:
            if self.publisher is None:
                return _reject("notification rejected", fingerprint)
            try:
                provider_id = self.publisher.publish(policy.message(candidate))
            except NotificationFailure as error:
                return _reject(str(error), fingerprint)
            except Exception:
                return _reject("notification temporarily unavailable", fingerprint)
            if (
                not isinstance(provider_id, str)
                or _PROVIDER_ID.fullmatch(provider_id) is None
            ):
                return _reject("notification rejected", fingerprint)
        try:
            with self.session_factory() as session:
                repository = AlertRepository(session)
                if repository.find_successful_by_fingerprint(fingerprint) is not None:
                    return _reject("already sent", fingerprint)
                repository.record(
                    opportunity_id,
                    fingerprint,
                    now,
                    candidate.estimated_net_profit,
                    provider_id,
                )
                session.commit()
        except IntegrityError:
            return _reject("already sent", fingerprint)
        except Exception:
            return _reject("notification temporarily unavailable", fingerprint)
        return decision

    def _already_sent(self, fingerprint: str) -> bool:
        with self.session_factory() as session:
            return (
                AlertRepository(session).find_successful_by_fingerprint(fingerprint)
                is not None
            )

    def _load(
        self, opportunity_id: int
    ) -> tuple[AlertCandidate, PreviousAlert | None, RuntimeSettings]:
        with self.session_factory() as session:
            effective = SettingRepository(session).effective(self.settings)
            opportunity = session.get(OpportunityRow, opportunity_id)
            if opportunity is None:
                raise LookupError("missing opportunity")
            observation = session.get(ObservationRow, opportunity.observation_id)
            event = session.get(EventRow, opportunity.event_id)
            if (
                observation is None
                or event is None
                or observation.event_id != opportunity.event_id
            ):
                raise LookupError("broken opportunity context")
            source_event = session.scalar(
                select(SourceEventRow).where(
                    SourceEventRow.event_id == opportunity.event_id,
                    SourceEventRow.source == observation.source,
                    SourceEventRow.external_id == observation.event_external_id,
                )
            )
            if source_event is None:
                raise LookupError("inconsistent opportunity context")
            source = Source(observation.source)
            listing_identity = observation.listing_identity
            previous_row = AlertRepository(session).latest_for_lineage(
                source.value,
                observation.event_external_id,
                listing_identity,
            )
            previous = (
                PreviousAlert(previous_row.profit_at_send, previous_row.fingerprint)
                if previous_row is not None
                else None
            )
            lineage_outcome = session.execute(
                select(OutcomeRow, OpportunityRow.estimated_net_profit)
                .join(
                    OpportunityRow,
                    OutcomeRow.opportunity_id == OpportunityRow.id,
                )
                .join(
                    ObservationRow,
                    OpportunityRow.observation_id == ObservationRow.id,
                )
                .where(
                    ObservationRow.source == source.value,
                    ObservationRow.event_external_id
                    == observation.event_external_id,
                    ObservationRow.listing_identity == listing_identity,
                )
                .order_by(OutcomeRow.updated_at.desc(), OutcomeRow.id.desc())
                .limit(1)
            ).first()
            authoritative_status = OpportunityStatus(opportunity.status)
            if lineage_outcome is not None:
                authoritative_status = OpportunityStatus(lineage_outcome[0].status)
            if (
                lineage_outcome is not None
                and authoritative_status is OpportunityStatus.WATCHING
                and lineage_outcome[1] is not None
                and (
                    previous_row is None
                    or lineage_outcome[0].updated_at > previous_row.sent_at
                )
            ):
                previous = PreviousAlert(
                    lineage_outcome[1],
                    "0" * 64,
                )
            candidate = AlertCandidate(
                source=source,
                event_external_id=observation.event_external_id,
                listing_identity=listing_identity,
                observed_at=observation.observed_at,
                team=Team(event.team),
                opponent=event.opponent,
                acquisition_total=opportunity.acquisition_total,
                estimated_net_profit=opportunity.estimated_net_profit,
                roi=opportunity.roi,
                confidence=Confidence(opportunity.confidence),
                actionable=opportunity.actionable,
                status=authoritative_status,
                kind=ObservationKind(observation.kind),
                currency=observation.currency,
                pair_price=observation.pair_price,
                section=observation.section,
                row=observation.row,
                quantity_available=observation.quantity_available,
                can_buy_pair=observation.can_buy_pair,
                listing_url=observation.listing_url,
            )
            return candidate, previous, effective
