from enum import Enum


class Team(str, Enum):
    TEXANS = "texans"
    AGGIES = "aggies"


class Source(str, Enum):
    STUBHUB = "stubhub"
    TICKETMASTER = "ticketmaster"
    SEATGEEK = "seatgeek"
    TICKPICK = "tickpick"
    MANUAL = "manual"


class ObservationKind(str, Enum):
    LISTING = "listing"
    EVENT_FLOOR = "event_floor"
    EVENT_AGGREGATE = "event_aggregate"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OpportunityStatus(str, Enum):
    NEW = "new"
    WATCHING = "watching"
    PASSED = "passed"
    PURCHASED = "purchased"
    SOLD = "sold"
    EXPIRED = "expired"
