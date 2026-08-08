"""Small transaction-scoped repositories for persistence consumers."""

from datetime import datetime
from decimal import Decimal
import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ticket_reviewer.config import RuntimeSettings, Settings
from ticket_reviewer.domain.enums import OpportunityStatus, Source, Team
from ticket_reviewer.domain.models import ExternalEvent, OpportunityEstimate, SourceObservation

from .schema import (
    AlertRow,
    ConnectorRunRow,
    EventRow,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
    SettingRow,
    SourceEventRow,
    utc_now,
)


ALLOWED_SETTING_FIELDS = frozenset(
    {
        "budget_cap",
        "alert_profit_threshold",
        "profit_improvement_threshold",
        "observation_freshness_minutes",
        "scan_interval_minutes",
        "stubhub_seller_fee_rate",
        "ticketmaster_seller_fee_rate",
        "seatgeek_seller_fee_rate",
    }
)
_VALID_RUNTIME_BASE = {
    "budget_cap": Decimal("1"),
    "alert_profit_threshold": Decimal("0"),
    "profit_improvement_threshold": Decimal("0"),
    "observation_freshness_minutes": 60,
    "scan_interval_minutes": 60,
    "stubhub_seller_fee_rate": Decimal("0"),
    "ticketmaster_seller_fee_rate": Decimal("0"),
    "seatgeek_seller_fee_rate": Decimal("0"),
}
_INTEGER_RUNTIME_FIELDS = frozenset(
    {"observation_freshness_minutes", "scan_interval_minutes"}
)


def _source_value(source: Source | str) -> str:
    return Source(source).value


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self, event: ExternalEvent, *, canonical_event_id: int | None = None
    ) -> int:
        source_value = event.source.value
        source_row = self.session.scalar(
            select(SourceEventRow).where(
                SourceEventRow.source == source_value,
                SourceEventRow.external_id == event.external_id,
            )
        )
        now = utc_now()
        if source_row is not None:
            event_row = self.session.get(EventRow, source_row.event_id)
            if event_row is None:
                raise RuntimeError("source event references a missing event")
            event_row.team = event.team.value
            event_row.opponent = event.opponent
            event_row.venue = event.venue
            event_row.starts_at = event.starts_at
            event_row.is_home = event.is_home
            event_row.updated_at = now
            source_row.url = event.url
            source_row.raw_name = self._raw_name(event)
            source_row.last_seen = now
            self.session.flush()
            return event_row.id

        event_row = (
            self.session.get(EventRow, canonical_event_id)
            if canonical_event_id is not None
            else self.session.scalar(
                select(EventRow).where(
                    EventRow.team == event.team.value,
                    EventRow.opponent == event.opponent,
                    EventRow.venue == event.venue,
                    EventRow.starts_at == event.starts_at,
                )
            )
        )
        if canonical_event_id is not None and event_row is None:
            raise LookupError(f"canonical event {canonical_event_id} does not exist")
        if event_row is None:
            event_row = EventRow(
                team=event.team.value,
                opponent=event.opponent,
                venue=event.venue,
                starts_at=event.starts_at,
                is_home=event.is_home,
            )
            self.session.add(event_row)
            self.session.flush()

        self.session.add(
            SourceEventRow(
                event_id=event_row.id,
                source=source_value,
                external_id=event.external_id,
                url=event.url,
                raw_name=self._raw_name(event),
                last_seen=now,
            )
        )
        self.session.flush()
        return event_row.id

    def get(self, event_id: int) -> EventRow | None:
        return self.session.get(EventRow, event_id)

    def find_by_source(
        self, source: Source | str, external_id: str
    ) -> SourceEventRow | None:
        return self.session.scalar(
            select(SourceEventRow).where(
                SourceEventRow.source == _source_value(source),
                SourceEventRow.external_id == external_id,
            )
        )

    def list_for_team(self, team: Team | str) -> list[EventRow]:
        team_value = Team(team).value
        return list(
            self.session.scalars(
                select(EventRow)
                .where(EventRow.team == team_value)
                .order_by(EventRow.starts_at, EventRow.id)
            )
        )

    @staticmethod
    def _raw_name(event: ExternalEvent) -> str:
        return f"{event.team.value} vs {event.opponent}"


class ObservationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, event_id: int, observation: SourceObservation) -> int:
        listing_identity = (
            f"missing:{observation.kind.value}"
            if observation.listing_id is None
            else f"listing:{observation.listing_id}"
        )
        existing = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.source == observation.source.value,
                ObservationRow.event_external_id == observation.event_external_id,
                ObservationRow.listing_identity == listing_identity,
                ObservationRow.observed_at == observation.observed_at,
            )
        )
        if existing is not None:
            return existing.id

        row = ObservationRow(
            event_id=event_id,
            source=observation.source.value,
            event_external_id=observation.event_external_id,
            observed_at=observation.observed_at,
            kind=observation.kind.value,
            currency=observation.currency,
            pair_price=observation.pair_price,
            buyer_fees=observation.buyer_fees,
            estimated_tax=observation.estimated_tax,
            section=observation.section,
            row=observation.row,
            quantity_available=observation.quantity_available,
            can_buy_pair=observation.can_buy_pair,
            listing_id=observation.listing_id,
            listing_identity=listing_identity,
            listing_url=observation.listing_url,
            listing_count=observation.listing_count,
            popularity=observation.popularity,
            freshness_at=observation.observed_at,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def get(self, observation_id: int) -> ObservationRow | None:
        return self.session.get(ObservationRow, observation_id)

    def list_for_event(self, event_id: int) -> list[ObservationRow]:
        return list(
            self.session.scalars(
                select(ObservationRow)
                .where(ObservationRow.event_id == event_id)
                .order_by(ObservationRow.observed_at.desc(), ObservationRow.id.desc())
            )
        )


class OpportunityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_estimate(
        self, event_id: int, observation_id: int, estimate: OpportunityEstimate
    ) -> int:
        row = OpportunityRow(
            event_id=event_id,
            observation_id=observation_id,
            acquisition_total=estimate.acquisition_total,
            exit_source=estimate.exit_source.value if estimate.exit_source else None,
            projected_resale_gross=estimate.projected_resale_gross,
            seller_fee_rate=estimate.seller_fee_rate,
            projected_proceeds=estimate.projected_proceeds,
            estimated_net_profit=estimate.estimated_net_profit,
            roi=estimate.roi,
            confidence=estimate.confidence.value,
            risk_reasons=list(estimate.risk_reasons),
            actionable=estimate.actionable,
            status=OpportunityStatus.NEW.value,
            scenarios=[
                {
                    "marketplace": scenario.marketplace.value,
                    "projected_resale_gross": str(scenario.projected_resale_gross),
                    "seller_fee_rate": str(scenario.seller_fee_rate),
                    "projected_proceeds": str(scenario.projected_proceeds),
                    "comparable_count": scenario.comparable_count,
                }
                for scenario in estimate.scenarios
            ],
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def get(self, opportunity_id: int) -> OpportunityRow | None:
        return self.session.get(OpportunityRow, opportunity_id)

    def list_for_event(self, event_id: int) -> list[OpportunityRow]:
        return list(
            self.session.scalars(
                select(OpportunityRow)
                .where(OpportunityRow.event_id == event_id)
                .order_by(OpportunityRow.created_at.desc(), OpportunityRow.id.desc())
            )
        )


class AlertRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_fingerprint(self, fingerprint: str) -> AlertRow | None:
        return self.session.scalar(
            select(AlertRow).where(AlertRow.fingerprint == fingerprint)
        )

    def record(
        self,
        opportunity_id: int,
        fingerprint: str,
        sent_at: datetime,
        profit_at_send: Decimal,
        provider_message_id: str | None = None,
    ) -> int:
        existing = self.find_by_fingerprint(fingerprint)
        if existing is not None:
            return existing.id
        row = AlertRow(
            opportunity_id=opportunity_id,
            fingerprint=fingerprint,
            sent_at=sent_at,
            profit_at_send=profit_at_send,
            provider_message_id=provider_message_id,
        )
        self.session.add(row)
        self.session.flush()
        return row.id


class OutcomeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        opportunity_id: int,
        status: OpportunityStatus | str,
        *,
        actual_acquisition: Decimal | None = None,
        actual_proceeds: Decimal | None = None,
        actual_fees: Decimal | None = None,
        notes: str | None = None,
    ) -> int:
        status_value = OpportunityStatus(status).value
        opportunity = self.session.get(OpportunityRow, opportunity_id)
        if opportunity is None:
            raise LookupError(f"opportunity {opportunity_id} does not exist")
        row = self.session.scalar(
            select(OutcomeRow).where(OutcomeRow.opportunity_id == opportunity_id)
        )
        if row is None:
            row = OutcomeRow(opportunity_id=opportunity_id, status=status_value)
            self.session.add(row)
        row.status = status_value
        row.actual_acquisition = actual_acquisition
        row.actual_proceeds = actual_proceeds
        row.actual_fees = actual_fees
        row.notes = notes
        row.updated_at = utc_now()
        opportunity.status = status_value
        opportunity.updated_at = utc_now()
        self.session.flush()
        return row.id

    def get(self, opportunity_id: int) -> OutcomeRow | None:
        return self.session.scalar(
            select(OutcomeRow).where(OutcomeRow.opportunity_id == opportunity_id)
        )


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(self, source: Source | str, started_at: datetime | None = None) -> int:
        if started_at is not None and (
            started_at.tzinfo is None or started_at.utcoffset() is None
        ):
            raise ValueError("started_at must be timezone-aware")
        row = ConnectorRunRow(
            source=_source_value(source),
            started_at=started_at or utc_now(),
            observation_count=0,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def finish(
        self,
        run_id: int,
        *,
        success: bool,
        observation_count: int,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        if observation_count < 0:
            raise ValueError("observation_count must be non-negative")
        row = self.session.get(ConnectorRunRow, run_id)
        if row is None:
            raise LookupError(f"connector run {run_id} does not exist")
        row.finished_at = finished_at or utc_now()
        row.success = success
        row.observation_count = observation_count
        row.redacted_error = _redact_error(error)
        self.session.flush()

    def get(self, run_id: int) -> ConnectorRunRow | None:
        return self.session.get(ConnectorRunRow, run_id)


class SettingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        self._ensure_allowed(key)
        row = self.session.get(SettingRow, key)
        return row.value if row is not None else None

    def set(self, key: str, value: str | Decimal | int) -> None:
        self._ensure_allowed(key)
        parsed = self._parse(key, value)
        row = self.session.get(SettingRow, key)
        if row is None:
            row = SettingRow(key=key, value=str(parsed))
            self.session.add(row)
        else:
            row.value = str(parsed)
            row.updated_at = utc_now()
        self.session.flush()

    def effective(self, base: Settings) -> RuntimeSettings:
        values = RuntimeSettings.from_settings(base).model_dump()
        rows = self.session.scalars(
            select(SettingRow).where(SettingRow.key.in_(ALLOWED_SETTING_FIELDS))
        )
        for row in rows:
            values[row.key] = self._parse(row.key, row.value)
        return RuntimeSettings.model_validate(values)

    @staticmethod
    def _ensure_allowed(key: str) -> None:
        if key not in ALLOWED_SETTING_FIELDS:
            raise ValueError(f"{key!r} is not an allowed runtime setting")

    @staticmethod
    def _parse(key: str, value: str | Decimal | int) -> Decimal | int:
        try:
            if isinstance(value, bool):
                raise ValueError
            candidate = dict(_VALID_RUNTIME_BASE)
            if key in _INTEGER_RUNTIME_FIELDS:
                if isinstance(value, Decimal) and value != value.to_integral_value():
                    raise ValueError
                candidate[key] = int(value)
            else:
                candidate[key] = value
            validated = RuntimeSettings.model_validate(candidate)
            return getattr(validated, key)
        except (ValidationError, ValueError):
            raise ValueError(f"invalid value for {key!r}") from None


_STRUCTURED_SECRET = re.compile(
    r'''(?ix)
    (?P<prefix>
        ["']?
        (?:api[_-]?key|access[_-]?token|client[_-]?(?:id|secret)|password|token|ntfy[_-]?topic)
        ["']?\s*[:=]\s*
    )
    (?P<value>"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;&}]+)
    '''
)
_BEARER_SECRET = re.compile(r"(?i)\b(bearer\s+)([^\s,;]+)")
_AUTHORIZATION_SECRET = re.compile(
    r'''(?ix)
    (?P<prefix>["']?authorization["']?\s*[:=]\s*)
    (?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\r\n]+)
    '''
)
_COOKIE_SECRET = re.compile(
    r'''(?ix)
    (?P<prefix>["']?(?:set-)?cookie["']?\s*[:=]\s*)
    (?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\r\n]+)
    '''
)
_URL_USERINFO_SECRET = re.compile(r"(://)[^/\s:@]+:[^/@\s]+@")


def _redact_error(error: str | None) -> str | None:
    if error is None:
        return None
    redacted = _STRUCTURED_SECRET.sub(r"\g<prefix>[REDACTED]", error)
    redacted = _BEARER_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = _URL_USERINFO_SECRET.sub(r"\1[REDACTED]@", redacted)
    redacted = _AUTHORIZATION_SECRET.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _COOKIE_SECRET.sub(r"\g<prefix>[REDACTED]", redacted)
    return redacted[:2000]
