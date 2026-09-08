# Repo-layer recon (STORY-1)

Read-only investigation that records the verified facts every later
story in this slice can rely on, instead of guessing. Every section
below cites a concrete file path and quotes the relevant snippet or
grep output verbatim. Each section ends with a one-line **Decision:**
statement applying the PRD's rule for that item.

Production code under `src/` and `scripts/` is not changed by this
story — `git status` is empty after this file lands.

---

## V-pre: c01 test file & how those tests obtain an Infrastructure

**Test file:** `tests/components/test_user_portfolio.py` (the only
existing test file for component 01 / `c01_user_portfolio.py`).

**How tests obtain an Infrastructure:** the file defines its own
private in-memory double, `_InMemoryInfrastructure`, declared in the
same file as the tests themselves. It is **not** a fixture from
`conftest.py`, **not** a real Postgres connection, and **not** a
re-export of `DefaultInfrastructure`. A new instance is created
inline inside each test (`_InMemoryInfrastructure()`) and injected
into `DefaultUserPortfolio(infrastructure=infra, ...)`.

Verbatim header from `tests/components/test_user_portfolio.py`:

```python
"""Tests for DefaultUserPortfolio, BrokerConnector, and
PlaceholderBrokerConnector (src/components/c01_user_portfolio.py).

Uses an in-memory Infrastructure test double rather than a live
Postgres/Redis connection, so these tests run unconditionally (unlike
tests/test_infrastructure_postgres.py, which needs a live service and
skips cleanly without one) — DefaultUserPortfolio's own logic (real
persistence calls, real provenance tagging, real exposure math, real
relevance lookups) is what's under test here, not DefaultInfrastructure
itself, which already has its own dedicated test suite.
"""
```

Verbatim class declaration from `tests/components/test_user_portfolio.py`:

```python
class _InMemoryInfrastructure:
    """Minimal Infrastructure test double. store/retrieve/query mirror
    DefaultInfrastructure's real semantics closely enough for this
    component's tests: store() upserts by record["id"] (or a generated
    id), retrieve() looks up by id, query() returns records that
    contain every key/value in the filter dict (the same containment
    match DefaultInfrastructure.query documents for its JSONB `@>`
    operator). publish/subscribe/schedule/cache_get/cache_set/get_secret
    are unused by DefaultUserPortfolio and are not implemented."""
```

The double's body implements only `store` / `retrieve` / `query`:

```python
    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def store(self, table: str, record: dict) -> str:
        self._next_id += 1
        record_id = str(record["id"]) if "id" in record else f"generated-{self._next_id}"
        self._tables.setdefault(table, {})[record_id] = dict(record, id=record_id)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        return self._tables.get(table, {}).get(id_)

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            record
            for record in self._tables.get(table, {}).values()
            if all(record.get(key) == value for key, value in filters.items())
        ]
```

So: tests obtain an Infrastructure by **instantiating a private
`_InMemoryInfrastructure` test double defined inside the same test
file**. There is no `conftest.py` fixture and no real Postgres.

**Decision:** later stories that need an Infrastructure for c01 tests
should reuse `_InMemoryInfrastructure` (or extract it into a
shared `conftest.py` fixture) rather than reach for `DefaultInfrastructure`,
because `tests/components/test_user_portfolio.py` already establishes
that pattern and tests run unconditionally under it.

---

## V1: Exact signatures of `store`, `retrieve`, `query`

**All three methods are synchronous (`def`, NOT `async def`).** No
method in the `Infrastructure` Protocol, in `StubInfrastructure`,
or in `DefaultInfrastructure` carries an `async` keyword.

### Protocol declarations — `src/infrastructure.py` lines 26–36

```python
    def store(self, table: str, record: dict) -> str:
        """Write a record. Returns its id."""
        ...

    def retrieve(self, table: str, id_: str) -> dict | None:
        """Read a record by id."""
        ...

    def query(self, table: str, filters: dict) -> list[dict]:
        """Read records matching filters."""
        ...
```

### Concrete stub — `src/infrastructure.py` lines 68–78 (`StubInfrastructure`)

```python
    def store(self, table: str, record: dict) -> str:
        with traced("StubInfrastructure.store"):
            return ""

    def retrieve(self, table: str, id_: str) -> dict | None:
        with traced("StubInfrastructure.retrieve"):
            return None

    def query(self, table: str, filters: dict) -> list[dict]:
        with traced("StubInfrastructure.query"):
            return []
```

### Concrete default — `src/infrastructure_postgres.py` lines 142–179 (`DefaultInfrastructure`)

