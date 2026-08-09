import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.factories import make_estimate
from ticket_reviewer.config import Settings
from ticket_reviewer.data.repositories import (
    OpportunityRepository,
    OutcomeRepository,
)
from ticket_reviewer.data.schema import (
    AlertRow,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
    SourceEventRow,
)
from ticket_reviewer.services.alerts import AlertService
from ticket_reviewer.web import routes

from .conftest import NOW, seed_opportunity


def _csrf(client) -> str:
    response = client.get("/manual")
    found = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert found is not None
    return found.group(1)


def _assert_security_headers(response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def _opportunity(session_factory):
    with session_factory() as session:
        event, _observation, opportunity = seed_opportunity(
            session, profit=Decimal("63.00")
        )
        session.commit()
        return event.id, opportunity.id


def _later_opportunity(session, first_id: int, *, profit: Decimal, minutes: int) -> int:
    first = session.get(OpportunityRow, first_id)
    candidate = session.get(ObservationRow, first.observation_id)
    later = ObservationRow(
        event_id=first.event_id,
        source=candidate.source,
        event_external_id=candidate.event_external_id,
        observed_at=candidate.observed_at + timedelta(minutes=minutes),
        kind=candidate.kind,
        currency=candidate.currency,
        pair_price=Decimal("190.00"),
        buyer_fees=candidate.buyer_fees,
        estimated_tax=candidate.estimated_tax,
        section=candidate.section,
        row=candidate.row,
        quantity_available=candidate.quantity_available,
        can_buy_pair=candidate.can_buy_pair,
        listing_id=candidate.listing_id,
        listing_identity=candidate.listing_identity,
        listing_url=candidate.listing_url,
        freshness_at=candidate.freshness_at + timedelta(minutes=minutes),
    )
    session.add(later)
    session.flush()
    acquisition = Decimal("225.00")
    return OpportunityRepository(session).save_estimate(
        first.event_id,
        later.id,
        make_estimate(
            acquisition_total=acquisition,
            projected_resale_gross=acquisition + profit,
            projected_proceeds=acquisition + profit,
            estimated_net_profit=profit,
            roi=profit / acquisition,
            actionable=True,
            scenarios=(),
        ),
    )


def test_sold_outcome_requires_actual_proceeds(client, session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)

    response = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={"status": "sold", "csrf_token": _csrf(client)},
    )

    assert response.status_code == 422
    _assert_security_headers(response)
    assert 'role="alert"' in response.text
    assert f'action="/opportunities/{opportunity_id}/status"' in response.text
    assert "Outcome was not saved" in response.text
    with session_factory() as session:
        assert session.get(OpportunityRow, opportunity_id).status == "new"
        assert session.scalars(select(OutcomeRow)).all() == []


def test_purchased_then_sold_preserves_actual_acquisition(client, session_factory):
    event_id, opportunity_id = _opportunity(session_factory)
    token = _csrf(client)

    purchased = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={
            "status": "purchased",
            "actual_acquisition": "245.50",
            "actual_proceeds": "",
            "actual_fees": "",
            "notes": "Bought after review",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    sold = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={
            "status": "sold",
            "actual_acquisition": "",
            "actual_proceeds": "330.00",
            "actual_fees": "49.50",
            "notes": "Completed sale",
            "csrf_token": token,
        },
        follow_redirects=False,
    )

    assert purchased.status_code == 303
    assert sold.status_code == 303
    assert sold.headers["location"] == f"/events/{event_id}?result=outcome-updated"
    _assert_security_headers(sold)
    with session_factory() as session:
        outcome = session.scalar(
            select(OutcomeRow).where(OutcomeRow.opportunity_id == opportunity_id)
        )
        assert outcome.status == "sold"
        assert outcome.actual_acquisition == Decimal("245.50")
        assert outcome.actual_proceeds == Decimal("330.00")
        assert outcome.actual_fees == Decimal("49.50")
        assert session.get(OpportunityRow, opportunity_id).status == "sold"


def test_new_status_is_never_user_selectable_or_accepted(client, session_factory):
    event_id, opportunity_id = _opportunity(session_factory)

    page = client.get(f"/events/{event_id}")
    response = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={"status": "new", "csrf_token": _csrf(client)},
    )

    assert 'value="new"' not in page.text
    assert 'action="/opportunities/' in page.text
    assert response.status_code == 422


def test_opportunity_dashboard_also_has_reusable_outcome_controls(
    client, session_factory
):
    _event_id, opportunity_id = _opportunity(session_factory)

    response = client.get("/")

    assert response.status_code == 200
    assert f'action="/opportunities/{opportunity_id}/status"' in response.text
    form = re.search(
        rf'<form class="outcome-form" action="/opportunities/{opportunity_id}/status".*?</form>',
        response.text,
        re.DOTALL,
    )
    assert form is not None
    assert 'name="csrf_token"' in form.group(0)
    assert 'value="new"' not in form.group(0)


