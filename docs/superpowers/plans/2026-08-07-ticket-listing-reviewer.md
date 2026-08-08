# Ticket Listing Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private Windows-local application that scans permitted ticket-market APIs hourly, ranks Houston Texans and Texas A&M football home-game pairs under $400, and sends an iPhone push alert for opportunities with at least $50 estimated net profit.

**Architecture:** A single Python service exposes a localhost FastAPI dashboard, runs an APScheduler job, normalizes each marketplace through a capability-aware connector, and persists snapshots in SQLite. Domain services own matching, conservative resale estimates, confidence, and notification decisions; source failures and event-level-only data degrade explicitly instead of being treated as listing-level inventory.

**Tech Stack:** Python 3.12+, FastAPI, Jinja2, HTMX, Chart.js, SQLAlchemy 2, Alembic, Pydantic Settings, HTTPX, APScheduler 3, SQLite, pytesseract/Pillow, ntfy, pytest, pytest-asyncio, respx.

## Global Constraints

- Support only Houston Texans home games at NRG Stadium and Texas A&M football home games at Kyle Field.
- Evaluate only two adjacent tickets with estimated all-in acquisition cost at or below **$400**.
- Alert only at **$50 or more estimated net profit**; re-alert only after at least a **$20 improvement**.
- Run an immediate scan at Windows sign-in and then every **60 minutes** while the computer is on.
- Use free, official, permitted API access; never scrape or automate restricted marketplace websites.
- Keep Ticketmaster and StubHub restricted capabilities disabled unless the issued key explicitly grants them.
- Treat event-level price floors and aggregates as comparison signals, never as confirmed purchasable pairs.
- Keep the web server bound to **127.0.0.1** by default.
- Keep secrets out of Git, logs, push messages, and database rows.
- Perform no buying, reserving, transferring, listing, repricing, or selling.
- Store money as Decimal values rounded to cents; never use binary floating point for money.
- All datetimes are timezone-aware UTC internally and rendered in America/Chicago.

---

## File Structure