```python
    def store(self, table: str, record: dict) -> str:
        """Writes `record` as JSONB. Uses `record["id"]` as the row id
        when present (so callers can control identity), otherwise
        generates a uuid4 — the Infrastructure protocol requires this
        to return an id but doesn't say where it comes from, and
        record dicts aren't guaranteed to carry one."""
        with traced("DefaultInfrastructure.store"):
            record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO records (table_name, id, data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (table_name, id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (table, record_id, Jsonb(record)),
                )
            return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        with traced("DefaultInfrastructure.retrieve"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "SELECT data FROM records WHERE table_name = %s AND id = %s",
                    (table, id_),
                )
                row = cursor.fetchone()
            return row[0] if row is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        """JSONB containment match only (`data @> filters`) — not a
        general query DSL, deliberately kept simple for this first
        version."""
        with traced("DefaultInfrastructure.query"):
            with self._connection().cursor() as cursor:
                cursor.execute(
                    "SELECT data FROM records WHERE table_name = %s AND data @> %s",
                    (table, Jsonb(filters)),
                )
                rows = cursor.fetchall()
            return [row[0] for row in rows]
```

Facts about all three (matching across Protocol, Stub, and
Default):
* Parameter names/order is identical across all three:
  `store(self, table, record)`, `retrieve(self, table, id_)`,
  `query(self, table, filters)`.
* Return types are identical across all three:
  `store → str`, `retrieve → dict | None`, `query → list[dict]`.
* `query` takes a **filter dict** (not raw SQL). `DefaultInfrastructure.query`
  implements this with `data @> %s` (JSONB containment).
* None of the three methods has an `order_by` / `limit` / `offset`
  parameter — **no ordering or limit support** anywhere in the
  Protocol, the Stub, or the Default.
* All three methods are **synchronous** (`def`, not `async def`),
  in the Protocol, the Stub, *and* the Default.

**Decision:** later stories must call `store`/`retrieve`/`query`
synchronously, pass a dict (never SQL) to `query`, and accept that
the Protocol does not expose ordering/limits — any ordering or
pagination requirement is out of scope for a `query` call and must
be filtered client-side.

---

## V2: Existing in-memory / fake Infrastructure

**Explicit answer: YES, an existing in-memory / fake Infrastructure
implementation was found.** It is the private class
`_InMemoryInfrastructure` at file path
**`tests/components/test_user_portfolio.py`** (defined inside the
test file itself, not in a separate `conftest.py`). See V-pre for
the verbatim class declaration; the V-pre snippet already shows
the full implementation, so this section only enumerates which
Protocol methods it covers.

Coverage of the Protocol methods `_InMemoryInfrastructure`
implements (full coverage table for every method listed on the
`Infrastructure` Protocol — answer: 3 of 9):

| Protocol method | Implemented? |
|---|---|
| `store(table, record) -> str` | **Yes** — upserts by `record["id"]` or generates `"generated-<n>"`. |
| `retrieve(table, id_) -> dict \| None` | **Yes** — dict lookup. |
| `query(table, filters) -> list[dict]` | **Yes** — returns records where every `key=value` in `filters` is contained. |
| `publish(topic, event)` | **No** — not declared. |
| `subscribe(topic, handler)` | **No** — not declared. |
| `schedule(delay_seconds, task)` | **No** — not declared. |
| `cache_get(key)` | **No** — not declared. |
| `cache_set(key, value, ttl_seconds)` | **No** — not declared. |
| `get_secret(name)` | **No** — not declared. |

The double's docstring is explicit: *"publish/subscribe/schedule/
cache_get/cache_set/get_secret are unused by DefaultUserPortfolio
and are not implemented."*

There is also `StubInfrastructure` in `src/infrastructure.py` — a
*structural* no-op stub (every method returns the empty shape:
`""`, `None`, `[]`) used in tracing-only tests. It implements **all
nine** Protocol methods, but is not a usable fake for any test that
needs to assert on stored data; it is a structural placeholder so
`isinstance(x, Infrastructure)` succeeds.

There is no other in-memory / fake Infrastructure in `src/` or
`tests/`.

**Decision:** later stories may use `_InMemoryInfrastructure`
(from `tests/components/test_user_portfolio.py`) for c01 tests, and
may use `StubInfrastructure` only when the test only needs
`isinstance` conformance — never when stored data needs to be
asserted against.

---

## V3: `DefaultInfrastructure.store` semantics + lazy `CREATE TABLE IF NOT EXISTS` DDL

From `src/infrastructure_postgres.py`:

`store` (lines 117–129):

```python
    def store(self, table: str, record: dict) -> str:
        """Writes `record` as JSONB. Uses `record["id"]` as the row id
        when present (so callers can control identity), otherwise
        generates a uuid4 — the Infrastructure protocol requires this
        to return an id but doesn't say where it comes from, and
        record dicts aren't guaranteed to carry one."""
        with traced("DefaultInfrastructure.store"):
            record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
            with self._connection().cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO records (table_name, id, data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (table_name, id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (table, record_id, Jsonb(record)),
                )
            return record_id
```

So `store` is **upsert-by-id** (not insert-only): `INSERT ... ON
CONFLICT (table_name, id) DO UPDATE SET data = EXCLUDED.data`.
Returns the record id it used (`record["id"]` if present, else a
fresh `uuid4()`). It does **not** return the row id for new rows
from the database (no `RETURNING`).

