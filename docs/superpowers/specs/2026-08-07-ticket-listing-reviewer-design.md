# Ticket Listing Reviewer Design

**Date:** 2026-08-07
**Status:** Approved for implementation planning

## Purpose

Build a private, local decision-support application that automatically finds potentially profitable pairs of tickets for Houston Texans home games and Texas A&M football home games. The application ranks opportunities primarily by estimated net profit, while using demand, resale speed, data quality, and downside risk to avoid misleading recommendations.

The application recommends opportunities only. It never buys, reserves, transfers, lists, or sells tickets.

## Goals

- Scan permitted marketplace APIs every hour while the user's Windows computer is running.
- Start automatically when the user signs in to Windows.
- Evaluate ticket pairs with an estimated all-in purchase cost of no more than **$400**.
- Send an iPhone push notification when a new opportunity has at least **$50 estimated net profit**.
- Rank primarily by expected net profit, with risk, confidence, and likely resale speed as secondary factors.
- Provide a local dashboard showing the data and assumptions behind every estimate.
- Accept screenshots for manual review when detailed listing data is not available through an approved API.
- Record user outcomes so future versions can calibrate estimates from the user's own history.

## Non-goals

- Automated purchasing, reservation, relisting, repricing, transfer, or selling.
- Website scraping, browser automation, or bypassing marketplace technical controls.
- Guaranteed profit or guaranteed resale-price predictions.
- Cloud hosting or execution while the user's computer is off.
- Away games, neutral-site games, other teams, other sports, single tickets, or quantities other than two.
- Training a machine-learning model in version one.

## Operating Constraints

- The first version must use free access tiers and free local software.
- The only optional external notification dependency is the free ntfy service and iPhone app.
- Marketplace access must use official, permitted APIs and the user's own credentials.
- When a source does not provide listing-level data, the application must label estimates as lower-confidence rather than infer nonexistent seat details.
- Restricted marketplace URLs may be saved as references, but the application will not automatically fetch or extract their page content without authorized access. The user can upload a screenshot of the visible listing instead.

## Source Strategy

### StubHub

Use StubHub's official OAuth API for the event catalog, event search, public listing information, and minimum-price information exposed to the user's approved application. The connector must be capability-driven: if the issued credentials do not include detailed buyer inventory, it will use only the fields actually granted and lower confidence accordingly.

### Ticketmaster

Use the public Discovery API for Houston Texans event discovery and metadata. Use price ranges, resale status, or detailed inventory only if the user's API key is explicitly authorized for the relevant Inventory Status, Top Picks, or partner capability. The application must not assume those restricted capabilities are present.

### SeatGeek

Use the public API as a background signal for Texas A&M home-game metadata, listing count, lowest price, average price, highest price, and popularity where provided. SeatGeek is a comparison input; the user is not required to buy or sell there.

### TickPick

Do not automate collection from TickPick in version one because no suitable public consumer-inventory API has been identified and its terms prohibit unauthorized automated collection. A TickPick URL may be attached to a manual review as a reference. The listing details must come from a user-supplied screenshot or manual corrections.

### Source-policy references

- StubHub API overview: <https://developer.stubhub.com/docs/overview/introduction/>
- Ticketmaster API overview: <https://developer.ticketmaster.com/products-and-docs/apis/getting-started/>
- SeatGeek API documentation: <https://seatgeek.github.io/>
- TickPick user agreement: <https://www.tickpick.com/terms/user-agreement/>
- Texas A&M digital ticketing and SeatGeek resale guide: <https://12thman.com/feature/digital-ticketing-guide>
- Texas A&M 2026 seat-selection policy warning about purchasing primarily for resale profit: <https://app.12thman.com/F26SSAppointmentInfo>
- Houston Texans ticketing page identifying Ticketmaster: <https://www.houstontexans.com/tickets/>

Marketplace rules may change. Connector behavior and policy notes must be configurable so the application can disable a source without affecting the rest of the system.

## Architecture

The application will run as one local Python service with a browser-based interface bound to `127.0.0.1`.

### Runtime components

1. **Local web application**
   - FastAPI serves a small server-rendered dashboard using Jinja templates and HTMX-style interactions.
   - Chart.js renders price history in the browser.
   - The application is accessible only from the local computer by default.