- **pyproject.toml** — package metadata, runtime dependencies, test configuration.
- **.gitignore** — excludes secrets, virtual environments, database files, logs, and screenshots.
- **.env.example** — redacted configuration contract with safe defaults.
- **README.md** — setup, credentials, dry-run, startup-task, and operating instructions.
- **ticket_reviewer/config.py** — typed environment configuration and secret handling.
- **ticket_reviewer/bootstrap.py** — composition root for database, connectors, scanner, alerts, and scheduler dependencies.
- **ticket_reviewer/main.py** — FastAPI application factory and lifespan wiring.
- **ticket_reviewer/domain/enums.py** — teams, sources, observation kinds, confidence, and statuses.
- **ticket_reviewer/domain/models.py** — immutable domain input/output dataclasses.
- **ticket_reviewer/domain/pricing.py** — cent-safe acquisition, proceeds, profit, and ROI functions.
- **ticket_reviewer/domain/matching.py** — event normalization, supported-home-game checks, and source matching.
- **ticket_reviewer/domain/comparables.py** — comparable selection and conservative weighted quantile.
- **ticket_reviewer/domain/scoring.py** — resale projection, confidence, risk explanations, and eligibility.
- **ticket_reviewer/data/db.py** — SQLAlchemy engine/session construction.
- **ticket_reviewer/data/schema.py** — persisted tables.
- **ticket_reviewer/data/repositories.py** — transaction-scoped persistence interfaces.
- **ticket_reviewer/data/migrations/** — Alembic environment and numbered schema migrations.
- **ticket_reviewer/connectors/base.py** — connector protocol, capabilities, and typed failures.
- **ticket_reviewer/connectors/ticketmaster.py** — public Discovery API adapter.
- **ticket_reviewer/connectors/seatgeek.py** — public event aggregate adapter.
- **ticket_reviewer/connectors/stubhub.py** — OAuth and documented catalog/minimum-price adapter.
- **ticket_reviewer/services/scanner.py** — connector isolation, normalization, persistence, and evaluation flow.
- **ticket_reviewer/services/alerts.py** — alert policy, fingerprinting, and ntfy publishing.
- **ticket_reviewer/services/scheduler.py** — immediate/hourly job and single-instance lifecycle.
- **ticket_reviewer/services/ocr.py** — local OCR adapter and conservative listing-text parser.
- **ticket_reviewer/web/routes.py** — dashboard, event, manual review, outcomes, settings, and health routes.
- **ticket_reviewer/web/viewmodels.py** — presentation-only formatting.
- **ticket_reviewer/web/templates/** — server-rendered screens and fragments.
- **ticket_reviewer/web/static/app.css** — small responsive visual system.
- **scripts/run.ps1** — local service launcher.
- **scripts/install_startup_task.ps1** — idempotent Windows Task Scheduler registration.
- **scripts/uninstall_startup_task.ps1** — remove only this application's startup task.
- **tests/factories.py** — deterministic aware-datetime event, observation, and estimate builders shared by tests.
- **tests/** — mirrors package responsibilities with fixtures under **tests/fixtures/**.

### Task 1: Application shell and secure configuration

**Files:**
- Create: **pyproject.toml**
- Create: **.gitignore**
- Create: **.env.example**
- Create: **ticket_reviewer/__init__.py**
- Create: **ticket_reviewer/config.py**
- Create: **ticket_reviewer/main.py**
- Create: **tests/test_config.py**
- Create: **tests/test_app.py**

**Interfaces:**
- Produces: **Settings** with Decimal budget/thresholds, local host/port, API secrets, paths, dry-run flag, and 60-minute interval.
- Produces: **RuntimeSettings** containing only budget, thresholds, freshness, scan interval, and marketplace seller-fee assumptions.
- Produces: **create_app(settings: Settings | None = None) -> FastAPI**.

- [ ] **Step 1: Add the package and test dependencies**

Use this project configuration:

~~~toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "ticket-listing-reviewer"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.14,<2",
  "apscheduler>=3.10,<4",
  "fastapi>=0.115,<1",
  "httpx>=0.28,<1",
  "jinja2>=3.1,<4",
  "pillow>=11,<13",
  "pydantic-settings>=2.7,<3",
  "python-multipart>=0.0.20,<1",
  "pytesseract>=0.3.13,<1",
  "sqlalchemy>=2.0,<3",
  "uvicorn>=0.34,<1",
]

[project.optional-dependencies]
dev = ["pytest>=8,<10", "pytest-asyncio>=0.25,<2", "respx>=0.22,<1"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
~~~

- [ ] **Step 2: Write failing settings and health tests**

~~~python
from decimal import Decimal
from fastapi.testclient import TestClient
from ticket_reviewer.config import Settings
from ticket_reviewer.main import create_app

def test_safe_defaults_are_local_and_dry_run():
    settings = Settings(_env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.scan_interval_minutes == 60
    assert settings.budget_cap == Decimal("400.00")
    assert settings.alert_profit_threshold == Decimal("50.00")
    assert settings.profit_improvement_threshold == Decimal("20.00")
    assert settings.observation_freshness_minutes == 120
    assert settings.stubhub_seller_fee_rate == Decimal("0.15")
    assert settings.dry_run is True

def test_health_route_does_not_expose_secrets():
    settings = Settings(_env_file=None, ticketmaster_api_key="secret")
    response = TestClient(create_app(settings)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dry_run": True}
    assert "secret" not in response.text
~~~

- [ ] **Step 3: Run the tests and verify they fail**

Run: **python -m pytest tests/test_config.py tests/test_app.py -v**

Expected: FAIL because **ticket_reviewer.config** and **ticket_reviewer.main** do not exist.

- [ ] **Step 4: Implement the minimal settings and app factory**

~~~python
# ticket_reviewer/config.py
from decimal import Decimal
from pathlib import Path
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TR_", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8765
    scan_interval_minutes: int = 60
    budget_cap: Decimal = Decimal("400.00")
    alert_profit_threshold: Decimal = Decimal("50.00")
    profit_improvement_threshold: Decimal = Decimal("20.00")
    observation_freshness_minutes: int = 120
    stubhub_seller_fee_rate: Decimal = Decimal("0.15")
    ticketmaster_seller_fee_rate: Decimal = Decimal("0.15")
    seatgeek_seller_fee_rate: Decimal = Decimal("0.15")
    timezone: str = "America/Chicago"
    database_url: str = "sqlite:///./data/ticket_reviewer.db"
    screenshot_dir: Path = Path("data/screenshots")
    dry_run: bool = True
    ticketmaster_api_key: SecretStr | None = None
    seatgeek_client_id: SecretStr | None = None
    seatgeek_client_secret: SecretStr | None = None
    stubhub_client_id: SecretStr | None = None
    stubhub_client_secret: SecretStr | None = None
    ntfy_topic: SecretStr | None = None

class RuntimeSettings(BaseModel):
    budget_cap: Decimal
    alert_profit_threshold: Decimal
    profit_improvement_threshold: Decimal
    observation_freshness_minutes: int
    scan_interval_minutes: int
    stubhub_seller_fee_rate: Decimal
    ticketmaster_seller_fee_rate: Decimal
    seatgeek_seller_fee_rate: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "RuntimeSettings":
        return cls.model_validate(settings.model_dump(include=set(cls.model_fields)))

# ticket_reviewer/main.py
from fastapi import FastAPI
from ticket_reviewer.config import Settings

def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="Ticket Listing Reviewer")
    app.state.settings = resolved

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "dry_run": resolved.dry_run}

    return app

app = create_app()
~~~

Add **.env**, **.venv/**, **data/*.db**, **data/screenshots/**, **logs/**, and Python caches to **.gitignore**. Put every **TR_** key in **.env.example** with blank secret values and the exact safe defaults above.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/test_config.py tests/test_app.py -v**

Expected: 2 PASS.

Commit:

~~~powershell
git add pyproject.toml .gitignore .env.example ticket_reviewer tests
git commit -m "build: add local app shell and secure settings"
~~~

### Task 2: Domain observations and cent-safe pricing

**Files:**
- Create: **ticket_reviewer/domain/__init__.py**
- Create: **ticket_reviewer/domain/enums.py**
- Create: **ticket_reviewer/domain/models.py**
- Create: **ticket_reviewer/domain/pricing.py**
- Create: **tests/domain/test_pricing.py**
- Create: **tests/domain/test_models.py**
- Create: **tests/factories.py**

**Interfaces:**
- Produces: **Team**, **Source**, **ObservationKind**, **Confidence**, **OpportunityStatus** enums.
- Produces: **ExternalEvent**, **SourceObservation**, **Comparable**, **ExitScenario**, and **OpportunityEstimate** dataclasses.
- Produces: **money(value) -> Decimal**, **acquisition_total(...)**, **projected_proceeds(...)**, **net_profit(...)**, and **roi(...)**.

- [ ] **Step 1: Write failing pricing boundary tests**

~~~python
from decimal import Decimal
from ticket_reviewer.domain.pricing import acquisition_total, projected_proceeds, net_profit, roi

def test_pair_profit_uses_decimal_cents():
    cost = acquisition_total(Decimal("175.00"), Decimal("20.00"), Decimal("12.50"))
    proceeds = projected_proceeds(Decimal("300.00"), Decimal("0.15"))
    assert cost == Decimal("207.50")
    assert proceeds == Decimal("255.00")
    assert net_profit(proceeds, cost) == Decimal("47.50")
    assert roi(Decimal("47.50"), cost) == Decimal("0.2289")

def test_zero_cost_roi_is_none():
    assert roi(Decimal("10.00"), Decimal("0")) is None
~~~

- [ ] **Step 2: Run the pricing tests and verify failure**

Run: **python -m pytest tests/domain/test_pricing.py -v**

Expected: FAIL because the pricing module does not exist.

- [ ] **Step 3: Implement pricing and immutable domain types**

~~~python
# ticket_reviewer/domain/pricing.py
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)

def acquisition_total(pair_price: Decimal, buyer_fees: Decimal, estimated_tax: Decimal) -> Decimal:
    return money(pair_price + buyer_fees + estimated_tax)

def projected_proceeds(resale_gross: Decimal, seller_fee_rate: Decimal) -> Decimal:
    return money(resale_gross * (Decimal("1") - seller_fee_rate))

def net_profit(proceeds: Decimal, acquisition: Decimal) -> Decimal:
    return money(proceeds - acquisition)

def roi(profit: Decimal, acquisition: Decimal) -> Decimal | None:
    if acquisition == 0:
        return None
    return (profit / acquisition).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
~~~

Define enums with stable lowercase values and dataclasses with these required fields:

~~~python
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

@dataclass(frozen=True, slots=True)
class ExternalEvent:
    source: Source
    external_id: str
    team: Team
    opponent: str
    venue: str
    starts_at: datetime
    is_home: bool
    is_parking: bool
    url: str | None

@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: Source
    event_external_id: str
    observed_at: datetime
    kind: ObservationKind
    currency: str
    pair_price: Decimal | None
    buyer_fees: Decimal | None
    estimated_tax: Decimal | None
    section: str | None
    row: str | None
    quantity_available: int | None
    can_buy_pair: bool | None
    listing_id: str | None
    listing_url: str | None
    listing_count: int | None = None
    popularity: Decimal | None = None

@dataclass(frozen=True, slots=True)
class ExitScenario:
    marketplace: Source
    projected_resale_gross: Decimal
    seller_fee_rate: Decimal
    projected_proceeds: Decimal
    comparable_count: int

@dataclass(frozen=True, slots=True)
class OpportunityEstimate:
    acquisition_total: Decimal
    exit_source: Source | None
    projected_resale_gross: Decimal | None
    seller_fee_rate: Decimal | None
    projected_proceeds: Decimal | None
    estimated_net_profit: Decimal | None
    roi: Decimal | None
    confidence: Confidence
    risk_reasons: tuple[str, ...]
    actionable: bool
    scenarios: tuple[ExitScenario, ...]
~~~

Create **tests/factories.py** with **make_event(**overrides)**, **make_observation(**overrides)**, and **make_estimate(**overrides)**. Defaults use **datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)**, a Texans home game at NRG Stadium, a confirmed StubHub pair at $220, and Decimal values. Each builder applies overrides through **dataclasses.replace**, so later test modules do not duplicate large constructors.

- [ ] **Step 4: Add model validation tests**

Test that naive datetimes raise **ValueError**, currency is normalized to uppercase, and a negative price raises **ValueError** through dataclass **__post_init__** checks.

- [ ] **Step 5: Run domain tests and commit**

Run: **python -m pytest tests/domain/test_pricing.py tests/domain/test_models.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/domain tests/domain
git commit -m "feat: add ticket domain and pricing math"
~~~

### Task 3: SQLite schema, migrations, and repositories

**Files:**
- Create: **alembic.ini**
- Create: **ticket_reviewer/data/__init__.py**
- Create: **ticket_reviewer/data/db.py**
- Create: **ticket_reviewer/data/schema.py**
- Create: **ticket_reviewer/data/repositories.py**
- Create: **ticket_reviewer/data/migrations/env.py**
- Create: **ticket_reviewer/data/migrations/script.py.mako**
- Create: **ticket_reviewer/data/migrations/versions/0001_initial.py**
- Create: **tests/data/test_repositories.py**
- Create: **tests/data/test_migrations.py**

**Interfaces:**
- Produces: **create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]**.
- Produces: **EventRepository**, **ObservationRepository**, **OpportunityRepository**, **AlertRepository**, **OutcomeRepository**, **RunRepository**, and **SettingRepository**.
- Produces: **SettingRepository.effective(base: Settings) -> RuntimeSettings**, merging nonsecret database overrides with environment defaults for each scan.
- Repositories accept an existing SQLAlchemy **Session** so a scan can commit atomically.

- [ ] **Step 1: Write failing repository round-trip tests**

At the top of the module, create a temporary SQLite session fixture and derive **sample_event** and **sample_observation** from **tests.factories.make_event** and **make_observation**.

~~~python
def test_observation_round_trip(session, sample_event, sample_observation):
    event_id = EventRepository(session).upsert(sample_event)
    observation_id = ObservationRepository(session).add(event_id, sample_observation)
    loaded = ObservationRepository(session).get(observation_id)
    assert loaded.event_id == event_id
    assert loaded.pair_price == Decimal("220.00")

def test_duplicate_source_snapshot_is_idempotent(session, sample_event, sample_observation):
    event_id = EventRepository(session).upsert(sample_event)
    first = ObservationRepository(session).add(event_id, sample_observation)
    second = ObservationRepository(session).add(event_id, sample_observation)
    assert second == first
~~~

Use a uniqueness key of **source + event_external_id + listing_id + observed_at**; use a stable sentinel for a missing listing ID.

- [ ] **Step 2: Run repository tests and verify failure**

Run: **python -m pytest tests/data/test_repositories.py -v**

Expected: FAIL because the data package does not exist.

- [ ] **Step 3: Create the explicit initial migration**

Create tables:

- **events**: canonical team, opponent, venue, UTC kickoff, home flag, created/updated timestamps.
- **source_events**: event FK, source, external ID, URL, raw name, last seen; unique source/external ID.
- **observations**: event FK plus every **SourceObservation** field and freshness timestamp.
- **opportunities**: candidate observation FK, selected exit source, projected values, fee rate, confidence, risk JSON, status, and serialized exit scenarios.
- **alerts**: opportunity FK, fingerprint, sent time, profit at send, provider message ID.
- **outcomes**: opportunity FK, status, actual acquisition, actual proceeds, actual fees, notes.
- **connector_runs**: source, start/end, success, observation count, redacted error.
- **settings**: nonsecret key/value only.
- **manual_reviews**: screenshot path, OCR text, corrected payload JSON, confirmation time.

Every money column is **Numeric(12, 2)**; every datetime column stores timezone-aware UTC values.

- [ ] **Step 4: Implement transaction-scoped repositories**

Use explicit return types and never commit inside repository methods:

~~~python
class OpportunityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_estimate(self, event_id: int, observation_id: int, estimate: OpportunityEstimate) -> int:
        row = OpportunityRow(
            event_id=event_id,
            observation_id=observation_id,
            acquisition_total=estimate.acquisition_total,
            exit_source=estimate.exit_source.value if estimate.exit_source else None,
            projected_resale_gross=estimate.projected_resale_gross,
            projected_proceeds=estimate.projected_proceeds,
            estimated_net_profit=estimate.estimated_net_profit,
            roi=estimate.roi,
            confidence=estimate.confidence.value,
            risk_reasons=list(estimate.risk_reasons),
            scenarios=[
                {
                    "marketplace": item.marketplace.value,
                    "projected_resale_gross": str(item.projected_resale_gross),
                    "seller_fee_rate": str(item.seller_fee_rate),
                    "projected_proceeds": str(item.projected_proceeds),
                    "comparable_count": item.comparable_count,
                }
                for item in estimate.scenarios
            ],
            actionable=estimate.actionable,
        )
        self.session.add(row)
        self.session.flush()
        return row.id
~~~

- [ ] **Step 5: Verify migrations from an empty database**

Run: **python -m alembic upgrade head**

Run: **python -m pytest tests/data/test_migrations.py tests/data/test_repositories.py -v**

Expected: migration creates all nine tables and repository tests PASS.

- [ ] **Step 6: Commit**

~~~powershell
git add alembic.ini ticket_reviewer/data tests/data
git commit -m "feat: add persistent ticket review history"
~~~

### Task 4: Event matching and hard eligibility

**Files:**
- Create: **ticket_reviewer/domain/matching.py**
- Create: **ticket_reviewer/domain/eligibility.py**
- Create: **tests/domain/test_matching.py**
- Create: **tests/domain/test_eligibility.py**

**Interfaces:**
- Consumes: **ExternalEvent**, **SourceObservation**, **Team**.
- Produces: **normalize_label(value: str) -> str**.
- Produces: **is_supported_home_game(event: ExternalEvent) -> bool**.
- Produces: **event_match_score(left: ExternalEvent, right: ExternalEvent) -> Decimal**.
- Produces: **is_actionable_pair(observation, acquisition, budget_cap) -> tuple[bool, tuple[str, ...]]**.

- [ ] **Step 1: Write failing matching tests**

~~~python
def test_texans_home_game_matches_nrg_alias(texans_event):
    assert is_supported_home_game(replace(texans_event, venue="NRG Stadium"))

def test_aggies_away_game_is_rejected(aggies_event):
    assert not is_supported_home_game(replace(aggies_event, is_home=False))

def test_parking_product_is_rejected(texans_event):
    assert not is_supported_home_game(replace(texans_event, is_parking=True))

def test_same_game_across_sources_scores_above_threshold(tm_event, stubhub_event):
    assert event_match_score(tm_event, stubhub_event) >= Decimal("0.85")
~~~

- [ ] **Step 2: Write failing pair eligibility boundary tests**

~~~python
def test_pair_at_budget_is_actionable(listing_observation):
    ok, reasons = is_actionable_pair(listing_observation, Decimal("400.00"), Decimal("400.00"))
    assert ok is True
    assert reasons == ()

def test_event_floor_is_not_a_confirmed_pair(event_floor_observation):
    ok, reasons = is_actionable_pair(event_floor_observation, Decimal("200.00"), Decimal("400.00"))
    assert ok is False
    assert "pair availability is not confirmed" in reasons
~~~

- [ ] **Step 3: Implement conservative matching**

Normalize Unicode, lowercase, remove punctuation, collapse whitespace, and map only explicit venue aliases:

~~~python
HOME_VENUES = {
    Team.TEXANS: {"nrg stadium", "reliant stadium"},
    Team.AGGIES: {"kyle field"},
}

def is_supported_home_game(event: ExternalEvent) -> bool:
    return (
        event.is_home
        and not event.is_parking
        and normalize_label(event.venue) in HOME_VENUES[event.team]
    )
~~~

Calculate match score from team equality, opponent token similarity, kickoff difference no greater than 12 hours, and venue alias equality. Return zero on conflicting teams or kickoff dates.

- [ ] **Step 4: Implement hard eligibility**

Require **ObservationKind.LISTING**, **can_buy_pair is True**, **quantity_available >= 2**, non-null pair price, USD, and acquisition at or below the configured cap. Return human-readable rejection reasons in deterministic order.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/domain/test_matching.py tests/domain/test_eligibility.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/domain tests/domain
git commit -m "feat: enforce supported games and pair budget"
~~~

### Task 5: Comparable selection, resale estimate, confidence, and risk

**Files:**
- Create: **ticket_reviewer/domain/comparables.py**
- Create: **ticket_reviewer/domain/scoring.py**
- Create: **tests/domain/test_comparables.py**
- Create: **tests/domain/test_scoring.py**

**Interfaces:**
- Consumes: candidate **SourceObservation**, comparison observations, seller-fee profiles by exit marketplace, current time, and budget/threshold settings.
- Produces: **select_comparables(candidate, observations) -> tuple[Comparable, ...]**.
- Produces: **weighted_quantile(comparables, quantile=Decimal("0.40")) -> Decimal**.
- Produces: **estimate_exit_scenarios(candidate, observations, fee_profiles) -> tuple[ExitScenario, ...]**.
- Produces: **estimate_opportunity(candidate, observations, fee_profiles, now, budget_cap) -> OpportunityEstimate**.

- [ ] **Step 1: Write failing conservative-estimate tests**

~~~python
def test_uses_lower_weighted_market_value(candidate, comparable_observations, now):
    estimate = estimate_opportunity(
        candidate,
        comparable_observations,
        fee_profiles={
            Source.STUBHUB: Decimal("0.15"),
            Source.TICKETMASTER: Decimal("0.18"),
            Source.SEATGEEK: Decimal("0.15"),
        },
        now=now,
        budget_cap=Decimal("400.00"),
    )
    assert estimate.exit_source is Source.STUBHUB
    assert estimate.projected_resale_gross == Decimal("330.00")
    assert estimate.projected_proceeds == Decimal("280.50")
    assert estimate.estimated_net_profit == Decimal("60.50")

def test_sparse_event_level_data_is_low_confidence(candidate, event_floor_only, now):
    estimate = estimate_opportunity(
        candidate,
        event_floor_only,
        {Source.STUBHUB: Decimal("0.15")},
        now,
        Decimal("400.00"),
    )
    assert estimate.confidence is Confidence.LOW
    assert "fewer than 3 seat-level comparables" in estimate.risk_reasons
~~~

- [ ] **Step 2: Run scoring tests and verify failure**

Run: **python -m pytest tests/domain/test_comparables.py tests/domain/test_scoring.py -v**

Expected: FAIL because comparable and scoring modules do not exist.

- [ ] **Step 3: Implement comparable weights**

Use exact weights:

~~~python
def comparable_weight(candidate: SourceObservation, other: SourceObservation) -> Decimal:
    if candidate.section and other.section and normalize_label(candidate.section) == normalize_label(other.section):
        if candidate.row and other.row and normalize_label(candidate.row) == normalize_label(other.row):
            return Decimal("1.00")
        return Decimal("0.80")
    if other.kind is ObservationKind.EVENT_AGGREGATE:
        return Decimal("0.35")
    if other.kind is ObservationKind.EVENT_FLOOR:
        return Decimal("0.20")
    return Decimal("0.50")
~~~

Discard observations older than 24 hours, from another event, without a usable price, or representing parking. Compute the 40th weighted percentile so isolated high asks cannot raise projected value. Build one **ExitScenario** per marketplace that has same-event comparable data, apply that marketplace's configured seller-fee rate, and select the scenario with the highest conservative projected proceeds. A marketplace with no same-event observations cannot be selected as the exit source.

- [ ] **Step 4: Implement confidence and risk rules**

Start at high confidence, drop one level for each applicable group:

- Fewer than three seat-level comparables.
- No same-section comparable.
- Any required fee or tax is estimated.
- Newest comparable older than two hours.
- Candidate comes from manually corrected OCR.

Apply a 10% haircut when fewer than three seat-level comparables exist and a 5% haircut when the most recent three comparable medians decline by more than 10%. Never apply an upward trend multiplier. Include each applied rule in **risk_reasons**.

Add these explicit demand safeguards without upward price multipliers:

- Derive opponent tier from the median available source popularity: high at or above 0.80, low below 0.35, otherwise standard; missing popularity is standard with an “opponent demand unverified” risk.
- If kickoff is within seven days and the three-snapshot trend is nonpositive, apply a 5% haircut.
- If kickoff is within three days and event listing count exceeds 500, apply an additional 5% haircut.
- Unknown row or no same-section comparable adds a seat-quality uncertainty reason and lowers confidence.
- Event-level floors and aggregates may support a scenario, but they can never make the candidate itself actionable.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/domain/test_comparables.py tests/domain/test_scoring.py -v**

Expected: all PASS, including exact Decimal values.

Commit:

~~~powershell
git add ticket_reviewer/domain tests/domain
git commit -m "feat: add conservative opportunity scoring"
~~~

### Task 6: Connector contract and isolated scan coordinator

**Files:**
- Create: **ticket_reviewer/connectors/__init__.py**
- Create: **ticket_reviewer/connectors/base.py**
- Create: **ticket_reviewer/services/__init__.py**
- Create: **ticket_reviewer/services/retry.py**
- Create: **ticket_reviewer/services/scanner.py**
- Create: **ticket_reviewer/bootstrap.py**
- Create: **tests/connectors/test_contract.py**
- Create: **tests/services/test_retry.py**
- Create: **tests/services/test_scanner.py**

**Interfaces:**
- Produces: **Capability(EVENT_SEARCH, EVENT_PRICE, LISTING_DETAIL)**.
- Produces: **MarketplaceConnector** protocol with **discover(team, starts_after, starts_before)** and **fetch_observations(event)**.
- Produces: **ConnectorFailure(source, category, safe_message, retryable)**.
- Produces: **call_with_retry(operation, sleep, attempts=3) -> T**.
- Produces: **ScanCoordinator.run(now: datetime) -> ScanSummary**.
- Produces: **build_services(settings, connectors=()) -> ApplicationServices** for dependency wiring without module-level globals.

- [ ] **Step 1: Write failing connector-isolation tests**

~~~python
def test_one_connector_failure_does_not_abort_scan(coordinator, good_connector, failing_connector, now):
    summary = coordinator.run(now)
    assert summary.sources_succeeded == (good_connector.source,)
    assert summary.sources_failed == (failing_connector.source,)
    assert summary.observations_saved == 1

def test_event_level_observation_is_saved_but_not_actionable(coordinator_with_floor_only, now):
    summary = coordinator_with_floor_only.run(now)
    assert summary.observations_saved == 1
    assert summary.actionable_opportunities == 0

def test_retry_stops_after_three_retryable_failures():
    operation = Mock(side_effect=retryable_network_failure())
    sleep = Mock()
    with pytest.raises(ConnectorFailure):
        call_with_retry(operation, sleep=sleep, attempts=3)
    assert operation.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.5, 1.0]
~~~

- [ ] **Step 2: Run scanner tests and verify failure**

Run: **python -m pytest tests/connectors/test_contract.py tests/services/test_retry.py tests/services/test_scanner.py -v**

Expected: FAIL because connector and scanner modules do not exist.

- [ ] **Step 3: Define the connector protocol**

~~~python
class MarketplaceConnector(Protocol):
    source: Source
    capabilities: frozenset[Capability]

    def discover(
        self, team: Team, starts_after: datetime, starts_before: datetime
    ) -> list[ExternalEvent]: ...

    def fetch_observations(self, event: ExternalEvent) -> list[SourceObservation]: ...
~~~

Define failure categories **AUTH**, **RATE_LIMIT**, **NETWORK**, **PARSE**, and **UNSUPPORTED**. Safe messages must omit response bodies and credentials.

Implement **call_with_retry** with three total attempts and injectable sleeps of 0.5 and 1.0 seconds. Retry only failures whose **retryable** flag is true; authentication, parsing, and unsupported-capability failures return immediately. Connectors wrap only their idempotent GET and token requests with this helper.

- [ ] **Step 4: Implement the coordinator**

At the beginning of every scan, load **RuntimeSettings** through **SettingRepository.effective** so dashboard changes apply without restart. For each connector and supported team: discover, reject unsupported games, upsert/match the event, fetch observations, persist snapshots, estimate only confirmed listing pairs, and record a connector run. Catch **ConnectorFailure** per connector. Commit each connector's successful transaction separately so one source cannot roll back another. Preserve the last successful observation after failures, but mark it stale once its age exceeds **RuntimeSettings.observation_freshness_minutes**; stale observations remain visible and cannot enter alert evaluation.

Create **ApplicationServices** as a dataclass containing session factory, repositories factory, connectors, scanner, and a nullable alert service. **build_services** takes injected connectors in tests; production registration is added as each real connector task lands.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/connectors/test_contract.py tests/services/test_retry.py tests/services/test_scanner.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/connectors ticket_reviewer/services tests/connectors tests/services
git commit -m "feat: add resilient marketplace scan pipeline"
~~~

### Task 7: Ticketmaster public Discovery connector

**Files:**
- Create: **ticket_reviewer/connectors/ticketmaster.py**
- Create: **tests/fixtures/ticketmaster/texans_events.json**
- Create: **tests/connectors/test_ticketmaster.py**
- Modify: **ticket_reviewer/config.py**
- Modify: **ticket_reviewer/bootstrap.py**
- Modify: **.env.example**

**Interfaces:**
- Consumes: **Settings.ticketmaster_api_key** and HTTPX client.
- Produces: **TicketmasterConnector** with **EVENT_SEARCH** and **EVENT_PRICE** only.
- Public endpoints: **GET https://app.ticketmaster.com/discovery/v2/events.json** and **GET https://app.ticketmaster.com/discovery/v2/events/{id}.json**.

- [ ] **Step 1: Save a sanitized fixture and write failing adapter tests**

~~~python
@respx.mock
def test_discovers_only_texans_home_games(connector, fixture_json):
    respx.get("https://app.ticketmaster.com/discovery/v2/events.json").mock(
        return_value=httpx.Response(200, json=fixture_json)
    )
    events = connector.discover(Team.TEXANS, window_start, window_end)
    assert [(event.opponent, event.venue) for event in events] == [("Colts", "NRG Stadium")]

def test_public_price_range_is_not_claimed_as_pair_inventory(connector, tm_event):
    observations = connector.fetch_observations(tm_event)
    assert observations[0].kind is ObservationKind.EVENT_FLOOR
    assert observations[0].can_buy_pair is None
~~~

- [ ] **Step 2: Run the connector tests and verify failure**

Run: **python -m pytest tests/connectors/test_ticketmaster.py -v**

Expected: FAIL because **TicketmasterConnector** does not exist.

- [ ] **Step 3: Implement Discovery requests and normalization**

Send keyword, countryCode=US, start/end UTC, size=100, and API key. Parse **_embedded.events**, local dates/times, venue, and URL. Filter to the Texans attraction/name and NRG Stadium; reject away and parking products. **fetch_observations** retrieves the documented event-details URL by external ID and parses its **priceRanges**.

Map a minimum price to an **EVENT_FLOOR** observation with **quantity_available=None** and **can_buy_pair=None**. Do not call Inventory Status, Top Picks, Partner, reserve, or commerce endpoints.

- [ ] **Step 4: Test auth, rate limit, and missing-price responses**

401/403 become non-retryable **AUTH**, 429 becomes retryable **RATE_LIMIT**, 5xx/network become retryable **NETWORK**, and a valid event without **priceRanges** yields no observation rather than zero dollars.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/connectors/test_ticketmaster.py -v**

Expected: all PASS.

Register this connector in **build_services** only when the key is configured. Commit:

~~~powershell
git add ticket_reviewer/connectors/ticketmaster.py ticket_reviewer/config.py .env.example tests
git commit -m "feat: add Ticketmaster event discovery"
~~~

### Task 8: SeatGeek Aggies aggregate connector

**Files:**
- Create: **ticket_reviewer/connectors/seatgeek.py**
- Create: **tests/fixtures/seatgeek/aggies_events.json**
- Create: **tests/connectors/test_seatgeek.py**
- Modify: **ticket_reviewer/config.py**
- Modify: **ticket_reviewer/bootstrap.py**
- Modify: **.env.example**

**Interfaces:**
- Consumes: SeatGeek client ID and optional secret.
- Produces: **SeatGeekConnector** with **EVENT_SEARCH** and **EVENT_PRICE**.
- Public endpoints: **GET https://api.seatgeek.com/2/events** and **GET https://api.seatgeek.com/2/events/{id}**.

- [ ] **Step 1: Write failing aggregate tests**

~~~python
def test_maps_aggies_event_aggregates(connector, mocked_aggies_response):
    event = connector.discover(Team.AGGIES, window_start, window_end)[0]
    observations = connector.fetch_observations(event)
    floor = next(item for item in observations if item.kind is ObservationKind.EVENT_FLOOR)
    aggregate = next(item for item in observations if item.kind is ObservationKind.EVENT_AGGREGATE)
    assert floor.pair_price == Decimal("240.00")
    assert aggregate.listing_count == 850
    assert floor.can_buy_pair is None
~~~

- [ ] **Step 2: Run tests and verify failure**

Run: **python -m pytest tests/connectors/test_seatgeek.py -v**

Expected: FAIL because the connector does not exist.

- [ ] **Step 3: Implement event and aggregate mapping**

Query performer/name, datetime window, venue state TX, and **per_page=100**. Accept only Texas A&M football at Kyle Field. **fetch_observations** retrieves the event-details URL by external ID. Convert SeatGeek per-ticket **lowest_price**, **average_price**, and **highest_price** values to pair values by multiplying by two, but keep **can_buy_pair=None** because event aggregates do not confirm pair availability.

Use **listing_count** and **score** only as demand/confidence signals; neither changes price by itself.

- [ ] **Step 4: Test partial and invalid payloads**

Missing lowest price yields an aggregate without a price; negative prices and naive datetimes produce **PARSE** failures; non-Kyle Field events are excluded.

- [ ] **Step 5: Run tests and commit**

Run: **python -m pytest tests/connectors/test_seatgeek.py -v**

Expected: all PASS.

Register this connector in **build_services** only when the client ID is configured. Commit:

~~~powershell
git add ticket_reviewer/connectors/seatgeek.py ticket_reviewer/config.py .env.example tests
git commit -m "feat: add SeatGeek Aggies market signals"
~~~

### Task 9: StubHub OAuth and catalog-floor connector

**Files:**
- Create: **ticket_reviewer/connectors/stubhub.py**
- Create: **tests/fixtures/stubhub/token.json**
- Create: **tests/fixtures/stubhub/texans_search.json**
- Create: **tests/connectors/test_stubhub.py**
- Modify: **ticket_reviewer/config.py**
- Modify: **ticket_reviewer/bootstrap.py**
- Modify: **.env.example**

**Interfaces:**
- Consumes: StubHub client ID/secret.
- Produces: **StubHubTokenProvider.get_token(now) -> str** with in-memory expiry caching.
- Produces: **StubHubConnector** with **EVENT_SEARCH** and **EVENT_PRICE** by default.
- Endpoints: **POST https://account.stubhub.com/oauth2/token**, **GET https://api.stubhub.net/catalog/events/search**, and **GET https://api.stubhub.net/catalog/events/{eventId}**.

- [ ] **Step 1: Write failing OAuth-cache and catalog tests**

~~~python
def test_reuses_token_until_safety_window(token_provider, token_route, now):
    first = token_provider.get_token(now)
    second = token_provider.get_token(now + timedelta(minutes=5))
    assert first == second == "access-token"
    assert token_route.call_count == 1

def test_catalog_minimum_is_event_floor_not_listing(connector, stubhub_event):
    observation = connector.fetch_observations(stubhub_event)[0]
    assert observation.kind is ObservationKind.EVENT_FLOOR
    assert observation.can_buy_pair is None
    assert observation.listing_id is None
~~~

- [ ] **Step 2: Run tests and verify failure**

Run: **python -m pytest tests/connectors/test_stubhub.py -v**

Expected: FAIL because StubHub classes do not exist.

- [ ] **Step 3: Implement application-only OAuth**

Post the documented client-credentials request, cache **access_token** until 60 seconds before **expires_in**, and use the Bearer token only in request headers. Redact token endpoint response content from all failures.

- [ ] **Step 4: Implement catalog search without inventing listing access**

Call **/catalog/events/search** with q, local date when known, page_size=100, country_code=US, and exclude_parking_passes=true. Parse the event URL. **fetch_observations** calls the event-details URL and converts **min_ticket_price** from a per-ticket amount to an event-floor pair signal by multiplying by two; keep listing identity, section, row, quantity, and pair availability null.

