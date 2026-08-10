from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ticket_reviewer.domain.enums import Source, Team
from ticket_reviewer.domain.matching import (
    event_match_score,
    is_supported_home_game,
    normalize_label,
)
from tests.factories import make_event


@pytest.fixture
def texans_event():
    return make_event(
        source=Source.TICKETMASTER,
        external_id="tm-texans-jaguars",
        opponent="Jacksonville Jaguars",
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def aggies_event():
    return make_event(
        source=Source.TICKETMASTER,
        external_id="tm-aggies-lsu",
        team=Team.AGGIES,
        opponent="LSU Tigers",
        venue="Kyle Field",
        starts_at=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def tm_event(texans_event):
    return texans_event


@pytest.fixture
def stubhub_event(texans_event):
    return replace(
        texans_event,
        source=Source.STUBHUB,
        external_id="sh-houston-v-jacksonville",
        opponent="JACKSONVILLE-JAGUARS",
        venue="Reliant Stadium",
        starts_at=texans_event.starts_at + timedelta(minutes=90),
    )


def test_normalize_label_handles_unicode_punctuation_and_whitespace():
    assert normalize_label("  \uff2e\uff32\uff27\u3000Stadium\u2014Home!  ") == "nrg stadium home"


def test_texans_home_game_matches_nrg_alias(texans_event):
    assert is_supported_home_game(replace(texans_event, venue="NRG Stadium"))


def test_texans_historical_reliant_stadium_alias_is_supported(texans_event):
    assert is_supported_home_game(replace(texans_event, venue="Reliant Stadium"))


def test_aggies_home_game_matches_kyle_field_alias(aggies_event):
    assert is_supported_home_game(aggies_event)


def test_aggies_away_game_is_rejected(aggies_event):
    assert not is_supported_home_game(replace(aggies_event, is_home=False))


def test_parking_product_is_rejected(texans_event):
    assert not is_supported_home_game(replace(texans_event, is_parking=True))


def test_unsupported_venue_is_rejected(texans_event):
    assert not is_supported_home_game(replace(texans_event, venue="Toyota Center"))


def test_same_game_across_sources_scores_above_threshold(tm_event, stubhub_event):
    assert event_match_score(tm_event, stubhub_event) >= Decimal("0.85")


def test_stable_nfl_nickname_matches_full_team_name_at_same_kickoff(texans_event):
    full = replace(texans_event, opponent="Indianapolis Colts")
    short = replace(
        texans_event,
        source=Source.STUBHUB,
        external_id="stub-colts",
        opponent="Colts",
    )

    assert event_match_score(full, short) >= Decimal("0.85")


def test_conflicting_teams_score_zero(texans_event):
    aggies_event = replace(texans_event, team=Team.AGGIES, venue="Kyle Field")

    assert event_match_score(texans_event, aggies_event) == Decimal("0.0000")


def test_different_kickoff_calendar_dates_score_zero(texans_event):
    next_date = replace(texans_event, starts_at=texans_event.starts_at + timedelta(hours=7))

    assert event_match_score(texans_event, next_date) == Decimal("0.0000")


def test_kickoffs_more_than_twelve_hours_apart_score_zero(texans_event):
    later_kickoff = replace(texans_event, starts_at=texans_event.starts_at + timedelta(hours=12, minutes=1))

    assert event_match_score(texans_event, later_kickoff) == Decimal("0.0000")


def test_clearly_different_opponents_do_not_clear_match_threshold(texans_event):
    different_opponent = replace(texans_event, opponent="Dallas Cowboys")

    assert event_match_score(texans_event, different_opponent) < Decimal("0.85")


def test_weak_opponent_overlap_does_not_clear_match_threshold(texans_event):
    weak_overlap = replace(texans_event, opponent="Jaguars Youth Football")

    assert event_match_score(texans_event, weak_overlap) < Decimal("0.85")