def test_outcome_rejects_duplicate_unknown_and_missing_csrf(client, session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    token = _csrf(client)
    path = f"/opportunities/{opportunity_id}/status"

    duplicate = client.post(
        path,
        content=f"status=watching&status=passed&csrf_token={token}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    unknown = client.post(
        path,
        data={"status": "watching", "redirect_to": "https://evil.example", "csrf_token": token},
    )
    missing = client.post(path, data={"status": "watching"})

    assert duplicate.status_code == 400
    assert unknown.status_code == 400
    assert missing.status_code == 400
    with session_factory() as session:
        assert session.get(OpportunityRow, opportunity_id).status == "new"
        assert session.scalars(select(OutcomeRow)).all() == []


@pytest.mark.parametrize(
    ("status", "values"),
    [
        ("new", {}),
        ("purchased", {}),
        ("purchased", {"actual_acquisition": Decimal("0.00")}),
        ("purchased", {"actual_acquisition": Decimal("1.001")}),
        ("sold", {"actual_proceeds": Decimal("10.00")}),
        (
            "sold",
            {"actual_proceeds": Decimal("10.00"), "actual_fees": Decimal("-0.01")},
        ),
        ("watching", {"actual_proceeds": Decimal("10.00")}),
        ("passed", {"notes": "control\x00text"}),
        ("passed", {"notes": "direction\u202etext"}),
        (
            "sold",
            {
                "actual_proceeds": Decimal("10.00"),
                "actual_fees": Decimal("-0.00"),
            },
        ),
    ],
)
def test_outcome_repository_rejects_invalid_direct_calls_without_mutation(
    session_factory, status, values
):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        with pytest.raises(ValueError):
            OutcomeRepository(session).save(opportunity_id, status, **values)
        assert session.get(OpportunityRow, opportunity_id).status == "new"
        assert session.scalars(select(OutcomeRow)).all() == []


def test_outcome_repository_clears_money_for_nonmonetary_state(session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        repository = OutcomeRepository(session)
        repository.save(
            opportunity_id,
            "sold",
            actual_acquisition=Decimal("200.00"),
            actual_proceeds=Decimal("300.00"),
            actual_fees=Decimal("45.00"),
        )
        repository.save(opportunity_id, "expired", notes="No longer available")
        outcome = repository.get(opportunity_id)

        assert outcome.status == "expired"
        assert outcome.actual_acquisition is None
        assert outcome.actual_proceeds is None
        assert outcome.actual_fees is None
        assert outcome.notes == "No longer available"


def test_identical_outcome_submission_does_not_rewrite_timestamps(session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        repository = OutcomeRepository(session)
        repository.save(opportunity_id, "watching", notes="Track this listing")
        session.flush()
        outcome = repository.get(opportunity_id)
        opportunity = session.get(OpportunityRow, opportunity_id)
        first_outcome_updated = outcome.updated_at
        first_opportunity_updated = opportunity.updated_at

        repeated_id = repository.save(
            opportunity_id, "watching", notes="Track this listing"
        )

        assert repeated_id == outcome.id
        assert outcome.updated_at == first_outcome_updated
        assert opportunity.updated_at == first_opportunity_updated


@pytest.mark.parametrize(
    ("status", "values"),
    [
        ("passed", {}),
        ("purchased", {"actual_acquisition": Decimal("225.00")}),
        (
            "sold",
            {"actual_proceeds": Decimal("300.00"), "actual_fees": Decimal("45.00")},
        ),
        ("expired", {}),
    ],
)
def test_terminal_outcome_suppresses_later_estimate_for_same_listing_lineage(
    session_factory, status, values
):
    event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        OutcomeRepository(session).save(opportunity_id, status, **values)
        later_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )

        assert session.get(OpportunityRow, later_id).status == status


@pytest.mark.parametrize(
    ("status", "values"),
    [
        ("passed", {}),
        ("purchased", {"actual_acquisition": Decimal("225.00")}),
        (
            "sold",
            {"actual_proceeds": Decimal("300.00"), "actual_fees": Decimal("45.00")},
        ),
        ("expired", {}),
    ],
)
def test_terminal_outcome_suppresses_stale_scanner_snapshot_at_send_time(
    session_factory, status, values
):
    event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        first = session.get(OpportunityRow, opportunity_id)
        candidate = session.get(ObservationRow, first.observation_id)
        session.add(
            SourceEventRow(
                event_id=event_id,
                source=candidate.source,
                external_id=candidate.event_external_id,
                url="https://www.stubhub.com/event/123",
                raw_name="Texans vs Colts",
                last_seen=NOW,
            )
        )
        stale_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )
        OutcomeRepository(session).save(opportunity_id, status, **values)
        assert session.get(OpportunityRow, stale_id).status == "new"
        session.commit()

    decision = AlertService(
        Settings(_env_file=None, dry_run=True), session_factory
    ).evaluate_and_send(stale_id, NOW + timedelta(minutes=2))

    assert decision.reason == "ineligible status"


def test_latest_terminal_marker_overrides_older_watching_marker_at_send_time(
    session_factory,
):
    event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        first = session.get(OpportunityRow, opportunity_id)
        candidate = session.get(ObservationRow, first.observation_id)
        session.add(
            SourceEventRow(
                event_id=event_id,
                source=candidate.source,
                external_id=candidate.event_external_id,
                url="https://www.stubhub.com/event/123",
                raw_name="Texans vs Colts",
                last_seen=NOW,
            )
        )
        OutcomeRepository(session).save(opportunity_id, "watching")
        later_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )
        OutcomeRepository(session).save(later_id, "passed")
        session.commit()

    decision = AlertService(
        Settings(_env_file=None, dry_run=True), session_factory
    ).evaluate_and_send(opportunity_id, NOW + timedelta(minutes=2))

    assert decision.reason == "ineligible status"


