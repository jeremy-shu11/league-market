# The League Market

Closed, play-money prediction exchange for a Sleeper fantasy football league. Opening odds are generated from league scoring, legal lineups, weekly projections, standings, schedule, and prior-season score volatility. Trading uses a model-prior-anchored LMSR; refreshed models remain visible as fair value without repricing existing positions.

## Quick Start

```bash
cd /Users/jeremyshu/Desktop/Dynasty/league-market
python3 -m pip install -r backend/requirements.txt
python3 backend/app.py
```

Open `http://127.0.0.1:5065`.

Default local codes:

- Invite code: `theleague`
- Admin code: `commissioner`

Override them with environment variables:

```bash
LEAGUE_MARKET_INVITE_CODE=your-code LEAGUE_MARKET_ADMIN_CODE=your-admin-code python3 backend/app.py
```

## Nuxt Shell

The prediction-market UI now has a Nuxt/Nuxt UI shell around the existing trading runtime. The FastAPI app serves the generated Nuxt public output when `.output/public/index.html` exists, then falls back to `frontend/index.html` for local backend-only work.

```bash
npm run dev:nuxt
npm run generate:nuxt
python3 backend/app.py
```

`npm run dev:nuxt` proxies `/api` to the FastAPI server on `127.0.0.1:5065`. Run `python3 backend/app.py` in another terminal when using the Nuxt dev server. `npm run generate:nuxt` refreshes the legacy runtime copy for Nuxt dev assets and writes the static Nuxt bundle used by FastAPI.

## Tests

```bash
npm install
npm run test:backend
npm run test:e2e
npm run qa:automation
```

Playwright runs on `127.0.0.1:5066` with a freshly recreated database at `/private/tmp/league-market-e2e.sqlite`. It never reuses the development server or `data/market.sqlite`.

For a production-mode layout rehearsal against an isolated restored database:

```bash
LEAGUE_MARKET_URL=http://127.0.0.1:5072 \
LEAGUE_MARKET_INVITE_CODE=your-rehearsal-code \
npm run qa:production
```

The rehearsal checks mobile and desktop overflow, binary outcome alignment, the price tape, API failures, browser errors, and commissioner-control visibility.

`npm run qa:automation` runs the launch-critical lifecycle against an isolated database. It closes Week 1 at kickoff, confirms Sleeper is not polled before the deadline, settles the winner from the Tuesday 1:00 AM ET snapshot, voids and refunds an exact tie, records the scheduler job, and verifies modeled markets never enter the manual resolution inbox.

## Production

Production mode refuses to start with the built-in local invite/admin codes. Start from `.env.example`, generate private replacements, and configure the public hostname and origin explicitly.

```bash
docker build -t league-market .
docker run --rm -p 5065:5065 \
  --env-file .env \
  -v league-market-data:/data \
  league-market
```

The SQLite database lives at `LEAGUE_MARKET_DB`; mount its parent directory as persistent storage. Run one application process against that database. Put TLS and request-rate limiting at the reverse proxy, and use `/api/health` for health checks.

## Free Vercel-Style Deploy

Pure Vercel hosting cannot safely persist the local SQLite file. For a free, multi-user launch, use Vercel Hobby for the FastAPI/Nuxt app and Turso free for shared SQLite-compatible storage.

1. Create a Turso database and token.
2. Import this repository into Vercel with the project root set to `league-market`.
3. Keep the included `vercel.json`; it runs `npm run generate:nuxt` and routes all requests through the FastAPI function at `api/index.py`.
4. Add these Vercel environment variables:

```text
LEAGUE_MARKET_ENV=production
LEAGUE_MARKET_INVITE_CODE=<12+ character private invite>
LEAGUE_MARKET_ADMIN_CODE=<16+ character private admin code>
LEAGUE_MARKET_ALLOWED_HOSTS=<your-app>.vercel.app
LEAGUE_MARKET_ALLOWED_ORIGINS=https://<your-app>.vercel.app
LEAGUE_MARKET_SCHEDULE_PIPELINE=0
LEAGUE_MARKET_AUTOMATION_DASHBOARD_GRACE_HOURS=1
LEAGUE_MARKET_AUTOMATION_RUNNING_TIMEOUT_HOURS=0.25
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
LEAGUE_MARKET_DATA_DIR=/tmp/league-market
```

For a custom domain, add it to both `LEAGUE_MARKET_ALLOWED_HOSTS` and `LEAGUE_MARKET_ALLOWED_ORIGINS`. Vercel Hobby cron cannot run every 15 minutes, so keep `.github/workflows/league-market-scheduler.yml` enabled and set its repository secrets after the Vercel URL is live. Keep `LEAGUE_MARKET_SCHEDULE_PIPELINE=0` on serverless deployments unless the model refresh is moved to a runner with a longer execution window.

Runtime settings:

