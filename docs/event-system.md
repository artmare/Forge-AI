# Forge event system

Phase 04 turns persistent domain events into reliable internal messages. PostgreSQL is the source of truth; Redis Streams is a replaceable delivery transport. No consumer starts tasks, calls an LLM, or performs autonomous work.

## Transactional outbox

Every publishable mutation writes three kinds of state in one PostgreSQL transaction:

```text
domain rows + events row + event_outbox row -> COMMIT
```

`EventFactory` creates both the event and its outbox entry through the caller's existing SQLAlchemy session. A rollback removes both. API writes do not call Redis, so a Redis outage cannot prevent a company, project, agent, task, or lifecycle transaction from committing.

`event_outbox.status` progresses through `PENDING`, `PROCESSING`, `PUBLISHED`, or `FAILED`. `available_at` controls retry scheduling; `processing_started_at` and `worker_id` form a recoverable lease. Failed publication is retried indefinitely with exponential backoff capped at 60 seconds because dropping an unpublished event would violate the source-of-truth contract.

## Envelope v1

Every Redis message contains a Pydantic-validated envelope:

```json
{
  "event_id": "uuid",
  "event_type": "TASK_STATUS_CHANGED",
  "event_version": 1,
  "occurred_at": "2026-08-20T05:00:00Z",
  "source": "forge-api",
  "company_id": "uuid-or-null",
  "project_id": "uuid-or-null",
  "agent_id": "uuid-or-null",
  "task_id": "uuid-or-null",
  "correlation_id": "uuid",
  "causation_id": "uuid-or-null",
  "payload": {}
}
```

`company_id`, `project_id`, `agent_id`, `task_id`, and `causation_id` are nullable because system and entity-level events do not always have that context. `correlation_id` is always present and defaults to the most specific context ID (task, project, agent, company, then event). Callers can supply correlation and causation IDs when continuing an existing workflow. Consumers accept envelope version 1; unsupported positive versions exhaust the configured retry policy and enter the DLQ rather than being silently misread.

Topics provide a coarse subscription boundary while event types remain specific:

| Prefix | Topic |
| --- | --- |
| `COMPANY_` | `forge.company` |
| `PROJECT_` | `forge.project` |
| `AGENT_` | `forge.agent` |
| `TASK_RUN_` | `forge.task_run` |
| `TASK_` | `forge.task` |
| everything else | `forge.system` |

## Redis Streams and publication

All topics share the `forge.events` Stream. The topic is a message field; Forge does not create a queue per event type. The development Stream is approximately trimmed to 10,000 entries. Production retention should be chosen from measured replay/debug requirements and monitored for memory usage; long-term audit history remains in PostgreSQL.

Publisher instances claim a configurable batch in a short transaction using `SELECT ... FOR UPDATE SKIP LOCKED`, set a lease, and commit before network I/O. They then `XADD` each envelope and update its outbox row in a new transaction. Multiple publishers cannot claim the same live row. A stale `PROCESSING` lease becomes claimable after `EVENT_PROCESSING_LEASE_SECONDS`.

There is necessarily a crash window after `XADD` and before `PUBLISHED` is committed. Recovery may publish that envelope again. Forge therefore guarantees **at-least-once**, not exactly-once, delivery. PostgreSQL prevents lost events; consumer idempotency handles duplicates.

## Consumers, retries, and idempotency

`EventConsumer` supplies envelope deserialization, version validation, optional topic filtering, consumer-group reads, stale pending-message reclaim, acknowledgements, retry, and DLQ behavior. Phase 04 runs two logical groups in one consumer container:

- `forge-audit` proves durable processing and records its result.
- `forge-dashboard` supplies an independently tracked dashboard subscriber; the live SSE path also tails the Stream without taking messages from either group.

Each attempt is recorded in `event_consumptions`. The unique `(event_id, consumer)` constraint is the durable idempotency key, and a row-level processing lease prevents concurrent duplicate deliveries from entering the same handler. A duplicate delivery whose record is already `SUCCEEDED` or actively leased is acknowledged without running its handler again; a stale lease is recoverable after the configured timeout. Redis message IDs are transport metadata, not the idempotency identity.

