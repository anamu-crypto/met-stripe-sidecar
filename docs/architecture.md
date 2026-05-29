# Architecture

> Three diagrams of the Stripe → Metronome sidecar (v0.1, customer sync only).
> Each is plain Mermaid — render inline on GitHub, edit at [mermaid.live](https://mermaid.live),
> or import into Lucidchart via `File → Import → Mermaid`.

## At a glance

A Stripe webhook lands → we persist it → a worker calls Metronome → we record the ID mapping. That's the whole loop.

```mermaid
flowchart LR
    Stripe["Stripe"] -->|"customer.created /<br/>customer.updated"| Receiver
    Receiver["Receiver<br/>(FastAPI)"] -->|"persist event"| DB[("Postgres")]
    DB -->|"claim pending event"| Worker
    Worker["Worker<br/>(polling loop)"] -->|"create / update customer"| Metronome["Metronome"]
    Worker -->|"record id mapping"| DB

    classDef external fill:#f4f4f4,stroke:#444,color:#222
    classDef process fill:#e6f0ff,stroke:#2961c1,color:#0a2548
    classDef store fill:#fff0e0,stroke:#bf6516,color:#5a2f00

    class Stripe,Metronome external
    class Receiver,Worker process
    class DB store
```

The receiver and worker are deliberately split so the receiver can return `200 OK` to Stripe in milliseconds regardless of how slow Metronome is. The worker carries the slow, retry-able work.

## Detailed architecture

The same picture with the extras: the local-dev tunnel, the database tables, and the second data flow this sidecar does **not** handle (usage events going straight to Metronome, and Metronome posting charges back to Stripe invoices).

```mermaid
flowchart LR
    Stripe["Stripe<br/>(cloud)"]
    Metronome["Metronome<br/>(cloud)"]
    CustomerApp["Customer's app"]

    subgraph SidecarHost["Sidecar host / your infra"]
        StripeListen["stripe listen<br/>(local dev only)"]
        Receiver["Receiver<br/>FastAPI :8000<br/>POST /webhooks/stripe"]
        Worker["Worker<br/>SELECT FOR UPDATE SKIP LOCKED<br/>polling loop"]
        DB[("Postgres<br/>webhook_events<br/>customer_mappings")]
    end

    Stripe -->|"signed webhook"| StripeListen
    StripeListen -->|"HTTPS POST"| Receiver
    Receiver -->|"INSERT ON CONFLICT DO NOTHING"| DB

    DB -->|"claim pending event"| Worker
    Worker -->|"POST /v1/customers<br/>or setName"| Metronome
    Worker -->|"INSERT customer_mappings<br/>UPDATE webhook_events"| DB

    CustomerApp -.->|"usage events<br/>customer_id=cus_xxx"| Metronome
    Metronome -.->|"charges onto Stripe invoice"| Stripe

    classDef external fill:#f4f4f4,stroke:#444,color:#222
    classDef process fill:#e6f0ff,stroke:#2961c1,color:#0a2548
    classDef store fill:#fff0e0,stroke:#bf6516,color:#5a2f00
    classDef devonly fill:#f0f0f0,stroke:#888,color:#444,stroke-dasharray:4 3

    class Stripe,Metronome,CustomerApp external
    class Receiver,Worker process
    class DB store
    class StripeListen devonly
```

- **Solid arrows** = handled by this sidecar.
- **Dotted arrows** = configured separately (Metronome ↔ Stripe OAuth integration; customer's app emits usage directly).
- **Dashed `stripe listen` box** = local dev only. In production, Stripe POSTs directly to the receiver's public URL.

## Happy-path runtime (one event)

What actually happens when a single `customer.created` fires.

```mermaid
sequenceDiagram
    autonumber
    actor S as Stripe
    participant L as stripe listen
    participant R as Receiver
    participant DB as Postgres
    participant W as Worker
    participant M as Metronome

    S->>L: customer.created (signed)
    L->>R: POST /webhooks/stripe
    R->>R: verify HMAC signature
    R->>DB: INSERT INTO webhook_events<br/>ON CONFLICT DO NOTHING
    R-->>L: 200 OK
    L-->>S: 200 OK

    Note over W,DB: Worker polls every ~2s

    W->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    DB-->>W: row claimed (lock held)
    W->>W: mapper: stripe payload → metronome request
    W->>M: POST /v1/customers
    M-->>W: { data: { id: <metronome_uuid> } }
    W->>DB: INSERT customer_mappings<br/>UPDATE webhook_events SET status='processed'
    DB-->>W: COMMIT (lock released)
```

The receiver-side and worker-side halves are decoupled by the database, so a slow Metronome never slows down webhook ingestion.

## Event lifecycle

Every row in `webhook_events` lives in exactly one of these states. The state machine is the entire retry/failure contract:

```mermaid
stateDiagram-v2
    [*] --> pending: receiver INSERT

    pending --> processed: handler success
    pending --> pending: transient error<br/>(5xx / 429 / network)<br/>attempts++ ; next_attempt_at += backoff
    pending --> failed: permanent error<br/>(4xx / mapper error)
    pending --> failed: attempts >= WORKER_MAX_ATTEMPTS

    processed --> [*]
    failed --> pending: operator replay<br/>(UPDATE SET status='pending', attempts=0)
    failed --> [*]: accepted / triaged
```

- **`pending → processed`**: terminal happy path.
- **`pending → pending`**: transient failure, scheduled retry with exponential backoff + jitter.
- **`pending → failed`**: either a permanent error (no retry will help) or we hit the retry budget.
- **`failed → pending`**: the operator escape hatch. One SQL update puts a row back in the queue:

  ```sql
  UPDATE webhook_events
  SET status='pending', attempts=0, next_attempt_at=NOW(),
      last_error=NULL, processed_at=NULL
  WHERE stripe_event_id = 'evt_...';
  ```

## How it maps to production

| Local dev | Production |
|---|---|
| `uvicorn` in a terminal | Long-running container behind a load balancer with TLS |
| `python -m sidecar.worker` in a terminal | Long-running container, no exposed port; scale to N replicas safely (`SKIP LOCKED`) |
| `stripe listen` tunnel | Gone — replaced by a webhook endpoint registered in Stripe Dashboard → Developers → Webhooks |
| Homebrew Postgres on `:5432` | Managed Postgres (RDS, Cloud SQL, Neon, Supabase, etc.) |
| `alembic upgrade head` in your shell | One-shot job before each release (see the `migrate` service in `docker-compose.yml`) |

Same image, different commands. The Dockerfile in this repo is the production unit; the `docker-compose.yml` shows the topology.