Do not call seller-listing endpoints and do not add **LISTING_DETAIL** capability. Add a health note: “Detailed StubHub buyer inventory is unavailable to this key; event floors cannot trigger actionable alerts.”

- [ ] **Step 5: Test 401 refresh, 403 downgrade, and missing minimum**

One 401 triggers one token refresh and one request retry. A 403 records an **AUTH** failure without retry loops. A valid event without **min_ticket_price** is saved as an event but produces no price observation.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/connectors/test_stubhub.py -v**

Expected: all PASS.

Register this connector in **build_services** only when both OAuth values are configured. Commit:

~~~powershell
git add ticket_reviewer/connectors/stubhub.py ticket_reviewer/config.py .env.example tests
git commit -m "feat: add StubHub catalog price signals"
~~~

### Task 10: Alert policy and free iPhone ntfy notifications

**Files:**
- Create: **ticket_reviewer/services/alerts.py**
- Create: **tests/services/test_alerts.py**
- Create: **tests/fixtures/ntfy/success.json**
- Modify: **ticket_reviewer/config.py**
- Modify: **ticket_reviewer/services/scanner.py**
- Modify: **ticket_reviewer/bootstrap.py**
- Modify: **.env.example**

**Interfaces:**
- Consumes: persisted opportunity, latest successful observation timestamp, 120-minute freshness setting, and alert history.
- Produces: **AlertDecision(should_send, reason, fingerprint)**.
- Produces: **NtfyPublisher.publish(message: PushMessage) -> str**.
- Produces: **AlertService.evaluate_and_send(opportunity_id: int, now: datetime) -> AlertDecision**.