2. **Scheduler**
   - APScheduler performs an immediate scan at service startup and then scans hourly.
   - A Windows Task Scheduler entry starts the service when the user signs in.
   - A single-instance guard prevents duplicate schedulers.

3. **Marketplace connectors**
   - Each source connector implements the same interface for event discovery and price observations.
   - Connectors expose capability flags such as `event_search`, `event_price_range`, and `listing_detail`.
   - One connector failing must not prevent other sources from completing a scan.

4. **Normalization and matching**
   - Normalize source-specific events, venues, sections, rows, quantities, currencies, and price fields.
   - Match events using team, opponent, local start time, and venue.
   - Match seat observations conservatively. Unknown or inconsistent seat locations are not treated as exact comparables.

5. **Opportunity engine**
   - Applies hard eligibility filters.
   - Estimates resale proceeds and net profit.
   - Produces a confidence level and risk explanations.
   - Persists the result and decides whether it merits a notification.

6. **Manual screenshot reviewer**
   - Runs OCR locally through an adapter, with Tesseract as the default implementation.
   - Extracts event, date, marketplace, section, row, quantity, per-ticket price, fees, and total when visible.
   - Requires a confirmation screen so the user can correct every extracted field before scoring.

7. **Persistence**
   - SQLite stores normalized events, source observations, price snapshots, opportunities, manual reviews, outcomes, settings, and alert history.
   - Database migrations keep schema changes explicit.

8. **Notifications**
   - Publish alerts to a high-entropy ntfy topic configured by the user.
   - Notification text contains enough information to act without exposing API credentials or private account data.
   - The action link opens the public marketplace listing when one is available; it does not expose the local dashboard to the internet.

## Data Flow

1. Windows sign-in starts the local service.
2. The scheduler runs an immediate scan and schedules the next scan one hour later.
3. Connectors retrieve supported Texans and Aggies home-game data through official APIs.
4. The normalizer maps source results to shared event and observation records.
5. The event matcher rejects away games, neutral-site games, parking-only products, and ambiguous matches.
6. The opportunity engine considers only quantities of two whose estimated all-in acquisition cost is at most $400.
7. The engine calculates projected resale proceeds, estimated net profit, ROI, confidence, and risk explanations.
8. Results are stored and shown in the dashboard.
9. The alert evaluator compares results with prior alert history and sends a push notification only when the notification rules are met.
10. Source failures are recorded without overwriting the most recent successful snapshot.

## Opportunity Calculation

### Hard eligibility rules

- Team is Houston Texans or Texas A&M football.
- Event is a home game at the expected home venue.
- Listing quantity is exactly two, or the listing explicitly permits buying two adjacent seats.
- Estimated all-in acquisition cost for the pair is no more than $400.
- Event and seat identity are sufficiently clear to avoid comparing unrelated products.

### Acquisition cost

`acquisition_total = pair_price + known_buyer_fees + estimated_tax`

The application uses all-in prices when supplied. If tax or fees are unknown, it uses a configurable source assumption and marks the estimate accordingly.

### Projected resale price

Version one estimates a realistic asking price from the best available comparables in this order:

1. Same event, same section, and comparable row.
2. Same event and same section.
3. Same event and mapped seating zone.
4. Event-level price range and demand signals.

The estimate uses conservative robust statistics, such as a lower weighted median, rather than the highest visible asking price. It then applies adjustments for opponent tier, days until kickoff, inventory levels, price direction, seat quality, and data age. Asking prices are not treated as completed sales.

### Net profit and ROI

`projected_proceeds = projected_resale_gross * (1 - assumed_seller_fee_rate)`

`estimated_net_profit = projected_proceeds - acquisition_total`

`estimated_roi = estimated_net_profit / acquisition_total`

Seller-fee profiles are configurable by marketplace. The dashboard must always display which exit marketplace and fee assumption produced the estimate.

### Ranking and confidence

Estimated net profit is the primary sort key. A secondary risk-adjusted score penalizes stale data, weak seat matching, sparse comparable inventory, declining prices, uncertain fees, and short time to kickoff when demand is weak.

Confidence is presented as **high**, **medium**, or **low** with explicit reasons. A high estimated profit with low-confidence source data remains visible but is clearly distinguished from a high-confidence opportunity.

## Dashboard

### Opportunities

Show ranked eligible pairs with:

- Event and kickoff time
- Section and row when known
- Source marketplace
- Pair acquisition total
- Projected resale gross and assumed exit marketplace
- Estimated net profit and ROI
- Confidence and risk flags
- First-seen and last-seen times
- Source link and current availability state