Lazy `CREATE TABLE IF NOT EXISTS` DDL — emitted from
`_ensure_schema` on first connection (`src/infrastructure_postgres.py`
lines 65–108):

```sql
                CREATE TABLE IF NOT EXISTS records (
                    table_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (table_name, id)
                )
```

```sql
                CREATE TABLE IF NOT EXISTS queue_events (
                    id SERIAL PRIMARY KEY,
                    topic TEXT NOT NULL,
                    event JSONB NOT NULL,
                    published_at TIMESTAMPTZ DEFAULT now(),
                    consumed BOOLEAN DEFAULT false
                )
```

```sql
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id SERIAL PRIMARY KEY,
                    run_at TIMESTAMPTZ NOT NULL,
                    task JSONB NOT NULL,
                    executed BOOLEAN DEFAULT false
                )
```

```sql
                CREATE TABLE IF NOT EXISTS migration_log (
                    id BIGSERIAL PRIMARY KEY,
                    migration_name VARCHAR NOT NULL,
                    run_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    rows_affected BIGINT,
                    error_message TEXT,
                    dry_run BOOLEAN NOT NULL
                )
```

```sql
                CREATE INDEX IF NOT EXISTS idx_migration_log_name_run ON migration_log (migration_name, run_at)
```

```sql
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id BIGSERIAL PRIMARY KEY,
                    migration_name VARCHAR NOT NULL UNIQUE,
                    applied_at TIMESTAMPTZ NOT NULL
                )
```

Facts about the four user-portfolio "tables" (`users`, `portfolios`,
`holdings`, `transactions`):

* They are **not** first-class DDL in `DefaultInfrastructure`.
* Each row is stored as one opaque JSONB blob in the single
  `records` table, addressed by `(table_name, id)`. The opaque
  payload column is named **`data JSONB`**.
* `records` carries a **`created_at TIMESTAMPTZ DEFAULT now()`**
  timestamp column. There are **no** `updated_at` /
  `modified_at` columns anywhere in the schema.
* The primary key is composite `(table_name, id)` — `id` is `TEXT`,
  not `UUID`, and the caller chooses it (or `store` generates a
  uuid4 for it).
* There are **no** per-table child FK columns in `records` (no
  `user_id` / `portfolio_id` indexes, no foreign keys). Parent /
  child relationships are enforced by application code only — see
  V6 for the orphan-row risk this creates.

**Decision:** because `store` is **upsert-by-id**, a migrated-table
path that re-inserts the same row can simply use the existing
`store(...)` API — no separate `INSERT ... ON CONFLICT` boilerplate
is needed. The "tables" `users`/`portfolios`/`holdings`/
`transactions` are not real DDL; the schema is one generic `records`
table with `data JSONB` and a `created_at` timestamp.

---

## V4: Dataclasses `User`, `Portfolio`, `Holding`, `Transaction` — fields, annotations, defaults

From `src/components/c01_user_portfolio.py` (verbatim, including
line numbers):

### `User` — lines 370–375

```python
@dataclass
class User:
    id: str
    preferences: dict = field(default_factory=dict)
    email: str = ""
```

Field list, annotation, default:
| Field | Annotation | Default |
|---|---|---|
| `id` | `str` | (required positional) |
| `preferences` | `dict` | `field(default_factory=dict)` |
| `email` | `str` | `""` |

### `Portfolio` — lines 377–380

```python
@dataclass
class Portfolio:
    id: str
    user_id: str
```

Field list, annotation, default:
| Field | Annotation | Default |
|---|---|---|
| `id` | `str` | (required positional) |
| `user_id` | `str` | (required positional) |

### `Holding` — lines 501–507

```python
@dataclass
class Holding:
    portfolio_id: str
    security_id: str
    quantity: Decimal
    currency: str = "USD"
    exchange: str | None = None
    symbol_suffix: str | None = None
```

Field list, annotation, default:
| Field | Annotation | Default |
|---|---|---|
| `portfolio_id` | `str` | (required positional) |
| `security_id` | `str` | (required positional) |
| `quantity` | `Decimal` | (required positional) |
| `currency` | `str` | `"USD"` |
| `exchange` | `str \| None` | `None` |
| `symbol_suffix` | `str \| None` | `None` |

`Holding.__post_init__` then coerces `self.quantity` through
`_coerce_quantity_to_decimal` (a `Decimal` quantized to 4 decimal
places — `Decimal("0.0001")`), and mutates `self.exchange` and
`self.currency` from `symbol_suffix` rules. None of those mutations
add an `id` field.

### `Transaction` — lines 586–589

```python
@dataclass
class Transaction:
    portfolio_id: str
    kind: str
    amount: float
```

Field list, annotation, default:
| Field | Annotation | Default |
|---|---|---|
| `portfolio_id` | `str` | (required positional) |
| `kind` | `str` | (required positional) |
| `amount` | `float` | (required positional) |