- [ ] **Step 1: Write failing policy tests**

~~~python
def test_new_qualifying_opportunity_sends(policy, opportunity):
    decision = policy.decide(opportunity, previous_alert=None, stale=False)
    assert decision.should_send is True

def test_49_99_does_not_send(policy, opportunity):
    decision = policy.decide(replace(opportunity, estimated_net_profit=Decimal("49.99")), None, False)
    assert decision.should_send is False

def test_repeat_requires_20_dollar_improvement(policy, opportunity, previous_alert):
    unchanged = replace(opportunity, estimated_net_profit=previous_alert.profit_at_send + Decimal("19.99"))
    improved = replace(opportunity, estimated_net_profit=previous_alert.profit_at_send + Decimal("20.00"))
    assert policy.decide(unchanged, previous_alert, False).should_send is False
    assert policy.decide(improved, previous_alert, False).should_send is True

def test_stale_opportunity_never_sends(policy, opportunity):
    assert policy.decide(opportunity, previous_alert=None, stale=True).should_send is False
~~~

- [ ] **Step 2: Run tests and verify failure**

Run: **python -m pytest tests/services/test_alerts.py -v**

Expected: FAIL because alert services do not exist.

- [ ] **Step 3: Implement deterministic fingerprints and message copy**

Fingerprint SHA-256 over source, event ID, listing ID, pair cost, projected profit, and observed timestamp. Message format:

