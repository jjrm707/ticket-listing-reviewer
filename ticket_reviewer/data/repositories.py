"""Small transaction-scoped repositories for persistence consumers."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
from collections.abc import Mapping
import unicodedata

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
_SETTING_MONEY = re.compile(r"(?:0|[1-9]\d{0,9})(?:\.\d{1,2})?")
_SETTING_RATE = re.compile(r"0(?:\.\d{1,4})?")
_SETTING_INTEGER = re.compile(r"[1-9]\d{0,3}")
_MAX_STORED_MONEY = Decimal("9999999999.99")
_USER_OUTCOME_STATUSES = frozenset(
    {
        OpportunityStatus.PASSED,
        OpportunityStatus.WATCHING,
        OpportunityStatus.PURCHASED,
        OpportunityStatus.SOLD,
        OpportunityStatus.EXPIRED,
    }
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


@dataclass(frozen=True, slots=True)
class ObservationAddResult:
    observation_id: int
    inserted: bool


class ObservationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, event_id: int, observation: SourceObservation) -> int:
        return self.add_with_status(event_id, observation).observation_id

    def add_with_status(
        self, event_id: int, observation: SourceObservation
    ) -> ObservationAddResult:
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
            return ObservationAddResult(existing.id, inserted=False)

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
        return ObservationAddResult(row.id, inserted=True)

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
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("event ID must be a positive integer")
        if type(observation_id) is not int or observation_id <= 0:
            raise ValueError("candidate observation ID must be a positive integer")
        candidate = self.session.get(ObservationRow, observation_id)
        if candidate is None:
            raise ValueError("candidate observation does not exist")
        if candidate.event_id != event_id:
            raise ValueError("candidate observation belongs to another event")
        for scenario in estimate.scenarios:
            if len(scenario.comparable_observation_ids) != scenario.comparable_count:
                raise ValueError(
                    "new estimates require exact comparable observation IDs"
                )
            for comparable_id in scenario.comparable_observation_ids:
                if type(comparable_id) is not int or comparable_id <= 0:
                    raise ValueError("comparable observation IDs must be positive integers")
                if comparable_id == observation_id:
                    raise ValueError("candidate observation cannot be its own comparable")
                comparable = self.session.get(ObservationRow, comparable_id)
                if comparable is None:
                    raise ValueError("comparable observation does not exist")
                if comparable.event_id != event_id:
                    raise ValueError("comparable observation belongs to another event")
                if comparable.source != scenario.marketplace.value:
                    raise ValueError("comparable observation source does not match scenario")
        latest_outcome_status = self.session.scalar(
            select(OutcomeRow.status)
            .join(
                OpportunityRow,
                OutcomeRow.opportunity_id == OpportunityRow.id,
            )
            .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
            .where(
                ObservationRow.source == candidate.source,
                ObservationRow.event_external_id == candidate.event_external_id,
                ObservationRow.listing_identity == candidate.listing_identity,
            )
            .order_by(OutcomeRow.updated_at.desc(), OutcomeRow.id.desc())
            .limit(1)
        )
        try:
            inherited_status = (
                OpportunityStatus(latest_outcome_status).value
                if latest_outcome_status is not None
                else OpportunityStatus.NEW.value
            )
        except (TypeError, ValueError):
            inherited_status = OpportunityStatus.NEW.value
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
            status=inherited_status,
            scenarios=[
                {
                    "marketplace": scenario.marketplace.value,
                    "projected_resale_gross": str(scenario.projected_resale_gross),
                    "seller_fee_rate": str(scenario.seller_fee_rate),
                    "projected_proceeds": str(scenario.projected_proceeds),
                    "comparable_count": scenario.comparable_count,
                    "comparable_observation_ids": list(
                        scenario.comparable_observation_ids
                    ),
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

    def find_successful_by_fingerprint(self, fingerprint: str) -> AlertRow | None:
        return self.session.scalar(
            select(AlertRow).where(
                AlertRow.fingerprint == fingerprint,
                AlertRow.provider_message_id.is_not(None),
                AlertRow.provider_message_id != "",
            )
        )

    def latest_for_lineage(
        self, source: str, event_external_id: str, listing_identity: str
    ) -> AlertRow | None:
        """Return the latest successful alert for one stable listing lineage."""
        return self.session.scalar(
            select(AlertRow)
            .join(OpportunityRow, AlertRow.opportunity_id == OpportunityRow.id)
            .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
            .where(
                ObservationRow.source == source,
                ObservationRow.event_external_id == event_external_id,
                ObservationRow.listing_identity == listing_identity,
                AlertRow.provider_message_id.is_not(None),
                AlertRow.provider_message_id != "",
            )
            .order_by(AlertRow.sent_at.desc(), AlertRow.id.desc())
            .limit(1)
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
            if not existing.provider_message_id:
                existing.opportunity_id = opportunity_id
                existing.sent_at = sent_at
                existing.profit_at_send = profit_at_send
                existing.provider_message_id = provider_message_id
                self.session.flush()
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
        if type(opportunity_id) is not int or opportunity_id <= 0:
            raise ValueError("opportunity ID must be a positive integer")
        try:
            status_enum = OpportunityStatus(status)
        except (TypeError, ValueError):
            raise ValueError("invalid outcome status") from None
        if status_enum not in _USER_OUTCOME_STATUSES:
            raise ValueError("invalid outcome status")
        acquisition = self._money(actual_acquisition, positive=True)
        proceeds = self._money(actual_proceeds, positive=True)
        fees = self._money(actual_fees, positive=False)
        clean_notes = self._notes(notes)
        if status_enum is OpportunityStatus.PURCHASED:
            if acquisition is None or proceeds is not None or fees is not None:
                raise ValueError("invalid purchased outcome")
        elif status_enum is OpportunityStatus.SOLD:
            if proceeds is None or fees is None:
                raise ValueError("invalid sold outcome")
        elif any(value is not None for value in (acquisition, proceeds, fees)):
            raise ValueError("monetary values are not valid for this outcome")

        opportunity = self.session.get(OpportunityRow, opportunity_id)
        if opportunity is None:
            raise LookupError(f"opportunity {opportunity_id} does not exist")
        candidate = self.session.get(ObservationRow, opportunity.observation_id)
        if candidate is None or candidate.event_id != opportunity.event_id:
            raise LookupError("opportunity context is unavailable")
        row = self.session.scalar(
            select(OutcomeRow).where(OutcomeRow.opportunity_id == opportunity_id)
        )
        if status_enum is OpportunityStatus.SOLD and acquisition is None:
            lineage_acquisition = self.session.scalar(
                select(OutcomeRow.actual_acquisition)
                .join(
                    OpportunityRow,
                    OutcomeRow.opportunity_id == OpportunityRow.id,
                )
                .join(
                    ObservationRow,
                    OpportunityRow.observation_id == ObservationRow.id,
                )
                .where(
                    ObservationRow.source == candidate.source,
                    ObservationRow.event_external_id == candidate.event_external_id,
                    ObservationRow.listing_identity == candidate.listing_identity,
                    OutcomeRow.actual_acquisition.is_not(None),
                )
                .order_by(OutcomeRow.updated_at.desc(), OutcomeRow.id.desc())
                .limit(1)
            )
            if lineage_acquisition is not None:
                acquisition = self._money(lineage_acquisition, positive=True)
        outcome_changed = row is None or (
            row.status != status_enum.value
            or row.actual_acquisition != acquisition
            or row.actual_proceeds != proceeds
            or row.actual_fees != fees
            or row.notes != clean_notes
        )
        status_changed = opportunity.status != status_enum.value
        if not outcome_changed and not status_changed:
            return row.id
        now = utc_now()
        if row is None:
            row = OutcomeRow(
                opportunity_id=opportunity_id,
                status=status_enum.value,
                actual_acquisition=acquisition,
                actual_proceeds=proceeds,
                actual_fees=fees,
                notes=clean_notes,
                updated_at=now,
            )
            self.session.add(row)
        elif outcome_changed:
            row.status = status_enum.value
            row.actual_acquisition = acquisition
            row.actual_proceeds = proceeds
            row.actual_fees = fees
            row.notes = clean_notes
            row.updated_at = now
        if status_changed:
            opportunity.status = status_enum.value
            opportunity.updated_at = now
        self.session.flush()
        return row.id

    def get(self, opportunity_id: int) -> OutcomeRow | None:
        return self.session.scalar(
            select(OutcomeRow).where(OutcomeRow.opportunity_id == opportunity_id)
        )

    @staticmethod
    def _money(value: object, *, positive: bool) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("invalid outcome money")
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int) or exponent > 0 or exponent < -2:
            raise ValueError("invalid outcome money")
        if (
            value.is_signed()
            or value > _MAX_STORED_MONEY
            or value < 0
            or (positive and value <= 0)
        ):
            raise ValueError("invalid outcome money")
        return value

    @staticmethod
    def _notes(value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("invalid outcome notes")
        if any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("invalid outcome notes")
        return value


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
        self._store(key, parsed)

    def set_all(self, values: Mapping[str, object]) -> None:
        if not isinstance(values, Mapping) or set(values) != ALLOWED_SETTING_FIELDS:
            raise ValueError("complete runtime settings are required")
        parsed = {
            key: self._parse(key, values[key]) for key in sorted(ALLOWED_SETTING_FIELDS)
        }
        for key, value in parsed.items():
            self._store(key, value)

    def _store(self, key: str, parsed: Decimal | int) -> None:
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
            if key in _INTEGER_RUNTIME_FIELDS:
                if isinstance(value, bool) or isinstance(value, float):
                    raise ValueError
                if isinstance(value, str):
                    if _SETTING_INTEGER.fullmatch(value) is None:
                        raise ValueError
                    parsed: Decimal | int = int(value)
                elif type(value) is int:
                    parsed = value
                elif isinstance(value, Decimal):
                    if (
                        not value.is_finite()
                        or value.as_tuple().exponent != 0
                        or value != value.to_integral_value()
                    ):
                        raise ValueError
                    parsed = int(value)
                else:
                    raise ValueError
            else:
                if isinstance(value, bool) or isinstance(value, float):
                    raise ValueError
                pattern = _SETTING_RATE if key.endswith("seller_fee_rate") else _SETTING_MONEY
                scale = 4 if key.endswith("seller_fee_rate") else 2
                if isinstance(value, str):
                    if pattern.fullmatch(value) is None:
                        raise ValueError
                    parsed = Decimal(value)
                elif type(value) is int:
                    parsed = Decimal(value)
                elif isinstance(value, Decimal):
                    exponent = value.as_tuple().exponent
                    if (
                        not value.is_finite()
                        or value.is_signed()
                        or not isinstance(exponent, int)
                        or exponent > 0
                        or exponent < -scale
                    ):
                        raise ValueError
                    parsed = value
                else:
                    raise ValueError
                if key in {"alert_profit_threshold", "profit_improvement_threshold"}:
                    if parsed > _MAX_STORED_MONEY:
                        raise ValueError
            candidate = dict(_VALID_RUNTIME_BASE)
            candidate[key] = parsed
            validated = RuntimeSettings.model_validate(candidate)
            return getattr(validated, key)
        except (ArithmeticError, ValidationError, ValueError):
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