### Explicit yes/no on `id` field (per PRD's V4 acceptance criterion)

* **`Holding` has an `id` field: NO.** Its fields are exactly
  `portfolio_id`, `security_id`, `quantity`, `currency`, `exchange`,
  `symbol_suffix` — six fields total, none of them named `id`.
* **`Transaction` has an `id` field: NO.** Its fields are exactly
  `portfolio_id`, `kind`, `amount` — three fields total, none of
  them named `id`.

(For comparison: `User` *does* have an `id: str` field, and
`Portfolio` *does* have an `id: str` field. Only `Holding` and
`Transaction` lack one.)

### `Transaction.kind`

Annotated as plain **`str`** (not `Enum`, not `Literal`). The only
constraint in the codebase is at the call site:
`DefaultUserPortfolio.import_transactions` builds
`Transaction(..., kind=tagged["side"], ...)` where
`tagged["side"]` comes from `BrokerTransaction.side` — a
`Literal['BUY', 'SELL']` on the broker DTO. So the *broker* side
constrains the string to `'BUY'` / `'SELL'`, but the dataclass
itself accepts any `str`.

### Annotated types of `quantity` and `amount`

* `Holding.quantity: Decimal` (further coerced/quantized to
  `Decimal("0.0001")` in `__post_init__`).
* `Transaction.amount: float` — plain `float`, **not** `Decimal`.