~~~text
Texans vs Colts — Pair opportunity
Section 123, Row G
Buy: $240 all-in | Est. net: $63 | ROI: 26.3%
Confidence: medium
~~~

Omit unknown seat lines and any secret/account value. Use the public marketplace URL as the ntfy **Click** header when present.

- [ ] **Step 4: Implement ntfy publishing and dry-run behavior**

POST plain text to **https://ntfy.sh/{topic}** with **Title**, **Priority**, **Tags**, and optional **Click** headers. In dry-run mode, persist the decision with provider ID **dry-run** but do not make the HTTP request.

Wire **AlertService** into **ApplicationServices** and call it only after an actionable opportunity is committed with a fresh candidate observation. Configure HTTPX/request logging so the ntfy topic path is never written to application logs.

- [ ] **Step 5: Test network failures and deduplication**

A timeout records a retryable notification failure without marking the alert sent. A successful publish persists provider ID. The same fingerprint cannot be inserted twice.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/services/test_alerts.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/services/alerts.py ticket_reviewer/config.py .env.example tests
git commit -m "feat: send deduplicated ntfy opportunity alerts"
~~~

### Task 11: Immediate/hourly scheduler and Windows startup

**Files:**
- Create: **ticket_reviewer/services/scheduler.py**
- Create: **scripts/run.ps1**
- Create: **scripts/install_startup_task.ps1**
- Create: **scripts/uninstall_startup_task.ps1**
- Create: **tests/services/test_scheduler.py**
- Create: **tests/scripts/test_startup_scripts.py**
- Modify: **ticket_reviewer/main.py**

