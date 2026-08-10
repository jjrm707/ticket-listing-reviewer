"""Conservative matching for the explicitly supported home venues only."""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import unicodedata

from .enums import Team
from .models import ExternalEvent


HOME_VENUES: dict[Team, frozenset[str]] = {
    Team.TEXANS: frozenset({"nrg stadium", "reliant stadium"}),
    Team.AGGIES: frozenset({"kyle field"}),
}

_ZERO = Decimal("0.0000")
_SCORE_QUANTUM = Decimal("0.0001")
_MAX_KICKOFF_DIFFERENCE = timedelta(hours=12)
_SECONDS_PER_HOUR = Decimal("3600")
_NFL_NICKNAMES = frozenset(
    {
        "49ers", "bears", "bengals", "bills", "broncos", "browns",
        "buccaneers", "cardinals", "chargers", "chiefs", "colts",
        "commanders", "cowboys", "dolphins", "eagles", "falcons",
        "giants", "jaguars", "jets", "lions", "packers", "panthers",
        "patriots", "raiders", "rams", "ravens", "saints", "seahawks",
        "steelers", "texans", "titans", "vikings",
    }
)


def normalize_label(value: str) -> str:
    """Produce a comparable label without inferring any semantic equivalence."""
    if not isinstance(value, str):
        raise ValueError("label must be a string")

    normalized = unicodedata.normalize("NFKC", value).casefold()
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return " ".join(without_punctuation.split())


def normalize_opponent(value: str) -> str:
    """Normalize a stable NFL nickname without guessing unknown identities."""
    label = normalize_label(value).removesuffix(" football").strip()
    tokens = label.split()
    if tokens and tokens[-1] in _NFL_NICKNAMES:
        return tokens[-1]
    return label


def is_supported_home_game(event: ExternalEvent) -> bool:
    """Return whether an event is at an explicitly supported team's home stadium."""
    venues = HOME_VENUES.get(event.team)
    return bool(
        event.is_home
        and not event.is_parking
        and venues is not None
        and normalize_label(event.venue) in venues
    )


def _kickoff_difference_seconds(left: ExternalEvent, right: ExternalEvent) -> Decimal:
    difference = abs(left.starts_at - right.starts_at)
    return Decimal(difference.days * 86_400 + difference.seconds) + (
        Decimal(difference.microseconds) / Decimal(1_000_000)
    )


def _opponent_similarity(left: str, right: str) -> Decimal:
    left_tokens = set(normalize_opponent(left).split())
    right_tokens = set(normalize_opponent(right).split())
    if not left_tokens or not right_tokens:
        return Decimal("0")
    return Decimal(len(left_tokens & right_tokens)) / Decimal(len(left_tokens | right_tokens))


def event_match_score(left: ExternalEvent, right: ExternalEvent) -> Decimal:
    """Score a same-team, supported-home-game candidate from 0 through 1.

    The score is 20% team identity, 50% opponent token Jaccard similarity,
    20% linear kickoff proximity within twelve hours, and 10% explicit,
    team-specific venue alias equivalence.  It is a comparison aid, not a
    venue or home-game inference mechanism.
    """
    if left.team != right.team:
        return _ZERO
    if not is_supported_home_game(left) or not is_supported_home_game(right):
        return _ZERO
    if left.starts_at.date() != right.starts_at.date():
        return _ZERO

    kickoff_difference = abs(left.starts_at - right.starts_at)
    if kickoff_difference > _MAX_KICKOFF_DIFFERENCE:
        return _ZERO

    opponent_score = _opponent_similarity(left.opponent, right.opponent)
    proximity_score = Decimal("1") - (
        _kickoff_difference_seconds(left, right) / (_SECONDS_PER_HOUR * Decimal("12"))
    )
    venue_score = Decimal(
        normalize_label(left.venue) in HOME_VENUES[left.team]
        and normalize_label(right.venue) in HOME_VENUES[right.team]
    )
    score = (
        Decimal("0.20")
        + (Decimal("0.50") * opponent_score)
        + (Decimal("0.20") * proximity_score)
        + (Decimal("0.10") * venue_score)
    )
    return min(Decimal("1"), max(Decimal("0"), score)).quantize(
        _SCORE_QUANTUM, rounding=ROUND_HALF_UP
    )
