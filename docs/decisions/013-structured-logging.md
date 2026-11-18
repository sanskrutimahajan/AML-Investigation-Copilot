# ADR 013: Structured, PII-safe API logs

- **Status:** Accepted (2026-11-18). Phase 2 of the production-shape plan ([AUDIT.md](../../AUDIT.md)).
- **Decided by the user (2026-11-18):** structured logs with request IDs; no Langfuse (it would add
  Postgres, ClickHouse, Redis and MinIO to a laptop that already runs Neo4j and the LLM).

## Decisions

1. **JSON lines by default** (`LOG_FORMAT=json`, `text` for local reading), one object per line
   with `ts`, `level`, `logger`, `msg`, `request_id` and a fixed list of extra fields (route,
   method, status, duration, LLM calls, tokens, LLM seconds, outcome). Unknown fields are dropped.
2. **Request IDs.** A well-formed `X-Request-ID` header is kept, otherwise one is generated. It is
   returned in the response and attached to every line written during the request.
3. **The access log never holds names or IDs.** Requests are logged by route template
   (`/subjects/{subject_id}`, `/screen`), never the raw path or query string. Uvicorn's access log
   is off (`make api --no-access-log`).
4. **Redaction as a second line of defence.** A filter masks query strings and anything shaped
   like a customer, account, alert or case ID in every `tracegraph` log message and traceback.
5. **The audit trail is separate.** Analyst actions are recorded in Neo4j (ADR 012), not in logs;
   logs are for operations.

## Evidence

- Tests (`tests/test_observability.py`): a screened name and a subject ID appear in no log line;
  access lines carry the route template, status and the caller's request ID; malformed request IDs
  are replaced; unknown extra fields are dropped.
- Live: `/screen?name=…` and `/subjects/…` against the real API logged only `GET /screen 200` and
  `GET /subjects/{subject_id} 200`, each with a request ID. `make smoke` passed with JSON logs.
- 230 unit tests pass; lint clean.

## Limits

- No distributed tracing; the request ID is the only correlation key.
- Free text that is not shaped like an ID (a question, a name inside an error message from a
  library) is masked only if it follows `?`. The code never logs questions or names itself.