**Interfaces:**
- Consumes: **ScanCoordinator.run(now)** and **Settings.scan_interval_minutes**.
- Produces: **build_scheduler(scan_callable, settings) -> BackgroundScheduler**.
- Produces: idempotent Windows task named **TicketListingReviewer**.

- [ ] **Step 1: Write failing scheduler tests**

~~~python
def test_scheduler_has_immediate_and_hourly_scan(settings, frozen_now):
    scheduler = build_scheduler(lambda: None, settings, now=frozen_now)
    job = scheduler.get_job("hourly-market-scan")
    assert job.next_run_time == frozen_now
    assert job.trigger.interval == timedelta(minutes=60)

def test_scheduler_uses_single_job_id(settings):
    scheduler = build_scheduler(lambda: None, settings)
    assert [job.id for job in scheduler.get_jobs()] == ["hourly-market-scan"]
~~~

- [ ] **Step 2: Run tests and verify failure**

Run: **python -m pytest tests/services/test_scheduler.py -v**

Expected: FAIL because scheduler module does not exist.

- [ ] **Step 3: Implement scheduler lifespan**

Use **max_instances=1**, **coalesce=True**, **misfire_grace_time=300**, fixed 60-minute interval, and immediate **next_run_time**. Start in FastAPI lifespan after migrations succeed; shut down without waiting for a running scan during application exit.