def test_watching_uses_marked_profit_as_cross_scan_baseline_without_prior_alert(
    session_factory,
):
    event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        first = session.get(OpportunityRow, opportunity_id)
        candidate = session.get(ObservationRow, first.observation_id)
        session.add(
            SourceEventRow(
                event_id=event_id,
                source=candidate.source,
                external_id=candidate.event_external_id,
                url="https://www.stubhub.com/event/123",
                raw_name="Texans vs Colts",
                last_seen=NOW,
            )
        )
        outcome_id = OutcomeRepository(session).save(opportunity_id, "watching")
        session.get(OutcomeRow, outcome_id).updated_at = NOW
        below_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("82.99"), minutes=1
        )
        enough_id = _later_opportunity(
            session, below_id, profit=Decimal("83.00"), minutes=2
        )
        small_repeat_id = _later_opportunity(
            session, enough_id, profit=Decimal("84.00"), minutes=3
        )
        next_improvement_id = _later_opportunity(
            session, small_repeat_id, profit=Decimal("103.00"), minutes=4
        )
        session.commit()

    service = AlertService(Settings(_env_file=None, dry_run=True), session_factory)
    below = service.evaluate_and_send(below_id, NOW + timedelta(minutes=5))
    enough = service.evaluate_and_send(enough_id, NOW + timedelta(minutes=5))
    small_repeat = service.evaluate_and_send(
        small_repeat_id, NOW + timedelta(minutes=5)
    )
    next_improvement = service.evaluate_and_send(
        next_improvement_id, NOW + timedelta(minutes=5)
    )

    assert below.reason == "repeat improvement required"
    assert enough.should_send is True
    assert small_repeat.reason == "repeat improvement required"
    assert next_improvement.should_send is True
    with session_factory() as session:
        assert len(session.scalars(select(AlertRow)).all()) == 2


def test_changing_later_lineage_status_updates_all_snapshots_coherently(session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        OutcomeRepository(session).save(opportunity_id, "passed")
        later_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )
        OutcomeRepository(session).save(later_id, "watching", notes="Track again")
        future_id = _later_opportunity(
            session, later_id, profit=Decimal("84.00"), minutes=2
        )

        statuses = session.scalars(
            select(OpportunityRow.status).order_by(OpportunityRow.id)
        ).all()
        first_outcome = OutcomeRepository(session).get(opportunity_id)
        later_outcome = OutcomeRepository(session).get(later_id)

        assert statuses == ["passed", "watching", "watching"]
        assert first_outcome.status == statuses[0]
        assert later_outcome.status == statuses[1]
        assert session.get(OpportunityRow, future_id).status == "watching"
        assert (later_outcome.status, later_outcome.notes) == (
            "watching",
            "Track again",
        )


def test_later_sold_snapshot_preserves_lineage_purchase_cost(session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        repository = OutcomeRepository(session)
        repository.save(
            opportunity_id,
            "purchased",
            actual_acquisition=Decimal("245.50"),
        )
        later_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )

        repository.save(
            later_id,
            "sold",
            actual_proceeds=Decimal("330.00"),
            actual_fees=Decimal("49.50"),
        )

        later_outcome = repository.get(later_id)
        assert later_outcome.actual_acquisition == Decimal("245.50")


