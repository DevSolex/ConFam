# db

**Responsibility:** All database schema definitions, migrations, and seed data for the ConFam data layer.

See [`docs/DATA_MODEL.md`](../docs/DATA_MODEL.md) for the authoritative entity definitions, field-level notes, the transaction state machine, and the rationale for the `RailEvent` / `LedgerEntry` separation.

## Subdirectories

```
db/
  migrations/   — Ordered migration files, one per schema change (PostgreSQL)
  seeds/        — Development/test seed data only — never real credentials or real transactions
```

## Critical structural rules encoded in schema (not just convention)

These are structural guarantees, not application-level ones. They must survive ORM abstraction:

1. **`LedgerEntry` has no `UPDATE` or `DELETE` path** — append-only is enforced via Postgres `RULE` or row-level security that blocks `UPDATE`/`DELETE` on this table (Engineering Rule 2).
2. **`RailEvent (rail, rail_reference)` is a `UNIQUE` constraint** — the database itself rejects a duplicate confirmation event before the application layer can double-process it (Engineering Rule 3).
3. **Monetary amounts are `BIGINT`** — kobo (smallest naira unit), never `FLOAT` or `NUMERIC` with implicit rounding (Engineering Rule 6).
4. **`PayoutAccount` rows are never deleted or overwritten** — a `superseded_by` foreign key chain preserves full history (Engineering Rule 5, Rule 2).

## Status

**Scaffolded — initial migrations written, no logic.**

## Placeholder note (language/tooling)

The migration syntax here uses raw SQL (PostgreSQL). This is a placeholder choice to keep scaffolding concrete.

<!-- TODO (OPEN_QUESTIONS.md — "Backend language / ORM tooling"): once the backend language is confirmed,
     adopt the project-standard migration tool (e.g., Flyway, Alembic, node-pg-migrate, golang-migrate)
     and convert these files if needed. The schema content stays the same; only the runner changes. -->