- [ ] **Step 4: Write exact startup scripts**

**scripts/run.ps1** resolves its own repository path, uses **.venv\Scripts\python.exe**, creates **logs/**, and launches:

~~~powershell
& $pythonExe -m uvicorn ticket_reviewer.main:app --host 127.0.0.1 --port 8765
~~~

**install_startup_task.ps1** registers one current-user logon task named **TicketListingReviewer**, action **powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<absolute run.ps1>"**, working directory set to the repo, and **StartWhenAvailable=true**. If the named task exists, update it rather than duplicate it.

**uninstall_startup_task.ps1** resolves the exact task name, shows it, and unregisters only that task.

- [ ] **Step 5: Test script safety**

Parse the scripts in tests and assert the exact task name, localhost binding, hidden window, no administrator principal, no wildcard unregister, and no unrelated task deletion.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/services/test_scheduler.py tests/scripts/test_startup_scripts.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/services/scheduler.py ticket_reviewer/main.py scripts tests
git commit -m "feat: schedule hourly scans at Windows login"
~~~

### Task 12: Dashboard opportunities, event detail, and health

**Files:**
- Create: **ticket_reviewer/web/__init__.py**
- Create: **ticket_reviewer/web/routes.py**
- Create: **ticket_reviewer/web/viewmodels.py**
- Create: **ticket_reviewer/web/templates/base.html**
- Create: **ticket_reviewer/web/templates/opportunities.html**
- Create: **ticket_reviewer/web/templates/event_detail.html**
- Create: **ticket_reviewer/web/templates/health.html**
- Create: **ticket_reviewer/web/static/app.css**
- Create: **tests/web/test_opportunities.py**
- Create: **tests/web/test_event_detail.py**
- Create: **tests/web/test_health.py**
- Modify: **ticket_reviewer/main.py**

**Interfaces:**
- Consumes: repositories and application settings from FastAPI dependencies.
- Produces routes: **GET /**, **GET /events/{event_id}**, and **GET /health**.
- Produces: **OpportunityCard.from_row(row, timezone) -> OpportunityCard**.

- [ ] **Step 1: Write failing route tests**

~~~python
def test_opportunities_sorted_by_profit(client, seeded_opportunities):
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.index("$82.00") < response.text.index("$51.00")

def test_low_confidence_is_visibly_labeled(client, low_confidence_opportunity):
    response = client.get("/")
    assert "Low confidence" in response.text
    assert "fewer than 3 seat-level comparables" in response.text

def test_health_shows_connector_failure_without_secret(client, failed_run):
    response = client.get("/health")
    assert "StubHub" in response.text
    assert "authentication failed" in response.text
    assert "client_secret" not in response.text
~~~

- [ ] **Step 2: Run web tests and verify failure**

Run: **python -m pytest tests/web/test_opportunities.py tests/web/test_event_detail.py tests/web/test_health.py -v**

Expected: FAIL because web routes/templates do not exist.

- [ ] **Step 3: Implement opportunity cards and filters**

Render event, kickoff in America/Chicago, section/row, source, all-in pair cost, projected gross, assumed seller fee, projected proceeds, net profit, ROI, confidence, risk reasons, first/last seen, freshness, and source link. Query parameters: **team**, **event_id**, **source**, **confidence**, **min_profit**, **max_cost**, and **status**.

Use net profit descending as default sort; never sort by a combined score ahead of profit.

- [ ] **Step 4: Implement event history**

Render a JSON-safe Chart.js dataset from observations grouped by source and conservative seating key. Event floors use dashed lines and labels stating “event-level signal; pair not confirmed.” List the exact comparable observation IDs used by each estimate.

- [ ] **Step 5: Implement responsive local styling**

Use semantic HTML, system fonts, keyboard-visible focus, a compact card grid, and color plus text/icons for confidence. Do not rely on color alone. Keep JavaScript limited to HTMX and Chart.js.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/web -v**

Expected: dashboard tests PASS.

Commit:

~~~powershell
git add ticket_reviewer/web ticket_reviewer/main.py tests/web
git commit -m "feat: add explainable opportunity dashboard"
~~~

### Task 13: Local screenshot OCR and confirmed manual review

**Files:**
- Create: **ticket_reviewer/services/ocr.py**
- Create: **ticket_reviewer/web/templates/manual_review.html**
- Create: **ticket_reviewer/web/templates/manual_confirm.html**
- Create: **tests/services/test_ocr_parser.py**
- Create: **tests/web/test_manual_review.py**
- Modify: **ticket_reviewer/web/routes.py**
- Modify: **ticket_reviewer/config.py**

**Interfaces:**
- Produces: **OcrEngine.extract_text(image_path: Path) -> str**.
- Produces: **TesseractOcrEngine**.
- Produces: **parse_listing_text(text: str) -> ManualReviewDraft**.
- Produces routes: **GET /manual**, **POST /manual/extract**, **POST /manual/confirm**.

- [ ] **Step 1: Write failing parser tests with marketplace-like text**

~~~python
def test_parses_pair_listing_text():
    draft = parse_listing_text(
        "Houston Texans vs Colts\nSection 123 Row G\n2 tickets\n$110 each\nFees $24\nTotal $244"
    )
    assert draft.section == "123"
    assert draft.row == "G"
    assert draft.quantity == 2
    assert draft.total == Decimal("244.00")

def test_parser_does_not_guess_missing_total():
    draft = parse_listing_text("Aggies vs Texas\nSection 401\n$175")
    assert draft.total is None
    assert "total cost" in draft.missing_fields
~~~

- [ ] **Step 2: Run parser tests and verify failure**

Run: **python -m pytest tests/services/test_ocr_parser.py -v**

Expected: FAIL because OCR service does not exist.

- [ ] **Step 3: Implement local extraction and conservative parsing**

Use Pillow to normalize orientation and contrast, then **pytesseract.image_to_string**. Parse only explicit labels for section, row, quantity, per-ticket price, fees, tax, and total. Never derive a total when quantity or fee treatment is ambiguous. Return candidates plus **missing_fields** and **warnings**.

- [ ] **Step 4: Implement upload and confirmation routes**

Accept PNG/JPEG only, maximum 10 MiB, verify image bytes with Pillow, generate a random filename under the configured screenshot directory, and never use the uploaded filename. Accept an optional reference URL only when its scheme is http or https; save it without fetching it. Show every parsed field and the reference URL in an editable confirmation form.

**POST /manual/confirm** must reject quantity other than two, missing event, missing total, total above $400, and naive kickoff times. Only the confirmed form creates a **SourceObservation(kind=LISTING, can_buy_pair=True)** and runs the scoring engine.

- [ ] **Step 5: Test confirmation and deletion behavior**

Test invalid MIME, oversized file, OCR correction, $400 boundary, $400.01 rejection, no scoring before confirmation, and screenshot deletion through **POST /manual/{review_id}/delete**.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/services/test_ocr_parser.py tests/web/test_manual_review.py -v**

Expected: all PASS. Mark a live Tesseract executable smoke test with **pytest.mark.integration** and skip it with a clear reason when the executable is absent.

Commit:

~~~powershell
git add ticket_reviewer/services/ocr.py ticket_reviewer/web ticket_reviewer/config.py tests
git commit -m "feat: add confirmed screenshot listing reviews"
~~~

### Task 14: Outcomes, nonsecret settings, and notification test

**Files:**
- Create: **ticket_reviewer/web/templates/settings.html**
- Create: **ticket_reviewer/web/templates/outcome_form.html**
- Create: **tests/web/test_settings.py**
- Create: **tests/web/test_outcomes.py**
- Modify: **ticket_reviewer/web/routes.py**
- Modify: **ticket_reviewer/data/repositories.py**

**Interfaces:**
- Produces routes: **GET/POST /settings**, **POST /notifications/test**, **POST /opportunities/{id}/status**.
- Persists only nonsecret settings in SQLite.
- Outcome states: **passed**, **watching**, **purchased**, **sold**, **expired**.

- [ ] **Step 1: Write failing settings and outcome tests**

~~~python
def test_updates_budget_and_threshold(client):
    response = client.post("/settings", data={
        "budget_cap": "400.00",
        "alert_profit_threshold": "50.00",
        "profit_improvement_threshold": "20.00",
        "scan_interval_minutes": "60",
        "stubhub_seller_fee_rate": "0.15",
        "ticketmaster_seller_fee_rate": "0.15",
        "seatgeek_seller_fee_rate": "0.15",
    })
    assert response.status_code == 303

def test_sold_outcome_requires_actual_proceeds(client, opportunity):
    response = client.post(f"/opportunities/{opportunity.id}/status", data={"status": "sold"})
    assert response.status_code == 422
~~~

- [ ] **Step 2: Run tests and verify failure**

Run: **python -m pytest tests/web/test_settings.py tests/web/test_outcomes.py -v**

Expected: FAIL because routes/templates are missing.

- [ ] **Step 3: Implement validated nonsecret settings**

Allow budget 1–400, profit threshold at least zero, improvement threshold at least zero, seller-fee assumptions from 0 through 0.50 for each exit marketplace, observation freshness from 60 through 1,440 minutes, and interval fixed to 60 in version one. Persist these values through **SettingRepository** and verify **effective(base_settings)** returns the overrides on the next scan. Display whether each secret is configured as **Configured** or **Missing**; never render its value and never accept secret values through these HTML forms. Label all fee values as user assumptions rather than marketplace guarantees.

- [ ] **Step 4: Implement outcomes**

Require actual acquisition cost for **purchased** and actual proceeds plus actual fees for **sold**. Passed, sold, purchased, and expired opportunities are suppressed from future alerts. Watching remains eligible for a $20 improvement alert.

- [ ] **Step 5: Implement ntfy test notification**

Send a message titled “Ticket Reviewer test” through the same publisher. Return a visible dry-run label when **TR_DRY_RUN=true**. Never echo the topic.

- [ ] **Step 6: Run tests and commit**

Run: **python -m pytest tests/web/test_settings.py tests/web/test_outcomes.py -v**

Expected: all PASS.

Commit:

~~~powershell
git add ticket_reviewer/web ticket_reviewer/data/repositories.py tests/web
git commit -m "feat: add reviewer settings and outcome tracking"
~~~

### Task 15: End-to-end dry run, security regression, and operator guide

**Files:**
- Create: **tests/e2e/test_dry_run.py**
- Create: **tests/test_security_regressions.py**
- Create: **tests/fixtures/e2e/combined_scan.json**
- Create: **README.md**
- Modify: **ticket_reviewer/main.py**
- Modify: **.env.example**

**Interfaces:**
- Consumes all prior components.
- Produces a repeatable first-run path from empty database to dashboard and dry-run alert.
- Produces exact setup instructions for API registration, ntfy, Tesseract, local startup, and disabling startup.

- [ ] **Step 1: Write the failing end-to-end dry-run test**

~~~python
def test_dry_run_from_scan_to_dashboard_and_alert(app_with_fixture_connectors):
    client = TestClient(app_with_fixture_connectors)
    scan = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})
    assert scan.status_code == 200
    assert scan.json()["purchases_attempted"] == 0
    page = client.get("/")
    assert "$60.50" in page.text
    assert "dry-run" in page.text
    assert app_with_fixture_connectors.state.fake_ntfy.messages == []
~~~

The internal scan route exists only when **app.state.testing is True**; it is absent in production.

- [ ] **Step 2: Add security regression tests**

Assert:

- **/.env**, database, logs, and screenshot paths are not served.
- Health and HTML responses contain no configured secret values.
- The app rejects a non-loopback host unless an explicit future security mode exists; version one has none.
- Connector logs redact Authorization and API-key query values.
- Source links use only http/https schemes.
- There are no HTTP methods or connector methods named purchase, reserve, transfer, list, sell, or reprice.