**Decision:** later stories must (a) build the `data` JSONB payload
*without* relying on a dataclass `id` for `Holding` / `Transaction`
(because they don't have one) and (b) synthesize the row id at the
`store(...)` call site (the existing code does this — e.g.
`import_holdings` uses `f"{portfolio.id}:{holding.security_id}"`,
`import_transactions` uses `str(uuid.uuid4())`).

---

## V5: Call sites of the four table-name constants and bare literals

### Constants: `USERS_TABLE`, `PORTFOLIOS_TABLE`, `HOLDINGS_TABLE`, `TRANSACTIONS_TABLE`

Full `grep -rn` output (`grep -rn "USERS_TABLE\|PORTFOLIOS_TABLE\|HOLDINGS_TABLE\|TRANSACTIONS_TABLE" src/ tests/`):

```
src/components/c01_user_portfolio.py:675:USERS_TABLE = "users"
src/components/c01_user_portfolio.py:676:PORTFOLIOS_TABLE = "portfolios"
src/components/c01_user_portfolio.py:677:HOLDINGS_TABLE = "holdings"
src/components/c01_user_portfolio.py:678:TRANSACTIONS_TABLE = "transactions"
src/components/c01_user_portfolio.py:745:                USERS_TABLE, {"id": user.id, "preferences": user.preferences, "email": user.email}
src/components/c01_user_portfolio.py:756:                PORTFOLIOS_TABLE,
src/components/c01_user_portfolio.py:789:                    HOLDINGS_TABLE,
src/components/c01_user_portfolio.py:815:                    TRANSACTIONS_TABLE,
src/components/c01_user_portfolio.py:1062:            stored = self._infrastructure.retrieve(USERS_TABLE, user.id)
src/components/c01_user_portfolio.py:1072:                USERS_TABLE, {"id": user.id, "preferences": current_preferences, "email": email}
src/components/c01_user_portfolio.py:1089:            for portfolio_record in self._infrastructure.query(PORTFOLIOS_TABLE, {"user_id": user.id}):
src/components/c01_user_portfolio.py:1091:                    HOLDINGS_TABLE, {"portfolio_id": portfolio_record["id"]}
src/components/c01_user_portfolio.py:1143:                HOLDINGS_TABLE,
src/components/c01_user_portfolio.py:1160:                TRANSACTIONS_TABLE,
src/components/c01_user_portfolio.py:1166:        records = self._infrastructure.query(HOLDINGS_TABLE, {"portfolio_id": portfolio_id})
src/components/c01_user_portfolio.py:1184:        stored = self._infrastructure.retrieve(PORTFOLIOS_TABLE, portfolio.id)
tests/test_detect_delisted_stocks.py:224:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:255:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:284:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:331:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:367:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:390:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:423:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:456:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:499:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:536:    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)
tests/test_detect_delisted_stocks.py:684:    `monkeypatch`es of `fetch_yahoo_finance_quote` / `_HOLDINGS_TABLE`
```

Notes from this grep:
* All four constants are **defined** in `src/components/c01_user_portfolio.py`
  (lines 675–678).
* All in-`src/` *call sites* of the four constants are inside
  `src/components/c01_user_portfolio.py` only. **No other `src/`
  module references them.**
* Outside `c01`, the only references are in
  `tests/test_detect_delisted_stocks.py` — and these are **not**
  references to c01's `HOLDINGS_TABLE` constant: that test file
  monkeypatches a *different* attribute named `_HOLDINGS_TABLE` on
  the `detect_delisted_stocks` module (an internal name inside
  `scripts/detect_delisted_stocks.py`, not the c01 public constant).
* No test asserts exact-dict equality on `retrieve(...)` for any of
  the four tables. (`tests/components/test_user_portfolio.py` uses
  `infra.retrieve("users", user.id)` and reads individual fields
  off the returned dict — `stored["email"]`, `stored["preferences"]`,
  etc. — but never `assert stored == {...}` against a literal.)
* Code path that creates a child row whose parent does not exist:
  `import_holdings` and `import_transactions` create rows in
  `HOLDINGS_TABLE` / `TRANSACTIONS_TABLE` only after `connect_portfolio`
  has stored the parent in `PORTFOLIOS_TABLE`, so that flow is
  parent-then-child. But `tests/components/test_user_portfolio.py`
  does create child rows directly through the `_InMemoryInfrastructure`
  (e.g. line 366–367:
  `infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})`
  followed by
  `infra.store("holdings", {"id": "pf-1:AAPL", "portfolio_id": "pf-1", "security_id": "AAPL", "quantity": 1.0})`),
  and the *production* path `add_holding_manually` /
  `add_transaction_manually` will likewise persist children against
  a `portfolio_id` that came from the caller, not from a verified
  portfolio row — there is no FK or pre-check in the storage layer.

### Bare string literals: `"users"`, `"portfolios"`, `"holdings"`, `"transactions"`

Full `grep -rn` output
(`grep -rn -E '"users"|"portfolios"|"holdings"|"transactions"' src/ tests/`):

```
tests/test_infrastructure_postgres.py:403:    infra.store("holdings", {"id": "h2", "security_id": "MSFT", "portfolio_id": "p1"})
tests/test_infrastructure_postgres.py:404:    infra.store("holdings", {"id": "h3", "security_id": "TSLA", "portfolio_id": "p1"})
tests/test_infrastructure_postgres.py:405:    infra.store("holdings", {"id": "h4", "security_id": "XYZ", "portfolio_id": "p1"})  # not in CSV
tests/test_backfill_holdings_currency.py:133:            [("holdings", h_id, psycopg.types.json.Jsonb(data)) for h_id, data in holdings],
tests/test_verify_holdings_currency_backfill.py:127:            [("holdings", h_id, psycopg.types.json.Jsonb(data)) for h_id, data in holdings],
tests/components/test_decision_policy.py:133:    infra.store("holdings", {"id": "h1", "portfolio_id": portfolio.id, "security_id": "AAPL", "quantity": 5.0})
tests/components/test_user_portfolio.py:111:    stored = infra.retrieve("users", user.id)
tests/components/test_user_portfolio.py:131:    assert infra.retrieve("users", user.id)["email"] == "real@example.com"
tests/components/test_user_portfolio.py:150:    assert infra.retrieve("users", user.id)["email"] == "real@example.com"
tests/components/test_user_portfolio.py:164:    assert infra.retrieve("users", user.id)["preferences"] == {
tests/components/test_user_portfolio.py:194:    stored = infra.retrieve("portfolios", portfolio.id)
tests/components/test_user_portfolio.py:255:    stored = infra.retrieve("holdings", "pf-1:AAPL")
tests/components/test_user_portfolio.py:270:    stored_records = infra.query("transactions", {"portfolio_id": "pf-1"})
tests/components/test_user_portfolio.py:330:    infra.store("holdings", {"id": "pf-3:AAPL", "portfolio_id": "pf-3", "security_id": "AAPL", "quantity": 6.0})
tests/components/test_user_portfolio.py:331:    infra.store("holdings", {"id": "pf-3:GOOG", "portfolio_id": "pf-3", "security_id": "GOOG", "quantity": 4.0})
tests/components/test_user_portfolio.py:366:    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
tests/components/test_user_portfolio.py:367:    infra.store("holdings", {"id": "pf-1:AAPL", "portfolio_id": "pf-1", "security_id": "AAPL", "quantity": 1.0})
tests/components/test_user_portfolio.py:376:    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
tests/components/test_user_portfolio.py:377:    infra.store("holdings", {"id": "pf-1:AAPL", "portfolio_id": "pf-1", "security_id": "AAPL", "quantity": 1.0})
tests/components/test_user_portfolio.py:467:    stored = infra.retrieve("holdings", f"pf-1:{apple.id}")
tests/components/test_user_portfolio.py:484:    stored = infra.retrieve("holdings", f"pf-1:{apple.id}")
tests/components/test_user_portfolio.py:497:    assert infra.query("holdings", {"portfolio_id": "pf-1"}) == []
tests/components/test_user_portfolio.py:508:    stored_records = infra.query("transactions", {"portfolio_id": "pf-1"})
tests/components/test_user_portfolio.py:540:    infra.store("portfolios", {"id": "pf-1", "user_id": "user-1"})
tests/components/test_user_portfolio.py:581:    assert infra.retrieve("holdings", "pf-1:AAPL")["provenance"] == Provenance.UNTRUSTED.name
tests/test_story2_holdings_currency_backfill_ci.py:422:                    ("holdings", f"{_TEST_HOLDING_PREFIX}-AAPL", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 10})),
tests/test_story2_holdings_currency_backfill_ci.py:423:                    ("holdings", f"{_TEST_HOLDING_PREFIX}-MSFT", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 5})),
tests/test_story2_holdings_currency_backfill_ci.py:424:                    ("holdings", f"{_TEST_HOLDING_PREFIX}-GOOGL", psycopg.types.json.Jsonb({"currency": "USD", "quantity": 3})),
tests/test_story2_holdings_currency_backfill_ci.py:459:                ("holdings", f"{_TEST_HOLDING_PREFIX}-BAD",
tests/test_story2_holdings_currency_backfill_ci.py:510:                ("holdings", f"{_TEST_HOLDING_PREFIX}-PROC-PASS",
tests/test_story2_holdings_currency_backfill_ci.py:558:                ("holdings", f"{_TEST_HOLDING_PREFIX}-PROC-FAIL",
```

Notes:
* `src/` contains **zero** bare-literal call sites for these four
  table names. Every production call site in `src/` uses the
  `USERS_TABLE` / `PORTFOLIOS_TABLE` / `HOLDINGS_TABLE` /
  `TRANSACTIONS_TABLE` constants from `c01_user_portfolio.py`.
* Every bare-literal match is in `tests/`. The most frequent
  pattern: `tests/components/test_user_portfolio.py` passes the
  bare string to `_InMemoryInfrastructure.store/retrieve/query`
  (e.g. `infra.retrieve("users", user.id)`).
* **No test asserts exact-dict equality on `retrieve(...)` for any
  of the four tables** (`users`, `portfolios`, `holdings`,
  `transactions`). Every assertion in the grep output reads
  individual keys off the returned dict (e.g. `stored["email"]`,
  `stored["preferences"]`, `stored["provenance"]`,
  `stored["security_id"]`, `stored["quantity"]`,
  `stored["broker_connection"]["provenance"]`) — none of the matches
  in the grep output above is `assert ... == {...}` against a
  literal dict retrieved from one of these four tables.
* Orphan-row creation: tests directly `infra.store("holdings", ...)`
  with a `portfolio_id` that is never verified to exist in the
  `portfolios` table (e.g. `tests/components/test_user_portfolio.py`
  lines 366–367, 376–377, etc.). This is a deliberate test setup
  shortcut, not a production invariant.

**Decision:** later stories should (a) keep `src/` call sites on the
c01 constants — never import the bare strings — and (b) accept that
neither the storage layer nor any caller currently enforces parent-
before-child for `holdings`/`transactions` vs `portfolios`; the
storage layer is JSONB-only with no FKs.

---

## V6: Existing `us_stock` migration — file path, scheme, discovery, verify invocation, rollback

**Explicit summary up front:**

| File / item | Exact path |
|---|---|
| Existing US-stock migration SQL | `scripts/migrate_us_stocks.sql` |
| Migration name (logical version) | `us_stock_portfolio_defaults_v1` (constant `MIGRATION_NAME` in `scripts/migrate_us_stocks.py`) |
| Python wrapper around that SQL | `scripts/migrate_us_stocks.py` |
| Shell wrapper around that SQL | `scripts/run_migration.sh` |
| Verify SQL | `scripts/verify_migration.sql` |
| Verify Python | `scripts/verify_migration.py` |
| Operational runbook | `docs/migrations/us_stock_migration.md` |
| **Down / rollback file** | **NONE — no rollback file exists.** No `migrate_us_stocks.down.sql`, no `*_rollback.sql`, no `*_down.sql`, no `rollback_*` file of any kind in `scripts/`, `docs/migrations/`, or anywhere else in this repo. |

### Path and filename

* **SQL file:** `scripts/migrate_us_stocks.sql`.
* **Python wrapper:** `scripts/migrate_us_stocks.py`.
* **Shell wrapper:** `scripts/run_migration.sh`.
* **Verify SQL:** `scripts/verify_migration.sql`.
* **Verify Python:** `scripts/verify_migration.py`.
* **Migration name (the contract `verify_migration.py` keys off):**
  `MIGRATION_NAME = "us_stock_portfolio_defaults_v1"` (in
  `scripts/migrate_us_stocks.py`).

### Filename / version scheme

The filename is the migration's logical name in plain English with
underscores: `migrate_us_stocks.sql`. There is no timestamp prefix,
no numeric version prefix, no `V<NN>__`/`U<NN>__`/Liquibase/-
Flyway-style scheme in this repo. The version identifier lives in
the `MIGRATION_NAME` constant inside the Python wrapper
(`"us_stock_portfolio_defaults_v1"`).

Verbatim header of `scripts/migrate_us_stocks.sql`:

```
-- migrate_us_stocks.sql
--
-- Skeleton migration script for backfilling/normalizing US stock tickers
-- (e.g. exchange, currency, country) into the user_portfolio / stocks
-- tables. The structure here defines the contract the wrapper
-- (scripts/run_migration.sh) and downstream stories rely on.
--
-- Conventions:
--   * Idempotent where possible (safe to re-run).
--   * Runs inside a single transaction; the wrapper captures psql's
--     real exit status and the whole migration aborts on the first
--     statement failure (psql default with -v ON_ERROR_STOP=1 is set
--     by run_migration.sh).
--   * Temporary working tables live in pg_temp so they don't leak.
```

### How `scripts/run_migration.sh` discovers and applies files

It does **not** discover files. `run_migration.sh` hard-codes a
sibling-path lookup for one specific file (verbatim):

```bash
SQL_SCRIPT="${SCRIPT_DIR}/migrate_us_stocks.sql"
```

…and then invokes `psql` against that single file:

```bash
psql \
    "${DATABASE_URL}" \
    -v ON_ERROR_STOP=1 \
    --no-psqlrc \
    ${DRY_RUN_FLAG} \
    -f "${SQL_SCRIPT}" 2>&1 | tee "${LOG_FILE}" || PSQL_EXIT=$?
```

The wrapper does not iterate a `migrations/` directory; it is a
one-shot wrapper for one file.

### How `scripts/verify_migration.py` is invoked

* **CLI:** `python -m scripts.verify_migration` (or `python
  scripts/verify_migration.py` directly — `__main__` block calls
  `sys.exit(verify_migration())`).
* **Exit codes (verbatim from the module docstring):**

  ```
  0  All checks pass:
       (a) every row in `stocks` has currency='USD', exchange IS NULL,
           and symbol_suffix IS NULL;
       (b) migration_log has at least one row with
           migration_name='us_stock_portfolio_defaults_v1' and
           status='SUCCESS'.
  1  At least one check failed. Summary counts are always printed so
     ops/QA can see exactly which one.
  ```

* **Output format:** five lines always printed to stdout:

  ```
  stocks.total_rows     = <N>
  stocks.bad_currency   = <N>
  stocks.bad_exchange   = <N>
  stocks.bad_suffix     = <N>
  migration_log.success = 1|0
  ```

  Failures are written to stderr as `FAIL: <reason>` lines.

### Down / rollback file

**There is no separate down/rollback file in this repo.** No
`migrate_us_stocks.down.sql`, no `rollback_us_stocks.sql`, no
`*_rollback.*` and no `*_down.*`. `docs/migrations/us_stock_migration.md`
is explicit: *"There is no separate down-migration script in this
repo — do not invent one."* The rollback model is operational
(snapshot + manual corrective `UPDATE` whose `migration_name` is a
distinct string), not a checked-in SQL file.

**Decision:** later stories may add new migrations **next to**
`scripts/migrate_us_stocks.sql` (same directory, same naming
convention), but must update `run_migration.sh`'s hard-coded
`SQL_SCRIPT` if they want the shell wrapper to pick them up — the
shell wrapper does **not** glob-discover. Down-migrations, if
needed, must be authored by hand per the runbook and must not
introduce a `*_down.sql` file (the runbook explicitly forbids it).

---

## V7: Postgres / Redis env vars

**Explicit answer on env-var names used by `DefaultInfrastructure`
vs `run_migration.sh`:**

* **`DefaultInfrastructure` does NOT read any env var for Postgres
  or Redis.** The DSN / URL are constructor arguments; the
  hard-coded fallback defaults are the two module-level constants
  `DEFAULT_POSTGRES_DSN` and `DEFAULT_REDIS_URL` shown below.
  `DefaultInfrastructure` only reads the process environment from
  inside `get_secret(name)`, and there it reads whatever env var
  name the *caller* passes (a cloud-secret-manager placeholder, not
  Postgres- or Redis-specific).
* **`scripts/run_migration.sh` reads three env vars** (one required,
  two optional): `DATABASE_URL` (required), `MIGRATION_DRY_RUN`
  (optional), `LOG_FILE` (optional). None of them is a Postgres or
  Redis env var by name; `DATABASE_URL` is the Postgres DSN and the
  wrapper does not touch Redis at all.

### Verbatim details

#### `DefaultInfrastructure` (`src/infrastructure_postgres.py`)

### `DefaultInfrastructure` (`src/infrastructure_postgres.py`)

The class accepts DSNs through its constructor (lines 27–34):

```python
    def __init__(
        self,
        postgres_dsn: str = DEFAULT_POSTGRES_DSN,
        redis_url: str = DEFAULT_REDIS_URL,
    ) -> None:
        self._postgres_dsn = postgres_dsn
        self._redis_url = redis_url
```

The defaults (lines 22–23):

```python
DEFAULT_POSTGRES_DSN = "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
```

There is **no environment-variable read** for the Postgres DSN or
the Redis URL inside `DefaultInfrastructure`. The only env var it
ever reads is inside `get_secret`:

```python
    def get_secret(self, name: str) -> str:
        """Local-dev placeholder: reads directly from the process
        environment. ADR-0019 specifies the cloud provider's secret
        manager for production; real cloud secret manager integration
        is not implemented here — that's out of scope for this pass.
        Raises KeyError if `name` isn't set, same as `os.environ[name]`."""
        with traced("DefaultInfrastructure.get_secret"):
            return os.environ[name]
```

So `DefaultInfrastructure` reads **no specific named env var** for
Postgres or Redis — callers pass the DSN/URL positionally (or rely
on the constants above).

### `scripts/run_migration.sh`

```bash
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is not set. run_migration.sh requires DATABASE_URL" >&2
    ...
    exit 1
fi
```

```bash
: "${MIGRATION_DRY_RUN:=false}"
```

```bash
LOG_FILE="${LOG_FILE:-/tmp/migrate_us_stocks.log}"
```

Env vars read by `run_migration.sh`:
* **`DATABASE_URL`** — **required**. If unset/empty, the wrapper
  aborts with exit code 1 and a clear error message.
* **`MIGRATION_DRY_RUN`** — optional; defaults to `"false"`. When
  `"true"`, the wrapper passes `--single-transaction` to `psql` so
  the script runs inside one `BEGIN ... ROLLBACK` and nothing is
  committed.
* **`LOG_FILE`** — optional; defaults to `/tmp/migrate_us_stocks.log`.

### Combined env-var inventory for V7

| Env var | Read by | Required? | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `run_migration.sh` | **Yes** | Postgres DSN passed to `psql`. |
| `MIGRATION_DRY_RUN` | `run_migration.sh` and `scripts/migrate_us_stocks.py` | No (defaults to `"false"`) | When truthy, dry-run mode (no commit). |
| `LOG_FILE` | `run_migration.sh` | No (defaults to `/tmp/migrate_us_stocks.log`) | Where stdout+stderr of the `psql` invocation is tee'd. |
| any `name` passed to `DefaultInfrastructure.get_secret(name)` | `DefaultInfrastructure` | depends on caller | Cloud-secret-manager placeholder; reads `os.environ[name]` (i.e. an arbitrary env var whose name matches the `name` argument). |
| (none) | `DefaultInfrastructure` for Postgres/Redis DSN | n/a | DSN/URL are constructor args; the only hard-coded defaults are `DEFAULT_POSTGRES_DSN` and `DEFAULT_REDIS_URL` constants (above). |

**Decision:** later stories that need a Postgres/Redis connection
in the shell wrapper must export `DATABASE_URL`; stories that need
one in Python must pass `DEFAULT_POSTGRES_DSN` /
`DEFAULT_REDIS_URL` (or their own values) to the
`DefaultInfrastructure(...)` constructor — there is no
auto-discovery from env vars inside the Python class.

---

## V8: Is `verify_migration.py` generic or hard-coded to `us_stock`?

**Explicit answer: HARDCODED TO `us_stock`.** It is *not* generic —
it does not accept a table list, a spec list, or a migration name
argument; it has one fixed hard-coded check (the `stocks` table
after `us_stock_portfolio_defaults_v1`) and one optional argument
(the DSN). Every signal in the file points to a single migration:

* `scripts/verify_migration.sql` query (2) filters on the literal:

  ```sql
  WHERE migration_name = 'us_stock_portfolio_defaults_v1'
  ```

* `scripts/verify_migration.py` imports the constant from the
  Python wrapper and uses it in error messages:

  ```python
  from scripts.migrate_us_stocks import MIGRATION_NAME
  ...
  if log_summary["log_success"] == 0:
      failures.append(
          f"migration_log has no SUCCESS row for migration_name={MIGRATION_NAME!r}"
      )
  ```

* The verifier's pass criteria mention only `stocks` — it does not
  accept any `(table, spec)` pair:

  ```python
  if stocks_summary["bad_currency"] != 0:
      failures.append(
          f"{stocks_summary['bad_currency']} stock row(s) have currency <> 'USD'"
      )
  if stocks_summary["bad_exchange"] != 0:
      failures.append(
          f"{stocks_summary['bad_exchange']} stock row(s) have exchange IS NOT NULL"
      )
  if stocks_summary["bad_suffix"] != 0:
      failures.append(
          f"{stocks_summary['bad_suffix']} stock row(s) have symbol_suffix IS NOT NULL"
      )
  ```

* The `verify_migration(dsn=DEFAULT_POSTGRES_DSN)` function
  signature has only one optional argument (the DSN); there is no
  list-of-checks / list-of-tables argument.

There is no CLI flag, no config file, and no env var that switches
the verifier to a different migration. To check a different
migration, a separate `.sql`/`.py` pair must be authored.

**Decision:** later stories must **not** try to make the existing
`verify_migration.py` generic by passing a new spec list — they
must either author a sibling verifier for their migration or
extend the existing SQL/Python in a way that preserves the
`stocks` + `migration_name='us_stock_portfolio_defaults_v1'`
contract that CI/QA tooling already depends on.