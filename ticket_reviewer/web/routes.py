"""Read-only server-rendered dashboard routes."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from ticket_reviewer.data.schema import (
    ConnectorRunRow,
    EventRow,
    ObservationRow,
    OpportunityRow,
    SourceEventRow,
)
from ticket_reviewer.domain.enums import Confidence, OpportunityStatus, Source, Team

from .viewmodels import (
    EventEstimate,
    OpportunityCard,
    clean_text,
    local_time,
    money_text,
    observation_kind_text,
    safe_error,
    freshness_text,
    sanitize_public_url,
    seating_key,
    source_label,
    utc_iso,
)


router = APIRouter()
_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))
_MAX_OPPORTUNITIES = 200
_MAX_EVENT_OBSERVATIONS = 5000
_MAX_EVENT_ESTIMATES = 500
_MAX_SOURCE_EVENTS = 100
_FILTER_NAMES = frozenset(
    {"team", "event_id", "source", "confidence", "min_profit", "max_cost", "status"}
)
_MONEY = re.compile(r"-?(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?")
_POSITIVE_ID = re.compile(r"[1-9]\d{0,8}")


def _bad_filter() -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid dashboard filters")


def _parse_filters(request: Request) -> dict[str, str | int | Decimal]:
    values: dict[str, list[str]] = defaultdict(list)
    for name, value in request.query_params.multi_items():
        if name not in _FILTER_NAMES:
            raise _bad_filter()
        values[name].append(value)
    if any(len(items) != 1 or items[0] == "" for items in values.values()):
        raise _bad_filter()
    parsed: dict[str, str | int | Decimal] = {}
    enum_types = {
        "team": Team,
        "source": Source,
        "confidence": Confidence,
        "status": OpportunityStatus,
    }
    for name, enum_type in enum_types.items():
        if name in values:
            try:
                parsed[name] = enum_type(values[name][0]).value
            except ValueError:
                raise _bad_filter() from None
    if "event_id" in values:
        raw = values["event_id"][0]
        if _POSITIVE_ID.fullmatch(raw) is None:
            raise _bad_filter()
        parsed["event_id"] = int(raw)
    for name in ("min_profit", "max_cost"):
        if name not in values:
            continue
        raw = values[name][0]
        if _MONEY.fullmatch(raw) is None:
            raise _bad_filter()
        value = Decimal(raw)
        if not value.is_finite() or (name == "max_cost" and value < 0):
            raise _bad_filter()
        parsed[name] = value
    return parsed


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", None)
    value = clock() if callable(clock) else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("invalid dashboard clock")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DashboardContext:
    session: Session
    timezone: str
    freshness_minutes: int
    dry_run: bool
    now: datetime


def dashboard_context(request: Request):
    services = getattr(request.app.state, "services", None)
    session_factory = getattr(services, "session_factory", None)
    if not callable(session_factory):
        raise HTTPException(status_code=503, detail="Dashboard data is unavailable")
    session = session_factory()
    try:
        settings = request.app.state.settings
        yield DashboardContext(
            session=session,
            timezone=settings.timezone,
            freshness_minutes=settings.observation_freshness_minutes,
            dry_run=bool(settings.dry_run),
            now=_now(request),
        )
    finally:
        session.close()


@router.get("/", response_class=HTMLResponse)
def opportunities(
    request: Request, context: DashboardContext = Depends(dashboard_context)
):
    session = context.session
    filters = _parse_filters(request)
    lineage = aliased(ObservationRow)
    first_seen = (
        select(func.min(lineage.observed_at))
        .where(
            lineage.source == ObservationRow.source,
            lineage.event_external_id == ObservationRow.event_external_id,
            lineage.listing_identity == ObservationRow.listing_identity,
        )
        .correlate(ObservationRow)
        .scalar_subquery()
    )
    last_seen = (
        select(func.max(lineage.freshness_at))
        .where(
            lineage.source == ObservationRow.source,
            lineage.event_external_id == ObservationRow.event_external_id,
            lineage.listing_identity == ObservationRow.listing_identity,
        )
        .correlate(ObservationRow)
        .scalar_subquery()
    )
    statement = (
        select(OpportunityRow, EventRow, ObservationRow, first_seen, last_seen)
        .join(EventRow, OpportunityRow.event_id == EventRow.id)
        .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
        .where(
            ObservationRow.kind == "listing",
            ObservationRow.can_buy_pair.is_(True),
            ObservationRow.quantity_available >= 2,
        )
    )
    if "team" in filters:
        statement = statement.where(EventRow.team == filters["team"])
    if "event_id" in filters:
        statement = statement.where(EventRow.id == filters["event_id"])
    if "source" in filters:
        statement = statement.where(ObservationRow.source == filters["source"])
    if "confidence" in filters:
        statement = statement.where(OpportunityRow.confidence == filters["confidence"])
    if "status" in filters:
        statement = statement.where(OpportunityRow.status == filters["status"])
    if "min_profit" in filters:
        statement = statement.where(
            OpportunityRow.estimated_net_profit >= filters["min_profit"]
        )
    if "max_cost" in filters:
        statement = statement.where(
            OpportunityRow.acquisition_total <= filters["max_cost"]
        )
    finite_profit = case(
        (
            OpportunityRow.estimated_net_profit.between(
                Decimal("-9999999999.99"), Decimal("9999999999.99")
            ),
            OpportunityRow.estimated_net_profit,
        ),
        else_=None,
    )
    rows = session.execute(
        statement.order_by(
            finite_profit.desc().nullslast(),
            OpportunityRow.updated_at.desc(),
            OpportunityRow.id.desc(),
        ).limit(_MAX_OPPORTUNITIES)
    ).all()
    cards: list[OpportunityCard] = []
    for row in rows:
        try:
            cards.append(
                OpportunityCard.from_row(
                    tuple(row[:3]),
                    context.timezone,
                    now=context.now,
                    freshness_minutes=context.freshness_minutes,
                    first_seen_at=row[3],
                    last_seen_at=row[4],
                )
            )
        except (TypeError, ValueError):
            continue
    return templates.TemplateResponse(
        request,
        "opportunities.html",
        {"cards": cards, "filters": {key: str(value) for key, value in filters.items()}},
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    request: Request,
    event_id: int,
    context: DashboardContext = Depends(dashboard_context),
):
    session = context.session
    if type(event_id) is not int or event_id <= 0:
        raise HTTPException(status_code=404, detail="Event not found")
    event = session.get(EventRow, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    observation_total = session.scalar(
        select(func.count(ObservationRow.id)).where(ObservationRow.event_id == event_id)
    ) or 0
    observations = list(
        session.scalars(
            select(ObservationRow)
            .where(ObservationRow.event_id == event_id)
            .order_by(ObservationRow.observed_at.desc(), ObservationRow.id.desc())
            .limit(_MAX_EVENT_OBSERVATIONS)
        )
    )
    observations.sort(key=lambda row: (row.observed_at, row.id))
    series: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    history: list[dict[str, str]] = []
    for observation in observations:
        timestamp = utc_iso(observation.observed_at)
        price = observation.pair_price
        finite_price = (
            price if isinstance(price, Decimal) and price.is_finite() and price >= 0 else None
        )
        kind_text = observation_kind_text(observation.kind)
        seat = seating_key(observation)
        history.append(
            {
                "id": str(observation.id),
                "source": source_label(observation.source),
                "kind": kind_text,
                "seat": seat,
                "observed": local_time(observation.observed_at, context.timezone),
                "price": money_text(finite_price),
            }
        )
        if timestamp is not None and finite_price is not None:
            key = (source_label(observation.source), seat, observation.kind)
            series[key].append({"x": timestamp, "y": str(finite_price)})
    datasets = []
    for (source, seat, kind), points in sorted(series.items()):
        wording = observation_kind_text(kind)
        datasets.append(
            {
                "label": f"{source} — {seat} — {wording}",
                "data": sorted(points, key=lambda point: point["x"]),
                "borderDash": [7, 5] if kind == "event_floor" else [],
            }
        )
    source_event_total = session.scalar(
        select(func.count(SourceEventRow.id)).where(SourceEventRow.event_id == event_id)
    ) or 0
    source_events = list(
        session.scalars(
            select(SourceEventRow)
            .where(SourceEventRow.event_id == event_id)
            .order_by(SourceEventRow.last_seen.desc(), SourceEventRow.id.desc())
            .limit(_MAX_SOURCE_EVENTS)
        )
    )
    source_events.sort(key=lambda row: (row.source, row.id))
    estimate_total = session.scalar(
        select(func.count(OpportunityRow.id)).where(OpportunityRow.event_id == event_id)
    ) or 0
    estimate_rows = list(
        session.scalars(
            select(OpportunityRow)
            .where(OpportunityRow.event_id == event_id)
            .order_by(OpportunityRow.created_at.desc(), OpportunityRow.id.desc())
            .limit(_MAX_EVENT_ESTIMATES)
        )
    )
    estimates = []
    for item in estimate_rows:
        try:
            estimates.append(EventEstimate.from_row(item))
        except (TypeError, ValueError):
            continue
    history_notices = []
    if observation_total > _MAX_EVENT_OBSERVATIONS:
        history_notices.append(
            f"Showing newest {_MAX_EVENT_OBSERVATIONS} of {observation_total} observations."
        )
    if estimate_total > _MAX_EVENT_ESTIMATES:
        history_notices.append(
            f"Showing newest {_MAX_EVENT_ESTIMATES} of {estimate_total} estimates."
        )
    if source_event_total > _MAX_SOURCE_EVENTS:
        history_notices.append(
            f"Showing newest {_MAX_SOURCE_EVENTS} of {source_event_total} source references."
        )
    return templates.TemplateResponse(
        request,
        "event_detail.html",
        {
            "event": {
                "team": clean_text(event.team).title(),
                "opponent": clean_text(event.opponent),
                "venue": clean_text(event.venue),
                "kickoff": local_time(event.starts_at, context.timezone),
            },
            "source_events": [
                {
                    "source": source_label(item.source),
                    "external_id": clean_text(item.external_id),
                    "url": sanitize_public_url(item.url, item.source),
                }
                for item in source_events
            ],
            "history": history,
            "history_notices": history_notices,
            "chart": {"datasets": datasets},
            "estimates": estimates,
        },
    )


@router.get("/health", response_class=HTMLResponse)
def health(request: Request, context: DashboardContext = Depends(dashboard_context)):
    session = context.session
    sources = []
    for source in (Source.SEATGEEK, Source.STUBHUB, Source.TICKETMASTER):
        row = session.scalar(
            select(ConnectorRunRow)
            .where(ConnectorRunRow.source == source.value)
            .order_by(ConnectorRunRow.started_at.desc(), ConnectorRunRow.id.desc())
            .limit(1)
        )
        if row is None:
            sources.append(
                {
                    "source": source_label(source),
                    "state": "Not run yet",
                    "started": "Unknown",
                    "finished": "Unknown",
                    "count": "0",
                    "error": None,
                }
            )
            continue
        state = "In progress" if row.success is None else ("Success" if row.success else "Failure")
        sources.append(
            {
                "source": source_label(source),
                "state": state,
                    "started": local_time(row.started_at, context.timezone),
                    "finished": local_time(row.finished_at, context.timezone),
                "count": str(row.observation_count)
                if type(row.observation_count) is int and row.observation_count >= 0
                else "Unknown",
                "error": safe_error(row.redacted_error)
                if row.success is False
                else None,
                "freshness": freshness_text(
                    row.finished_at or row.started_at,
                    context.now,
                    context.freshness_minutes,
                )[0],
            }
        )
    return templates.TemplateResponse(
        request,
        "health.html",
        {"sources": sources, "dry_run": context.dry_run},
    )