Filters include team, event, source, confidence, profit, budget, and status.

### Event detail

Show source observations and price history grouped conservatively by seating area. Explain each scoring adjustment and list the comparable observations used for the projection.

### Manual review

Allow screenshot upload, optional reference URL, OCR preview, field correction, and scoring. Manual reviews follow the same budget, profit, fee, confidence, and alert rules as automated observations.

### Outcomes and settings

The user can mark an opportunity as passed, watching, purchased, sold, or expired. A purchased opportunity can record actual purchase cost; a sold opportunity can record sale proceeds and fees. Settings include the $400 cap, $50 alert threshold, source credentials, fee assumptions, ntfy topic, scan interval, and notification test.

## Notification Rules

Send a push notification when all hard eligibility rules pass and one of these conditions is true:

- A newly discovered opportunity has at least $50 estimated net profit.
- A previously known opportunity's estimated profit rises by at least $20 and remains at or above $50.

Do not send an alert for unchanged observations, stale data, low-confidence event matching, or an opportunity already marked passed, purchased, sold, or expired. Store an alert fingerprint and source timestamp to prevent duplicates across restarts.

## Error Handling and Data Freshness

- Isolate connector errors and continue the scan with remaining sources.
- Use bounded retries with backoff for transient network failures and rate limits.
- Preserve the last successful snapshot but mark it stale after the configured freshness window.
- Never send an opportunity alert based solely on stale data.
- Surface credential, quota, parsing, and matching failures in a dashboard health panel with actionable setup guidance.
- Reject screenshots that do not expose enough information to identify the event, quantity, and total cost; retain the image only if the user confirms the manual review.

## Privacy and Security

- Bind the web server to localhost by default.
- Keep API keys and ntfy configuration in an untracked local environment file or OS-protected settings store.
- Commit a redacted example configuration only.
- Exclude credentials, authorization headers, phone identifiers, and raw account responses from logs.
- Use read-only marketplace scopes whenever possible.
- Use a high-entropy ntfy topic and avoid sending sensitive account information in notifications.
- Store screenshots locally and provide a delete action.

## Testing Strategy

### Unit tests

- Pair-cost, fee, tax, proceeds, net-profit, and ROI calculations
- $400 budget filter and $50 notification threshold boundaries
- Home-game, team, quantity, and parking-product eligibility
- Event and seat matching
- Confidence and stale-data rules
- Notification deduplication and $20 improvement rule

### Connector tests

- Recorded sanitized API fixtures for every supported capability
- Missing fields, partial access, expired credentials, rate limits, timeouts, and malformed responses
- Capability downgrade when listing-level access is unavailable

### Integration tests

- Full scan from source fixtures through normalization, scoring, persistence, and alert evaluation
- Scheduler startup, hourly rescheduling, and single-instance behavior
- SQLite migrations and restart recovery
- ntfy test notification using a non-production topic
- OCR extraction followed by required user correction

### Acceptance verification

Before normal alerts are enabled, dry-run mode will process several real home games without sending notifications. The user will compare a sample of recommendations with visible marketplace data. Normal alerting is enabled only after calculations, links, freshness labels, and notifications are verified.

## Acceptance Criteria

The first version is complete when:

1. It starts automatically at Windows sign-in and runs only one local instance.
2. It scans configured permitted sources immediately and every hour while the computer is on.
3. It considers only Texans and Texas A&M football home-game ticket pairs at or below $400 all-in.
4. It calculates and explains projected resale gross, fees, net profit, ROI, confidence, and risk.
5. It sends a deduplicated iPhone push notification for a qualifying new opportunity at or above $50 estimated net profit.
6. It retains price history and clearly labels stale or low-confidence data.
7. It accepts a screenshot, requires correction of OCR results, and scores the confirmed listing.
8. It records user outcomes without claiming those records form a trained prediction model.
9. It continues operating when one connector fails or provides only event-level data.
10. It performs no purchasing, reserving, relisting, selling, or prohibited marketplace scraping.

## Future Enhancements

Future work may add authorized TickPick access, additional teams, cloud execution, a browser helper, richer venue maps, sold-price data from an approved provider, and personalized calibration from recorded outcomes. Each enhancement requires a separate design review and must preserve marketplace-access restrictions.