def test_latest_action_on_historical_snapshot_controls_future_lineage_state(
    session_factory,
):
    _event_id, opportunity_id = _opportunity(session_factory)
    with session_factory() as session:
        repository = OutcomeRepository(session)
        repository.save(opportunity_id, "passed")
        inherited_id = _later_opportunity(
            session, opportunity_id, profit=Decimal("83.00"), minutes=1
        )

        repository.save(opportunity_id, "watching", notes="Reactivate lineage")
        future_id = _later_opportunity(
            session, inherited_id, profit=Decimal("84.00"), minutes=2
        )

        assert session.get(OpportunityRow, opportunity_id).status == "watching"
        assert session.get(OpportunityRow, inherited_id).status == "passed"
        assert session.get(OpportunityRow, future_id).status == "watching"
        assert repository.get(opportunity_id).notes == "Reactivate lineage"


def test_concurrent_outcome_updates_leave_one_coherent_row(client, session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    token = _csrf(client)
    submissions = (
        {"status": "watching", "notes": "First complete state", "csrf_token": token},
        {"status": "passed", "notes": "Second complete state", "csrf_token": token},
    )

    def submit(values):
        return client.post(
            f"/opportunities/{opportunity_id}/status",
            data=values,
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, submissions))

    assert [response.status_code for response in responses] == [303, 303]
    with session_factory() as session:
        outcomes = session.scalars(select(OutcomeRow)).all()
        assert len(outcomes) == 1
        final_pair = (outcomes[0].status, outcomes[0].notes)
        assert final_pair in {
            ("watching", "First complete state"),
            ("passed", "Second complete state"),
        }
        assert session.get(OpportunityRow, opportunity_id).status == final_pair[0]


def test_outcome_notes_are_autoescaped_on_event_page(client, session_factory):
    event_id, opportunity_id = _opportunity(session_factory)
    notes = '<img src=x onerror="private-script()">'
    response = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={
            "status": "watching",
            "notes": notes,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )

    page = client.get(response.headers["location"])

    assert "&lt;img" in page.text
    assert "private-script()" in page.text
    assert notes not in page.text


def test_dashboard_preserves_a_recorded_zero_actual_fee(client, session_factory):
    _event_id, opportunity_id = _opportunity(session_factory)
    response = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={
            "status": "sold",
            "actual_proceeds": "300.00",
            "actual_fees": "0.00",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    dashboard = client.get("/")
    form = re.search(
        rf'<form class="outcome-form" action="/opportunities/{opportunity_id}/status".*?</form>',
        dashboard.text,
        re.DOTALL,
    )
    assert form is not None
    assert 'name="actual_fees" inputmode="decimal" value="0.00"' in form.group(0)


@pytest.mark.parametrize("opportunity_id", [True, 0, -1])
def test_outcome_repository_rejects_nonpositive_or_bool_ids(
    session_factory, opportunity_id
):
    with session_factory() as session:
        with pytest.raises(ValueError, match="positive integer"):
            OutcomeRepository(session).save(opportunity_id, "watching")


def test_outcome_repository_failure_renders_static_inline_error(
    client, session_factory, monkeypatch
):
    _event_id, opportunity_id = _opportunity(session_factory)
    original_save = routes.OutcomeRepository.save

    def fail(repository, *args, **kwargs):
        original_save(repository, *args, **kwargs)
        raise RuntimeError("private database detail")

    monkeypatch.setattr(routes.OutcomeRepository, "save", fail)
    response = client.post(
        f"/opportunities/{opportunity_id}/status",
        data={"status": "watching", "csrf_token": _csrf(client)},
    )

    assert response.status_code == 503
    assert 'role="alert"' in response.text
    assert f'action="/opportunities/{opportunity_id}/status"' in response.text
    assert "Outcome could not be saved" in response.text
    assert "private database detail" not in response.text
    with session_factory() as session:
        assert session.get(OpportunityRow, opportunity_id).status == "new"
        assert session.scalars(select(OutcomeRow)).all() == []


def test_outcome_commit_failure_rolls_back_row_and_status(
    client, session_factory, monkeypatch
):
    _event_id, opportunity_id = _opportunity(session_factory)
    token = _csrf(client)

    def fail_commit(_session):
        raise RuntimeError("private commit detail")

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail_commit)
        response = client.post(
            f"/opportunities/{opportunity_id}/status",
            data={"status": "watching", "csrf_token": token},
        )

    assert response.status_code == 503
    assert "Outcome could not be saved" in response.text
    assert "private commit detail" not in response.text
    with session_factory() as session:
        assert session.get(OpportunityRow, opportunity_id).status == "new"
        assert session.scalars(select(OutcomeRow)).all() == []