Handlers retry up to `EVENT_MAX_RETRIES` with exponential delay based on `EVENT_RETRY_BASE_SECONDS`. An exhausted message is appended to `forge.events.dlq`, persisted in `event_dead_letters`, marked `FAILED`, and acknowledged from its source group. If writing the DLQ itself fails, the source message is not acknowledged and remains recoverable.

## Replay

`POST /api/v1/events/{event_id}/replay` creates another `PENDING` outbox delivery that references `replay_of_outbox_id`. It preserves the original event ID, envelope, topic, and occurrence time; it does not invent a new domain event. Consumers that already succeeded ignore the duplicate through their idempotency record. This makes replay useful for a repaired/new consumer without applying an existing side effect twice.

## APIs and realtime path

- `GET /api/v1/events` returns newest-first persistent events with contextual, type, topic, correlation, date, offset, and limit filters.
- `GET /api/v1/events/{event_id}` includes envelope fields, metadata, all publication attempts, and consumer states.
- `GET /api/v1/events/stats` reports outbox counts, DLQ count, and publisher heartbeat health.
- `GET /api/v1/events/dlq` supports consumer/type filters and pagination.
- `POST /api/v1/events/{event_id}/replay` schedules an administrative redelivery.
- `GET /api/v1/events/stream` emits SSE records from Redis and honors `Last-Event-ID` for reconnects.

The browser connects to the same-origin Next.js `/api/events/stream` proxy. FastAPI tails Redis with `XREAD`; Next.js forwards the response body without buffering; the dashboard de-duplicates by event ID and prepends live events to its recent persistent-event snapshot. SSE heartbeats keep idle connections open, and browser `EventSource` reconnects using the last Stream ID.

## Outage and recovery behavior

- **Redis down:** domain transactions still commit. Outbox rows remain `PENDING` until claimed or become `FAILED` with a future `available_at`; publication resumes after Redis returns.
- **Publisher crash before Redis:** the lease expires and another publisher reclaims the row.
- **Publisher crash after Redis:** the row may be republished after lease expiry; this is the documented at-least-once duplicate window.
- **Consumer crash:** Redis keeps the unacknowledged group entry. `XAUTOCLAIM` transfers stale work to a live consumer.
- **PostgreSQL transaction failure:** business state, event, and outbox all roll back together.

The API health endpoint reports PostgreSQL and Redis reachability. Redis degradation can make `/health` unhealthy, but it does not disable domain write endpoints. Event stats separately report a recent publisher heartbeat.

## Configuration

| Variable | Default |
| --- | --- |
| `EVENT_STREAM_NAME` | `forge.events` |
| `EVENT_DLQ_STREAM_NAME` | `forge.events.dlq` |
| `EVENT_PUBLISH_BATCH_SIZE` | `50` |
| `EVENT_PUBLISH_INTERVAL_MS` | `500` |
| `EVENT_PROCESSING_LEASE_SECONDS` | `30` |
| `EVENT_MAX_RETRIES` | `5` |
| `EVENT_RETRY_BASE_SECONDS` | `1.0` |
| `EVENT_CONSUMER_BLOCK_MS` | `1000` |

## Security and data hygiene

Event routes are grouped separately so a future authentication/authorization dependency can protect them without changing domain routes. Phase 04 assumes a local/internal deployment and does not yet add authentication. Payloads must contain identifiers and operational metadata only. Never put credentials, passwords, API keys, access tokens, session tokens, or raw secrets in an event. Internal event data should be treated as sensitive and should not be exposed through an unrestricted public deployment.

## Current limits

Phase 04 has one Stream and two proof consumers, a fixed common envelope version, local Compose workers, approximate count-based retention, and no UI controls for DLQ/replay. It does not provide exactly-once delivery, cross-region failover, production authentication, payload schema registry, event-specific schemas beyond the common envelope, or autonomous event-driven behavior.
