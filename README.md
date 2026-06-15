# Stripe → Metronome Sidecar

Please Fork the Repository 

[![CI](https://github.com/REPLACE-ME/stripe-metronome-sidecar/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

> **v0.2a — Customer + subscription create.** This release keeps your Metronome
> customers in lockstep with Stripe, and creates a Metronome contract (with the
> right recurring credit allotment for the chosen tier) the moment a Stripe
> subscription is created. Subscription **updates** and **cancellations** are
> registered as explicit no-ops with `WARNING`-level logs — see
> [What's not handled](#whats-not-handled-yet).

## TL;DR

You run two small Python processes (a webhook receiver and a worker) plus one
Postgres. Stripe sends webhooks → the receiver writes them to Postgres → the
worker reads them out and calls Metronome → mappings of Stripe IDs to
Metronome IDs are persisted. From then on, usage events you send to Metronome
with the Stripe customer ID resolve to the right Metronome customer and draw
down on the contract this sidecar created.

## Why this exists

If you bill on Stripe and meter usage on Metronome, you need a customer in
both systems, a Metronome contract per Stripe subscription, and every
Metronome customer wired to its Stripe counterpart so finalized invoices
push back automatically. Doing that manually is tedious and error-prone
(and easy to get wrong on retries). This service does it automatically and
idempotently from Stripe webhooks.

It is intended to be **forked and customized**, not consumed as a managed
service. Every line you are expected to edit is tagged with `# CUSTOMIZE:`.

## How the full Stripe ↔ Metronome integration fits together

End-to-end usage-based billing on Stripe involves three independent pieces.
This sidecar is one of them — knowing what each piece owns avoids the most
common confusion ("does the sidecar send invoices? does it ingest usage?").

| Piece | What it does | Where it lives |
|---|---|---|
| **Metronome's native Stripe integration** | One-time org-level OAuth connection between your Metronome org and your Stripe account. Lets Metronome create line items / charges on Stripe invoices. | Metronome Dashboard → Integrations → Stripe. Set up once, never again. See [docs.metronome.com/connect-with-stripe](https://docs.metronome.com/connect-with-stripe). |
| **This sidecar** | Stripe → Metronome plumbing: customer create / update, subscription → contract create with the right recurring credit, **and** per-customer `billing_provider=stripe` + `stripe_customer_id` configuration on every Metronome customer (which is what tells Metronome's native integration *where* to push the finalized invoice). | This repo. Two long-running processes + Postgres. |
| **Your application** | Sends usage events to Metronome (using the Stripe customer ID, since this sidecar registers it as an `ingest_alias`). | Wherever your code already runs. |

Put together: a Stripe webhook fires → the sidecar mirrors customer + subscription into Metronome → your app emits usage events → Metronome aggregates them against the contract → Metronome's native integration pushes the finalized invoice line item back onto the Stripe customer's invoice. The sidecar covers the first leg and configures every customer so the last leg is automatic.

## Architecture

> See [`docs/architecture.md`](./docs/architecture.md) for full Mermaid
> diagrams (overview, detailed architecture, happy-path sequence, and the
> event-lifecycle state machine).

```
   ┌───────────┐
   │  Stripe   │
   └─────┬─────┘
         │ webhook POST
         ▼
 ┌───────────────────┐                       ┌──────────────────────────┐
 │  FastAPI receiver │── INSERT (idempotent)─▶│ Postgres                 │
 │  /webhooks/stripe │                       │ webhook_events           │
 │  /health          │                       │ customer_mappings        │
 └───────────────────┘                       │ subscription_mappings    │
                                             └────────────┬─────────────┘
                                                          │ polled (FOR UPDATE
                                                          │ SKIP LOCKED)
                                                          ▼
                                              ┌──────────────────┐
                                              │ Worker process   │
                                              └────────┬─────────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │ Metronome API    │
                                              └──────────────────┘
```

Two long-running Python processes (`server` and `worker`) share one Postgres.
That's the whole thing. No Redis, no Celery, no Kafka — the queue is a Postgres
table because a Postgres table is good enough.

**Key properties**

- **Idempotent receiver.** Every webhook is keyed on `stripe_event_id` and
  inserted with `ON CONFLICT DO NOTHING`. Stripe redeliveries are a no-op.
- **Fast receiver.** The receiver verifies the signature, writes one row, and
  returns 200. It never calls Metronome. Slow receivers cause Stripe webhook
  retries and cascading problems.
- **Crash-safe worker.** The worker holds a row lock for the duration of
  processing. If it dies mid-flight, the transaction rolls back and another
  worker (or the same one after restart) picks the row up.
- **Horizontally scalable.** `SELECT ... FOR UPDATE SKIP LOCKED` means
  multiple worker replicas can run safely.

## What it does and doesn't do

### What's handled

| | In v0.2a |
|---|---|
| `customer.created` → Metronome customer create | ✅ |
| `customer.updated` → Metronome `setName` | ✅ |
| `customer.subscription.created` (single-item) → Metronome contract + recurring credit | ✅ |
| Idempotent webhook persistence (`stripe_event_id` PK + `ON CONFLICT DO NOTHING`) | ✅ |
| Worker-retry idempotency (handler dedupes by `stripe_subscription_id`) | ✅ |
| Cross-process idempotency (deterministic Metronome `uniqueness_key` + 409 recovery) | ✅ |
| Out-of-order webhooks (subscription before customer) → retried with backoff | ✅ |
| Permanent vs. transient error classification with exponential-backoff retries | ✅ |

### What's not handled (yet)

These events land in `webhook_events` and the receiver returns 200, but the
worker treats them as explicit no-ops with a `WARNING`-level log. **The
Metronome contract is not amended or terminated.** Surface them in your
log dashboard or implement them yourself before you depend on them.

| Event | What v0.2a does |
|---|---|
| `customer.subscription.updated` (tier change, plan swap, quantity) | Logs `subscription_update_not_propagated`. Contract still reflects original tier. |
| `customer.subscription.deleted` (cancellation) | Logs `subscription_deletion_not_propagated`. **Contract remains active.** |
| `customer.deleted` | Logged as ignored. Mapping row stays. |
| Multi-item subscriptions | Rejected with `PermanentHandlerError` at the handler. |
| Trial-end transitions | No special handling; `trialing` and `active` produce identical contracts. |
| Prorations / mid-period plan changes | Not modeled. |
| Backfill of pre-existing Stripe customers / subscriptions | Out of scope. Run a one-shot script. |

### Not part of this sidecar (lives in another piece of the integration)

These are part of the end-to-end use case but **not** this codebase's job.
Confusion about this is the most common first-day question.

| | Where it lives instead |
|---|---|
| Usage event ingestion | Your application sends usage events directly to Metronome's ingestion endpoint, keyed on the Stripe customer ID. The sidecar's customer mapper registers that ID as a Metronome `ingest_alias`. |
| Finalized invoice push-back (Metronome → Stripe) | Handled by Metronome's native Stripe integration (org-level OAuth, one-time setup). The sidecar enables this *per customer* by setting `billing_provider=stripe` + `stripe_customer_id` on every Metronome customer it creates. |
| Stripe Connect / multi-account | Out of scope. Single-Stripe-account setups only. |
| UI / dashboard for `webhook_events` | Out of scope. Use `psql` and your log aggregator. |
| Terraform / Helm / Kubernetes manifests | Out of scope. Every team has a deployment platform of choice; a prescriptive one would just be in your way. |

---

## Local setup walkthrough

> This section is for **getting the sidecar running on a laptop so you can
> see what it does and customize the mappers.** Production deployment is a
> different topology — see [Production deployment](#production-deployment).
> The same code runs in both; only the surrounding infrastructure changes.

If you've never run this before, follow the five steps below in order. If you
get stuck, jump to [Troubleshooting](#troubleshooting). The Docker path is
recommended for first-time setup; the no-Docker path is below it.

### What you'll need

- A **Stripe account** (test mode is fine).
- A **Metronome account** with at least one rate card and at least one
  product + credit type configured. Set those up in the Metronome dashboard
  before you start.
- One of:
  - **Docker Desktop** (easiest path), OR
  - **Python 3.12+** and a local **Postgres 16** (Homebrew works).
- The **Stripe CLI** for forwarding webhooks during local dev:
  [install instructions](https://docs.stripe.com/stripe-cli).

### Step 1: Clone the repo

```bash
git clone https://github.com/REPLACE-ME/stripe-metronome-sidecar.git
cd stripe-metronome-sidecar
cp .env.example .env
```

The `.env` file is gitignored, so the secrets you put in it stay on your
laptop.

### Step 2: Get your Stripe webhook secret

In a new terminal:

```bash
stripe login                                                    # one-time
stripe listen --forward-to localhost:8000/webhooks/stripe
```

The CLI prints a line like `Your webhook signing secret is whsec_…`. Copy
that value and paste it into `.env` as `STRIPE_WEBHOOK_SECRET=whsec_…`.

Leave this `stripe listen` terminal running for the rest of the walkthrough —
it's the bridge that delivers Stripe webhooks to your local sidecar.

### Step 3: Get your Metronome credentials

Open the Metronome dashboard and grab three IDs. They are all different — see
the [Where each ID comes from](#where-each-id-comes-from) table for the
distinction.

1. **API key.** Settings → API tokens → Create token. Paste into `.env` as
   `METRONOME_API_KEY=…`.
2. **Rate card UUID.** Rate Cards → click the rate card you want every
   contract bound to → copy the ID. Paste into `.env` as
   `METRONOME_DEFAULT_RATE_CARD_ID=<uuid>`. **This is validated as a UUID at
   process startup** — the placeholder will refuse to boot, on purpose.
3. **Product UUID** and **credit type UUID** for the recurring credit you
   want each subscription to grant. Products and Credit Types in the
   dashboard. You'll paste these into `tiers.py` in the next step, not into
   `.env`.

### Step 4: Configure your tier(s)

Open `src/sidecar/config/tiers.py` and edit the `TIERS` dict. For every
Stripe price you charge that should produce a Metronome contract, add an
entry. The dict is keyed by **Stripe price ID** (`price_…`, the recurring
price the subscription is billed against, *not* the product ID).

Example with realistic values:

```python
TIERS: dict[str, Tier] = {
    "price_1ABC...XYZ": Tier(                              # ← Stripe price ID
        name="startup",
        rank=1,                                            # ordering for upgrade detection (v0.2b)
        credit_amount_per_period=10_000_000,               # 10M units of the credit type
        metronome_credit_product_id="<metronome product UUID>",
        metronome_credit_type_id="<metronome credit type UUID>",
        recurrence_frequency="MONTHLY",                    # MONTHLY | QUARTERLY | ANNUAL | WEEKLY
    ),
}
```

Delete the example placeholder rows once you've added your real ones. A
subscription whose price is *not* in `TIERS` will permanently fail with
`UnknownTierError` — the sidecar won't guess.

### Step 5: Run it

You have two choices. Pick one.

#### Option A — Docker (recommended for first run)

```bash
docker compose up --build
```

That brings up Postgres, runs migrations, starts the receiver on port 8000,
and starts the worker. You should see log lines like `server_started` and
`worker_started`. Leave it running.

If you change `.env` or `tiers.py`, stop with `Ctrl-C` and re-run the same
command.

#### Option B — No Docker (three terminals on your host)

You'll need a local Postgres listening on `:5432`. With Homebrew:

```bash
brew install postgresql@16
brew services start postgresql@16
createuser -s sidecar
createdb -O sidecar sidecar
psql -d sidecar -c "ALTER USER sidecar WITH PASSWORD 'localdev';"
```

Make sure your `.env` has `DATABASE_URL=postgresql+asyncpg://sidecar:localdev@localhost:5432/sidecar`
(with `localhost`, not `db`). Then, one-time Python setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
```

Now open three terminals (each with `source .venv/bin/activate` first) and
run one command in each:

```bash
# Terminal 1 — receiver
uvicorn sidecar.server:app --reload --port 8000

# Terminal 2 — worker
python -m sidecar.worker

# Terminal 3 — Stripe webhook forwarder (already running from Step 2)
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Whenever you edit `tiers.py` or `.env`, restart whichever process(es) are
affected. The receiver auto-reloads on code changes (because of `--reload`);
the worker doesn't, so you have to `Ctrl-C` and re-run it.

### Step 6: Verify it works end-to-end

In a new terminal, create a customer in Stripe and a subscription against
the price you configured in `tiers.py`:

```bash
stripe customers create --email test@example.com
# Note the cus_... ID it prints.

stripe subscriptions create \
  --customer cus_THE_ID_FROM_ABOVE \
  --items 'price=price_THE_ID_YOU_ADDED_TO_tiers.py'
```

Within ~2 seconds the worker will process both webhooks. Verify both
mappings were written:

```bash
# If you used Docker:
docker compose exec db psql -U sidecar -d sidecar -c "
  SELECT stripe_customer_id, metronome_customer_id FROM customer_mappings;
  SELECT stripe_subscription_id, metronome_contract_id, current_tier_name
    FROM subscription_mappings;
"

# If you used the host path:
psql -d sidecar -c "
  SELECT stripe_customer_id, metronome_customer_id FROM customer_mappings;
  SELECT stripe_subscription_id, metronome_contract_id, current_tier_name
    FROM subscription_mappings;
"
```

You should see one row in each table. Open the Metronome dashboard and
confirm:
- The new customer exists and its `external_id` matches `cus_…`.
- A contract exists for that customer, named
  `Stripe Subscription sub_… (<tier name>)`, bound to your rate card, with
  one recurring credit sized to the tier you configured.

If anything is missing, check the worker logs and the [Troubleshooting](#troubleshooting) section.

---

## Configuration reference

All configuration is by environment variable. See `.env.example` for the
canonical list; the table below is the same set with defaults made explicit.

| Variable | Required | Default | What it does |
|---|---|---|---|
| `STRIPE_WEBHOOK_SECRET` | ✅ | — | Signing secret from Stripe (or `stripe listen`). Used to verify each webhook. |
| `METRONOME_API_KEY` | ✅ | — | Bearer token for the Metronome API. |
| `DATABASE_URL` | ✅ | — | SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pass@host:5432/db`. |
| `METRONOME_DEFAULT_RATE_CARD_ID` | ✅ | — | UUID of the Metronome rate card every contract is bound to. **Validated at process startup** — a non-UUID value (including the `.env.example` placeholder) refuses to boot, by design. Find it under Rate Cards in the Metronome dashboard. |
| `LOG_LEVEL` | | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `WORKER_POLL_INTERVAL_SECONDS` | | `2` | How long the worker sleeps when the queue is empty. |
| `WORKER_MAX_ATTEMPTS` | | `5` | After this many failed attempts an event is marked `failed`. |
| `WORKER_RETRY_BASE_SECONDS` | | `30` | Base for exponential backoff. Retry n is scheduled at `min(BASE * 2^(n-1), CAP)` + jitter. |
| `WORKER_RETRY_CAP_SECONDS` | | `3600` | Maximum retry delay regardless of attempt count. |
| `METRONOME_BASE_URL` | | `https://api.metronome.com` | Override only for non-production Metronome environments. |
| `PORT` | | `8000` | HTTP listen port for the receiver. |

### Where each ID comes from

A common first-day question. None of these are interchangeable.

| ID | Where to get it | Where it's used |
|---|---|---|
| Stripe **price** ID (`price_…`) | Stripe Dashboard → Products → click product → click the price row | The **key** in `TIERS` (`src/sidecar/config/tiers.py`). Determines which tier a subscription resolves to. |
| Stripe **product** ID (`prod_…`) | Same place, top of the product page | Not used by this sidecar. Ignore it. |
| Metronome **rate card** UUID | Metronome Dashboard → Rate Cards → copy ID | `METRONOME_DEFAULT_RATE_CARD_ID` env var. Every contract is bound to this. |
| Metronome **product** UUID | Metronome Dashboard → Products | `Tier.metronome_credit_product_id` in `TIERS`. The product the recurring credit draws against. |
| Metronome **credit type** UUID | Metronome Dashboard → Credit Types | `Tier.metronome_credit_type_id` in `TIERS`. The unit the allotment is denominated in (USD cents, events, etc.). |

## Running tests

```bash
docker compose up -d db                     # Postgres only
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL="postgresql+asyncpg://sidecar:localdev@localhost:5432/sidecar"
export STRIPE_WEBHOOK_SECRET=whsec_test_secret_value
export METRONOME_API_KEY=test-token
export METRONOME_DEFAULT_RATE_CARD_ID=00000000-0000-0000-0000-000000000001
alembic upgrade head
pytest -v
```

> **Python version.** The full test suite is verified on Python 3.12 (what
> the Dockerfile and CI use). On Python 3.14, two of the receiver tests
> (`test_valid_signature_persists_event`,
> `test_duplicate_event_id_returns_200_without_inserting_new_row`) fail
> during teardown with "Future attached to a different loop" — a known
> interaction between FastAPI's `TestClient`, asyncpg, and 3.14's stricter
> asyncio shutdown. All other tests, including every mapper and handler
> test, pass on both. If you are forking on 3.14, run the full suite via
> `docker compose run --rm migrate pytest` (uses the 3.12 image) for a
> clean green.

## What to customize

The intent is that you fork this repo, change at most a handful of files, and
deploy. The customization seams are:

1. **`src/sidecar/config/tiers.py`** — the `TIERS` dict mapping Stripe price
   IDs to Metronome credit allotments. Every Stripe price you charge must
   have an entry here, or the subscription handler permanently fails with
   `UnknownTierError`. Tier name, recurrence frequency, credit amount, and
   credit product / type UUIDs all live here.

2. **`src/sidecar/mappers/customer.py`** and **`mappers/subscription.py`** —
   pure functions that build the Metronome request body. Every line tagged
   `# CUSTOMIZE:` is a fork-friendly seam. Typical edits:
   - Map Stripe `metadata` keys onto Metronome `custom_fields`.
   - Add additional `ingest_aliases` (e.g. an internal account ID).
   - Change the contract `name` template.
   - Adjust `rollover_fraction`, `priority`, or `commit_duration`.
   - Switch the billing provider configuration.

3. **`src/sidecar/handlers/customer.py`** and **`handlers/subscription.py`** —
   change *what* happens on each event type (e.g. write a row to your own
   analytics DB, fire an internal event, terminate a contract on cancel).
   Keep the orchestration shape — dedupe, mapper, Metronome call, mapping
   write — so the idempotency guarantees stay correct.

4. **`src/sidecar/worker.py`** — the `HANDLERS` registry. Adding a new
   Stripe event type is a one-line change plus a new handler module.

Because the mappers are pure functions, you can unit-test your edits with
fixtures alone — see `tests/test_mappers_customer.py` and
`tests/test_mappers_subscription.py` for templates. For the orchestration
layer, `tests/test_handlers_customer.py` and
`tests/test_handlers_subscription.py` show the integration-test pattern
(real Postgres, `respx`-mocked Metronome).

## Production deployment

The local-dev setup above (Docker Compose, or three terminals on a laptop)
is intentionally minimal. **The same code is what you run in production** —
nothing in this repo is "for local only." The local setup just collapses
the production topology onto one machine. This section answers "what does
that production topology look like, and is it good enough for a real
business?"

### Topology

```
   Stripe ───── HTTPS POST ─────▶ Load balancer
                                       │
                                       ▼
                               ┌───────────────┐         ┌─────────────────┐
                               │  Receiver     │         │  Worker         │
                               │  (uvicorn,    │         │  (no port,      │
                               │   stateless,  │         │   stateless,    │
                               │   N replicas) │         │   N replicas)   │
                               └───────┬───────┘         └────────┬────────┘
                                       │                          │
                                       ▼                          ▼
                                  ┌────────────────────────────────┐
                                  │  Managed Postgres              │
                                  │  webhook_events / *_mappings   │
                                  └────────────────────────────────┘
```

- **Receiver**: stateless HTTP service. Run **two or more replicas** behind
  any L7 load balancer with TLS termination. Every Stripe webhook lands on
  one replica, gets verified, written to Postgres in one round-trip,
  returns 200. Receiver replicas don't talk to each other.
- **Worker**: same Docker image, different command (`python -m
  sidecar.worker`). No ports exposed. Horizontally scalable: multiple
  replicas are safe because the queue query uses
  `SELECT … FOR UPDATE SKIP LOCKED`. Two workers polling the same row see
  exactly one of them claim it.
- **Postgres**: managed (RDS, Cloud SQL, Neon, Supabase, etc.). Schema is
  three tables and a partial index. Point-in-time recovery should be on —
  `webhook_events` is your audit log of every Stripe message you've ever
  received.
- **Migrations**: run `alembic upgrade head` once per release, **before**
  rolling out the new app version. The `migrate` service in
  `docker-compose.yml` shows the command; replicate it as a CI step, a
  Kubernetes Job, or an ECS one-shot task.
- **Webhook endpoint**: configure in Stripe Dashboard → Developers →
  Webhooks → `https://<your-host>/webhooks/stripe`. Subscribe to at least
  `customer.created`, `customer.updated`, and
  `customer.subscription.created`. Subscribe to
  `customer.subscription.updated` and `customer.subscription.deleted` too
  if you want the warning-level audit trail (see [What's not handled](#whats-not-handled-yet)).
  Pin the Stripe API version on the endpoint and don't change it without
  re-running the test suite.
- **Secrets**: `STRIPE_WEBHOOK_SECRET` and `METRONOME_API_KEY` should come
  from your secrets manager (AWS Secrets Manager, GCP Secret Manager,
  Vault). They are read at process startup and not refreshed at runtime;
  redeploy when rotating.
- **Logging**: JSON to stdout. Ship it to the same place as your other
  services.

### Will this scale to a real customer base?

**Short answer: yes, for tens of thousands of customers and well into
hundreds of webhooks per second.** Long answer:

| Concern | What's true today | What you may want to add |
|---|---|---|
| Throughput on the receiver | A 1 vCPU receiver replica handles >1k webhooks/sec because all it does is verify HMAC + insert one row. Scale horizontally — Stripe webhooks are independent events. | Standard L7 LB. |
| Worker throughput | One worker processes events serially per replica (each event holds a row lock during the Metronome call). To raise total throughput, run more worker replicas. Practical ceiling is set by Metronome's API rate limits, not Postgres. | More replicas. Watch Metronome 429s — the worker already retries them, but consistent rate limiting means you need fewer parallel workers, not more. |
| Postgres load | Three tables, partial index on the hot path, `INSERT … ON CONFLICT DO NOTHING` on the receiver, `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1` on the worker. A small managed Postgres is enough for thousands of webhooks per minute. | Right-size the instance; enable PITR. |
| `webhook_events` table growth | Grows by one row per Stripe webhook *of every type*, processed or not. At 100k events/day that's ~36M rows/year. Postgres handles it; query performance does not degrade because of the partial index. | Add a retention job that archives or deletes `processed` rows older than N days. Out of scope here — write a small cron / scheduled task. |
| `customer_mappings` / `subscription_mappings` growth | One row per customer / subscription. At your scale, well under any limit worth thinking about. | None. |
| Metronome API rate limits | The client classifies 429 + 5xx as transient and retries with exponential backoff + full jitter. Permanent 4xx are marked `failed` and stop retrying. | Tune `WORKER_RETRY_BASE_SECONDS` / `WORKER_RETRY_CAP_SECONDS` if you have a known burst pattern. |
| Stripe webhook retries | Stripe retries non-2xx responses for up to ~3 days. The receiver is idempotent on `stripe_event_id`, so retries are safe and a no-op. | None. |
| Observability | JSON logs only. Counters / SLOs are bring-your-own. | Wire to Datadog / Prometheus / Cloudwatch. Useful metrics: `webhook_events.status='failed'` count (page on > 0), oldest pending event age (page on > 5 min), p95 handler latency from the `handler_completed` log lines. |
| Multi-region / DR | Single-Postgres design. A regional outage in your DB region takes the sidecar offline, but Stripe will keep retrying for ~3 days, so events aren't lost. | Replicate your managed Postgres across regions if your RTO requires it. |
| Backfill of pre-existing customers | Out of scope. | Write a one-shot script that pages Stripe `/v1/customers` and `/v1/subscriptions` and feeds each through the same handlers. Roughly a day of work. |
| Stripe Connect / multiple Stripe accounts | Not supported. The signing secret is a single value. | Run one sidecar instance per connected account if you absolutely need this. |

### Gaps a large business should size carefully before depending on this

These aren't capacity issues — they're behavioral gaps that are bigger
problems at scale than they look:

1. **`customer.subscription.deleted` does not terminate the Metronome
   contract.** If you have a meaningful churn rate, those contracts pile
   up and continue accruing credit. v0.2b territory; work around with a
   reconciliation job until then.
2. **`customer.subscription.updated` does not propagate plan changes.**
   Mid-period upgrades / downgrades on Stripe are silently not reflected
   in Metronome. Same v0.2b answer.
3. **No backfill mode.** The sidecar only acts on webhooks going forward
   from the moment you turn it on. Pre-existing customers / subscriptions
   need a one-shot script.
4. **Tier config (`TIERS`) drift.** Adding a new Stripe price without a
   corresponding entry in `src/sidecar/config/tiers.py` permanently fails
   every subscription on that price (loud, but fail-closed). Make
   `tiers.py` part of your launch checklist.
5. **No metrics / alerting out of the box.** You will want a dashboard
   showing `webhook_events` by status and age — wiring it up is on you.

If those tradeoffs are acceptable (or you plan to close them yourself),
the architecture is genuinely production-grade for a tens-of-thousands-of-
customers business. If they aren't, treat this repo as an excellent
starting point that you'll extend, not a finished product.

## Troubleshooting

The top eight things that go wrong, in roughly the order they happen during
first integration:

1. **`uvicorn: command not found` (no-Docker path).**
   Your virtual environment isn't active. Run `source .venv/bin/activate`
   in this terminal first. You need to do this in *every* terminal that
   runs a sidecar process.

2. **The worker refuses to start with a UUID validation error.**
   `METRONOME_DEFAULT_RATE_CARD_ID` is still a placeholder, or you set it
   to something that isn't a UUID. Paste the rate card UUID from the
   Metronome dashboard (Rate Cards → copy ID) into `.env` and restart.

3. **400 from `/webhooks/stripe` with `invalid_signature` in logs.**
   Almost always means `STRIPE_WEBHOOK_SECRET` does not match the secret
   of the webhook endpoint Stripe is delivering to. Double-check that you
   restarted the receiver after editing `.env`, and that `stripe listen`
   prints the *same* `whsec_…` you copied in.

4. **`docker compose up` exits and `migrate` shows
   `connection refused` errors.**
   The migrator started before Postgres was ready. The compose file uses a
   healthcheck + `depends_on: condition: service_healthy`, but a slow disk
   or a stale `pgdata` volume can still trip it. Try
   `docker compose down -v && docker compose up --build`.

5. **`webhook_events` rows stuck in `status='pending'`.**
   The worker isn't running or can't reach Metronome.
   - Check the worker logs for `worker_started`.
   - Look for `event_retry_scheduled` — it includes `last_error` with the
     Metronome HTTP status.
   - Verify `METRONOME_API_KEY` is valid by hitting the API by hand:
     `curl -H "Authorization: Bearer $METRONOME_API_KEY" $METRONOME_BASE_URL/v1/customers`.

6. **Rows in `status='failed'` after deploying.**
   Permanent failures (mapper errors, Metronome 4xx). Inspect them with:
   ```sql
   SELECT stripe_event_id, event_type, attempts, last_error
   FROM webhook_events
   WHERE status = 'failed'
   ORDER BY received_at DESC;
   ```
   To replay one after fixing the root cause:
   ```sql
   UPDATE webhook_events
   SET status = 'pending', attempts = 0, next_attempt_at = NOW(), last_error = NULL
   WHERE stripe_event_id = 'evt_…';
   ```

7. **Subscription event marked `failed` with `UnknownTierError`.**
   The Stripe price ID is not in `TIERS`. Either add an entry to
   `src/sidecar/config/tiers.py` and redeploy, or — if this price isn't
   supposed to provision a Metronome contract — filter it out at the
   receiver. Replay with the SQL above once the config is fixed.

8. **Stripe subscription cancelled but Metronome contract still active.**
   Expected — see [What's not handled](#whats-not-handled-yet).
   `customer.subscription.deleted` is registered as an explicit no-op with
   a `WARNING`-level log (`subscription_deletion_not_propagated`). If your
   billing model needs the contract terminated, either implement the
   handler yourself or run an external reconciliation job that walks
   `subscription_mappings` and terminates contracts whose Stripe
   subscription is cancelled.

## Contributing

Issues and PRs welcome — please open an issue describing the change before
sending a large PR. Run `ruff check`, `mypy src`, and `pytest` locally before
opening; CI does the same.

A contribution guide stub lives at [`CONTRIBUTING.md`](./CONTRIBUTING.md)
(not yet written — happy to take a PR adding one).

## License

[Apache License 2.0](./LICENSE).