- [ ] **Step 3: Run the end-to-end and security tests**

Run: **python -m pytest tests/e2e/test_dry_run.py tests/test_security_regressions.py -v**

Expected: PASS with fixture connectors and no outbound notification.

- [ ] **Step 4: Write the operator guide**

README sections, in this order:

1. What the reviewer does and does not do.
2. Python virtual environment and **pip install -e ".[dev]"**.
3. Copy **.env.example** to **.env**.
4. Credential links and capability caveats: **https://developer.ticketmaster.com/**, **https://seatgeek.com/build**, and **https://developer.stubhub.com/**.
5. Install Tesseract using **https://tesseract-ocr.github.io/tessdoc/Installation.html** and verify **tesseract --version**.
6. Follow **https://docs.ntfy.sh/subscribe/phone/** to install ntfy on iPhone, choose a random high-entropy topic, and test it.
7. Run Alembic migration.
8. Start locally and open **http://127.0.0.1:8765**.
9. Keep dry-run enabled and verify several real games manually.
10. Set **TR_DRY_RUN=false** only after the notification test succeeds.
11. Install or uninstall the Windows startup task.
12. Backup and delete local history/screenshots.
13. Explain that market estimates use asking prices, not completed sales, and are not guaranteed.

- [ ] **Step 5: Run the complete verification suite**

Run:

~~~powershell
python -m pytest -v
python -m alembic upgrade head
python -m alembic current
git diff --check
~~~

Expected: all tests PASS; Alembic reports the head revision; Git reports no whitespace errors.

- [ ] **Step 6: Perform the manual dry-run checklist**

- Start the application with **TR_DRY_RUN=true**.
- Open **http://127.0.0.1:8765** manually and confirm the service is not reachable through the computer's LAN address.
- Configure available free API keys and confirm each health status.
- Run a scan and compare at least three event signals with their public marketplace pages.
- Upload one screenshot, correct OCR fields, and confirm its score.
- Trigger a test notification, then verify no normal push is sent in dry-run.
- Restart the app and verify observation history and alert fingerprints persist.
- Install the startup task, sign out/in once, and confirm exactly one service instance.
- Uninstall the startup task and confirm no unrelated task changed.

- [ ] **Step 7: Commit**

~~~powershell
git add README.md .env.example ticket_reviewer/main.py tests
git commit -m "docs: complete dry-run and operating guide"
~~~

## Final Completion Gate

Before claiming the reviewer is ready:

- Run the complete verification suite from Task 15 and capture its output.
- Confirm the working tree contains no unexpected changes.
- Confirm no real marketplace key or ntfy topic is committed.
- Confirm at least one screenshot review reaches a deterministic estimate.
- Confirm event-level API signals cannot trigger a pair-opportunity notification.
- Confirm normal alerting remains disabled until the user explicitly changes **TR_DRY_RUN** to false.
- Use **superpowers:verification-before-completion** before reporting success.