- `LEAGUE_MARKET_ENV`: `development`, `test`, or `production`
- `LEAGUE_MARKET_HOST` / `LEAGUE_MARKET_PORT`: bind address and port
- `LEAGUE_MARKET_DB`: persistent SQLite file path
- `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`: optional remote libSQL database for serverless hosts
- `LEAGUE_MARKET_RAW_DATA`: content-addressed gzip ingestion artifacts
- `LEAGUE_MARKET_BACKUP_DIR`: SQLite online backups
- `LEAGUE_MARKET_SIMULATIONS`: deterministic Monte Carlo sample count; default `20000`
- `LEAGUE_MARKET_SCHEDULE_PIPELINE`: set `0` to keep scheduled automation from running the heavier model pipeline
- `LEAGUE_MARKET_AUTOMATION_DASHBOARD_GRACE_HOURS`: stale-alert window for frequent scheduler jobs; default `1`
- `LEAGUE_MARKET_AUTOMATION_RUNNING_TIMEOUT_HOURS`: max age for a running job before Admin treats it as interrupted; default `0.25`
- `LEAGUE_MARKET_INVITE_CODE`: 12+ characters in production
- `LEAGUE_MARKET_ADMIN_CODE`: 16+ characters in production
- `LEAGUE_MARKET_ALLOWED_HOSTS`: comma-separated HTTP hostnames
- `LEAGUE_MARKET_ALLOWED_ORIGINS`: comma-separated browser origins

## Market Pipeline

The commissioner workflow is idempotent:

```text
ingest -> validate -> model -> draft/version -> publish -> close -> resolve|void
```

`POST /api/admin/pipeline` refreshes Sleeper league data and projections, validates 95% starter coverage and 24-hour freshness, runs the recorded-seed model, and originates only champion, team playoff, and current-week high/low markets. Repeating it never resets inventory, trades, opening priors, or contract versions.

The projection policy is Sleeper live, then same-week last-known-good data under 24 hours, then a freshness-checked nflverse/dynastyprocess rankings adapter. Rankings-only data still fails publication when the league's custom stat categories cannot be modeled. Fixtures are test-only and are never a production fallback.

The canonical scheduler target is `POST /api/admin/scheduled/run` with the `X-Admin-Code` header. Call it every 15 minutes. Each invocation runs lifecycle checks, live Sleeper score marks for active weekly markets, then conditionally runs backup/prune every 24 hours. If `LEAGUE_MARKET_SCHEDULE_PIPELINE=1`, it also runs the model pipeline every 12 hours. Jobs are serialized, persisted in `job_runs`, and reported in Admin.

The included `.github/workflows/league-market-scheduler.yml` provides that schedule. It health-checks the host, calls the canonical scheduler endpoint, validates that lifecycle completed with the expected result fields, and writes counts to the GitHub Actions job summary. Add repository secrets:

- `LEAGUE_MARKET_URL`: the public HTTPS origin, without an API path
- `LEAGUE_MARKET_ADMIN_CODE`: the production admin code

The separate `League Market Production Pipeline` workflow runs the heavier data and model refresh on a GitHub-hosted runner instead of inside Vercel's request window. Add `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` as repository secrets, then run the workflow manually from the Actions tab whenever Admin reports that the data pipeline needs attention. Its optional simulation-count input defaults to `20000`.

The individual pipeline, live score mark, lifecycle, backup, and prune endpoints remain available for focused retries.

## Restore Drill

Backups are written to a temporary file, checked with `PRAGMA integrity_check`, and atomically renamed only after validation. To restore, stop the single application writer, preserve the current database, copy the selected backup to `LEAGUE_MARKET_DB`, verify it with `sqlite3 "$LEAGUE_MARKET_DB" 'PRAGMA integrity_check;'`, and restart the application. Never restore while the application process is writing.

Weekly markets close at the first NFL kickoff assumption or, for irregular weeks, as soon as Sleeper begins reporting nonzero matchup points. Live score marks update the weekly high/low model odds from actual Sleeper points plus projections for unscored starters in the submitted lineup; they do not reprice the LMSR book or reset existing positions. At Tuesday 1:00 AM ET the lifecycle job fetches finalized Sleeper matchup scores and settles each weekly high/low market in the same run. Exact official ties are voided and refunded. Manual commissioner resolution remains available only for manually originated contracts.

## Data Model

- Normalized NFL players, league memberships, and projection observations replace embedded global catalogs.
- Every published model records input hashes, model version, seed, simulations, coverage, assumptions, and diagnostics.
- Contract definitions and immutable market versions are paired with append-only events and complete per-outcome price ticks.
- Trades require a current revision and a `max_cost` or `min_proceeds`; stale or slippage-exceeding submissions return `409` atomically.
- Raw inputs referenced by published models are retained through the season. Unreferenced artifacts are pruned after 14 days.

## Notes

- This is play money only.
- Sleeper is read-only and used for league structure, projections, standings, schedule, and resolution evidence.
- One hosted application process and one scheduled writer are required for the launch SQLite architecture.
- Postgres, multi-league tenancy, real-money accounting, order books, automatic repricing, and advanced player correlation are explicitly post-launch work.
- To switch leagues, open Admin, enter a Sleeper league ID, preview it, then run the launch pipeline.
- Player headshots use Sleeper CDN image URLs for real numeric Sleeper player IDs. Fixture/demo player IDs fall back to initials.
