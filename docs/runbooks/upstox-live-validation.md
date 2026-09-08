# Upstox Live Validation — Runbook

> This runbook is exercised by a human against one small real Upstox
> account. It is **not** run by CI, **not** run by any automated test,
> and **not** part of any deployment gate. See §0 for the two
> non-negotiable statements that make it safe to run at all.

This runbook is the explicit follow-up: a human walks through the steps
below against **one small real Upstox account** to confirm the connector
behaves identically against real Upstox HTTP responses as it does against
the fixtures. The numbered checklist in §9 is the executable version of
this document — anyone can tick the boxes top-to-bottom and end up with
either a recorded pass or a recorded failure in `checkpoint.md`.

This runbook is the explicit follow-up: a human walks through the steps
below against **one small real Upstox account** to confirm the connector
behaves identically against real Upstox HTTP responses as it does against
the fixtures. The numbered checklist in §9 is the executable version of
this document — anyone can tick the boxes top-to-bottom and end up with
either a recorded pass or a recorded failure in `checkpoint.md`.

---

## 0. The two statements you must internalise before reading any further

These are the load-bearing claims of this entire runbook. They are
restated as a numbered section (not buried in a blockquote, not implied
by the procedure) so a reader cannot reach §1 without seeing them.

### 0.1 This is a manual, out-of-pipeline step

**This runbook is a manual, out-of-pipeline step.** It is documentation
for a human, not an executable script, not a CI job, not a scheduled
task, and not part of any deployment gate. Nothing in this repository
automatically executes it; nothing in this repository should ever
automatically execute it. If a future contributor is tempted to "just
script this runbook" — **don't**. The honest reason this runbook exists
at all is recorded in ADR-0023: Upstox's public sandbox
(`sandbox.upstox.com`) covers **only** order placement/modify/cancel and
does **not** cover the long-term-holdings or charges/historical-trades
read endpoints this feature needs, so there is no sandbox path to
validate the real read flow end-to-end before a live account exists.
Correctness of parsing/mapping/import is therefore established only
against hand-authored fixtures in `tests/fixtures/upstox/`
(`long_term_holdings_empty.json`, `long_term_holdings_success.json`,
etc.) matching the real documented response shapes.

### 0.2 Automated tests must never call the real or sandbox Upstox API

**Automated tests must never call the real or sandbox Upstox API.**
This statement applies to the entire automated test suite — pytest
runs, CI runs, GitHub Actions workflows, any future scheduled task,
any future "smoke" job, anything that fires without a human clicking
"go". The only network egress to Upstox from this codebase is the
private `_UpstoxHttp` helper in `src/upstox_http.py`, and it is
injected into `DefaultUpstoxBrokerConnector`
(`src/components/c01_user_portfolio.py`) precisely so tests can
substitute a fake without ever opening a network socket. This runbook
is the only place the real wrapper is exercised against real Upstox,
and it is exercised by a human, in a browser, not by code. Adding a
test that hits `https://api.upstox.com` or `https://sandbox.upstox.com`
— even "just once", even "just for a smoke check", even "just behind a
flag" — is a violation of this runbook's contract and must be rejected
in review. (The fixture-only tests in `tests/test_story5_*`,
`tests/test_story23_*`, `tests/test_qa_story19_broker_import.py`, and
the rest of the broker-import suites are the right way to validate
connector behaviour without ever touching the network.)

These two statements are restated in checklist form in §9 (steps 1 and
2 of the executable checklist) so they cannot be missed even by a
reader who skips the prose and goes straight to the tickboxes.

---

## 1. Prerequisites

Before starting, gather these. None of them require a live Upstox
account yet — they are all local setup.

1. **One small, low-value real Upstox account.** Use an account with
   only a handful of holdings and a short transaction history — the
   point is to validate, not to stress-test. Do **not** run this against
   a primary portfolio.
2. **Local checkout of this repo** with dependencies installed:
   ```bash
   uv sync --extra dev
   ```
   `src/` is on the test path via `pyproject.toml`, so
   `from components.c01_user_portfolio import DefaultUpstoxBrokerConnector`
   and `from upstox_config import UpstoxConfig` resolve with no extra
   `PYTHONPATH` juggling.
3. **A running local Postgres** (the project's `docker-compose.yml`
   brings one up; this runbook does not need it for the Upstox HTTP
   call itself, but it does need it for the `CONNECTED` row check (§5)
   and for the imported-rows-vs-`total_records` check (§7)).
4. **`psycopg` available** to the same `uv` environment (already pulled
   by `uv sync --extra dev`).
5. **A throwaway `.env` file with only Upstox credentials** (do not
   reuse your primary `.env` — the credentials get rotated in §8):
   ```bash
   cat > .env.upstox-validation <<'EOF'
   UPSTOX_CLIENT_ID=<paste from Upstox developer portal>
   UPSTOX_CLIENT_SECRET=<paste from Upstox developer portal>
   UPSTOX_REDIRECT_URI=http://localhost:8000/brokers/upstox/callback
   BROKER_TOKEN_ENCRYPTION_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
   DATABASE_URL=postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent
   EOF
   set -a; source .env.upstox-validation; set +a
   ```
   `UpstoxConfig.from_env()` in `src/upstox_config.py` is the real
   validator — it raises `BrokerConfigError` listing every missing or
   empty variable, so a forgotten field fails immediately and loudly
   rather than at the first HTTP call.

---

## 2. Register an Upstox app with the **exact** redirect URI

The redirect URI must match `UPSTOX_REDIRECT_URI` character-for-character
— Upstox rejects mismatches at the OAuth callback step, and the failure
mode is a confusing "invalid `redirect_uri`" error from the
`exchange_auth_code` call, not a clear local exception.

1. Sign in to the Upstox developer portal
   (`https://upstox.com/developer/api-documentation/`) with the
   throwaway account's credentials.
2. **My Apps → Create app.**
3. Set the **Redirect URI** field to exactly:
   ```
   http://localhost:8000/brokers/upstox/callback
   ```
   Copy/paste — do not retype. The trailing slash, the port, the scheme,
   and the lowercase `b` in `brokers` are all part of the match.
4. Note the issued `client_id` and `client_secret` and paste them into
   `.env.upstox-validation` (overwriting the placeholders from §1).
5. Re-source the env file:
   ```bash
   set -a; source .env.upstox-validation; set +a
   ```
6. Confirm `UpstoxConfig.from_env()` succeeds:
   ```bash
   python -c "from upstox_config import UpstoxConfig; print(UpstoxConfig.from_env())"
   ```
   Expected: a printed `UpstoxConfig(client_id=..., client_secret=...,
   redirect_uri='http://localhost:8000/brokers/upstox/callback')` line
   and exit code `0`. If this fails, the env vars are not exported in
   the shell that will run §3 — re-source and retry before continuing.

---

## 3. Run the connect flow against the real account

1. Start the webapp locally against `.env.upstox-validation`:
   ```bash
   set -a; source .env.upstox-validation; set +a
   python -m src.webapp
   ```
2. In a browser, visit `http://localhost:8000/settings/brokers` for the
   throwaway user. Confirm the registered connector list contains an
   entry with `broker_id == 'upstox'` (sourced from
   `DefaultUpstoxBrokerConnector.broker_id` in
   `src/components/c01_user_portfolio.py`).
3. Click **Connect Upstox**. The authorize URL the app redirects to is
   built by `DefaultUpstoxBrokerConnector.build_authorize_url`, which
   produces exactly:
   ```
   https://api.upstox.com/v2/login/authorization/dialog
       ?response_type=code
       &client_id=<percent-encoded client_id>
       &redirect_uri=<percent-encoded redirect_uri>
       &state=<percent-encoded state>
   ```
   Log in with the throwaway account and approve.
4. Upstox redirects back to
   `http://localhost:8000/brokers/upstox/callback?code=...&state=...`.
   The webapp's callback handler calls
   `DefaultUpstoxBrokerConnector.exchange_auth_code` to swap the `code`
   for an access token, stores the encrypted token via
   `src/broker_token_crypto.py`, and writes a row to the broker
   connection table.

> **Reminder for step 4.** Anything that just happened in the browser
> was a real Upstox HTTP round trip. The test suite never does this and
> must never do this; this runbook is the only place it happens.

---

## 4. Confirm a `CONNECTED` row exists

The `CONNECTED` state is what `connect_portfolio` returns on the success
path — a missing or wrong-state row here means the callback handler did
not run end-to-end against real Upstox, and §5 / §6 / §7 below are
meaningless without it.

Run, against the local `DATABASE_URL` from `.env.upstox-validation`:

```sql
SELECT user_id,
       broker_id,
       status,
       created_at,
       last_refreshed_at
FROM   broker_connections
WHERE  broker_id = 'upstox'
  AND  user_id   = '<throwaway user id>'
ORDER BY created_at DESC
LIMIT  1;
```

Expected: exactly one row, with `status = 'CONNECTED'`, a non-null
`created_at`, and a non-null `last_refreshed_at`. The last field is
populated by the token-exchange call against real Upstox, so a NULL
there means `exchange_auth_code` did not actually reach Upstox.

If the row is missing or in a different state, **stop here** — the
spot-checks in §6 and §7 are not meaningful without a real, live token.

---

## 5. Trigger the holdings import

1. From the same browser session as §3, navigate to the holdings import
   affordance for the same `(user_id, portfolio_id)` pair (the route
   backed by `DefaultUserPortfolio.import_holdings`). Trigger the import.
2. Capture the count of imported holdings rows from the response.
3. Do **not** re-trigger or replay this from a script — the next two
   sections are spot-checks against what you just got back.

---

## 6. Spot-check the holdings against the Upstox web portal

**Do this in the browser the user will trust** — the real Upstox web
portal, not a screenshot tool, not a cached page. The comparison is the
whole point of this runbook; an outdated page invalidates it.

1. Open the Upstox web portal → **Holdings** for the throwaway account.
2. **Spot-check #1 — holdings count.** Compare the holdings count from
   the import response (§5.2) to the count shown on the Upstox web
   portal Holdings page. They must be equal. A mismatch by even one row
   means the connector silently dropped an element (the per-element
   skip rule in `DefaultUpstoxBrokerConnector.fetch_holdings` logs
   `[UPSTOX_HOLDING_ELEMENT_SKIPPED]` for elements missing
   `trading_symbol`; check the app logs for those lines if a mismatch
   appears).
3. **Spot-check #2 — sample `trade_id`s / quantities / prices.** Pick
   **three holdings at random** from the Upstox portal. For each one,
   confirm the imported row has the same:
   - `trading_symbol` (must match exactly, including any `EQ` suffix
     Upstox appends),
   - `instrument_token`,
   - `quantity` (whole shares; partial-unit fractional shares do not
     appear in `fetch_holdings`),
   - `average_price` (to 2 decimal places).

   A mismatch on any of these four fields means the parser/mapper is
   silently wrong and the fixture-only test suite has been validating
   against an outdated shape.

If any spot-check in §6 fails, record the exact mismatch in
`checkpoint.md` (see §8) and **stop** — §7 still gets a count comparison
but the per-row spot-checks in §7 are unlikely to be meaningful when
the holdings shape is already wrong.

---

## 7. Spot-check the transactions and confirm multi-page paging

1. Trigger `import_transactions` for a date range large enough to span
   multiple Upstox pages (e.g., the last full financial year plus the
   current year-to-date — Upstox will only return up to 3 financial
   years, per ADR-0023 Consequence #3).
2. Capture the count of imported transaction rows and Upstox's
   `total_records` field from the response.
3. **Spot-check #3 — `total_records` vs. imported row count.** Compare
   the count of rows the import wrote to Upstox's `total_records`. They
   must be equal. A short count means the connector is not following
   the `next_page` cursor correctly and is dropping pages; a long count
   means it is duplicating rows. Either failure mode invalidates
   multi-page correctness.
4. **Spot-check #4 — sample transaction fields.** Pick **two trades at
   random** from the Upstox portal's **Contract notes** / **Trade book**
   view. For each, confirm the imported `BrokerTransaction` has the
   same:
   - `trade_id`,
   - `trading_symbol` / `instrument_token`,
   - `trade_date`,
   - `quantity`,
   - `price` (per-unit, to 2 decimal places),
   - `side` (`BUY` / `SELL`).

If any spot-check in §7 fails, record the exact mismatch in
`checkpoint.md` (§8) before doing anything else.

---

## 8. Record the result in `checkpoint.md`

Whatever the spot-checks found — clean pass, a missed element, a wrong
field, a paging mismatch — record it in `checkpoint.md` under a new
`Upstox live validation, <date>` heading. **Quote the exact Upstox
field value and the exact imported value** when reporting a mismatch;
do not paraphrase. A future maintainer reading `checkpoint.md` must be
able to reproduce the comparison from the recorded values alone, with
no access to the live account.

The three open questions listed in ADR-0023 are explicitly out of
scope for this runbook and **must remain open** after validation, even
if the spot-checks all passed:

1. Upstox rate limits are undocumented — our retry policy in
   `_UpstoxHttp` (`src/upstox_http.py`) is a conservative guess, and
   this runbook does **not** attempt to characterize the real limits.
2. Whether the token response includes `expires_in` / `refresh_token`
   is not answerable from the sandbox (no sandbox for this endpoint)
   and is intentionally **handled as absent** by the connector. If this
   validation run reveals Upstox actually does return either field,
   record the finding in `checkpoint.md` and raise a follow-up story —
   do not silently flip the connector's branch on the basis of one
   manual run.
3. Whether a background/async import job is needed once real portfolio
   sizes are known is a separate concern that this runbook deliberately
   does not decide.

---

## 9. The checklist (executable top-to-bottom)

This is the numbered, executable-by-a-human checklist. Tick each box
as you complete the corresponding step above. Every box must be ticked
(checked, dated, and initialled) before the runbook is considered
"done" for this validation pass, and the result recorded under §8.

The first two boxes restate the two load-bearing claims from §0 as
explicit checklist items, so even a reader who skips the prose and
goes straight to the tickboxes cannot miss them.

1. [ ] **Acknowledged: this is a manual, out-of-pipeline step.** I am
       running this as a human, by hand, not via any CI job, scheduled
       task, or deployment script (§0.1).
2. [ ] **Acknowledged: automated tests must never call the real or
       sandbox Upstox API.** No code path I add or modify as part of
       this runbook (or as a follow-up) will hit `api.upstox.com` or
       `sandbox.upstox.com` from a test (§0.2).
3. [ ] Throwaway Upstox account prepared; primary account untouched.
2. [ ] `.env.upstox-validation` populated with `UPSTOX_CLIENT_ID`,
       `UPSTOX_CLIENT_SECRET`, `UPSTOX_REDIRECT_URI`
       (`http://localhost:8000/brokers/upstox/callback`), and
       `BROKER_TOKEN_ENCRYPTION_KEY`; `python -c "from upstox_config
       import UpstoxConfig; print(UpstoxConfig.from_env())"` exits `0`.
3. [ ] Upstox app registered with the exact redirect URI from §2.
4. [ ] Connect flow run in the browser; callback handler reached.
5. [ ] `SELECT … FROM broker_connections WHERE broker_id = 'upstox'
       AND user_id = '<throwaway user id>'` returns one `CONNECTED`
       row with non-null `last_refreshed_at`.
6. [ ] Holdings import run; **holdings count** captured from the
       response (§5).
7. [ ] **Spot-check #1 — holdings count** matches the Upstox web
       portal Holdings page (§6.2).
8. [ ] **Spot-check #2 — three random holdings** all match on
       `trading_symbol`, `instrument_token`, `quantity`, and
       `average_price` against the Upstox web portal (§6.3).
9. [ ] Transactions import run; imported row count and Upstox's
       `total_records` both captured.
10. [ ] **Spot-check #3 — `total_records` vs. imported row count**
       match (§7.3) — this confirms multi-page transaction paging.
11. [ ] **Spot-check #4 — two random transactions** all match on
       `trade_id`, `trading_symbol` / `instrument_token`, `trade_date`,
       `quantity`, `price`, and `side` against the Upstox web portal
       (§7.4).
12. [ ] Result (pass / specific failure) recorded in `checkpoint.md`
       under `Upstox live validation, <date>`, with exact field values
       for any mismatch.
13. [ ] Three open questions from ADR-0023 confirmed still open in
       `checkpoint.md` (not silently resolved by this run).
14. [ ] Upstox app revoked in the portal; `broker_connections` row
       transitioned out of `CONNECTED` (verify with the SELECT in
       "Revoke / disconnect" step 2 below).
15. [ ] `.env.upstox-validation` deleted.
16. [ ] Throwaway app registration deleted from the Upstox portal if
       the portal supports it.

### Revoke / disconnect (after step 14)

When the spot-checks are complete (whether they passed or failed):

1. In the Upstox web portal → **My Apps → <this app> → Revoke**. The
   access token issued in §3 becomes unusable immediately, and
   `import_holdings` / `import_transactions` on the next run will fail
   at the HTTP layer with a `401`/`403` from `_UpstoxHttp` — that is
   the expected, desired end state.
2. In the local app, call `disconnect_portfolio` for the throwaway
   user so the `broker_connections` row from §4 transitions out of
   `CONNECTED`. Verify with:
   ```sql
   SELECT status FROM broker_connections
   WHERE  broker_id = 'upstox' AND user_id = '<throwaway user id>'
   ORDER BY created_at DESC LIMIT 1;
   ```
3. Delete `.env.upstox-validation`. The `UPSTOX_CLIENT_ID`,
   `UPSTOX_CLIENT_SECRET`, and `UPSTOX_REDIRECT_URI` it contained are
   now single-use; leaving them on disk is a credential-leak risk and
   adds nothing to the audit trail (`checkpoint.md` is the audit trail,
   not the env file).
4. If the portal allows it, **delete the throwaway app registration**
   entirely so the `client_id` / `client_secret` pair cannot be reused.

---

## 10. Hard rules — non-negotiable

These are the rules that make this runbook safe to run at all. They are
restated here in one place because the rest of the document is
optimised for "follow the steps"; this section is optimised for
"don't be clever".

1. **No code path may call the real or sandbox Upstox API
   automatically.** The HTTP wrapper (`_UpstoxHttp` in
   `src/upstox_http.py`) is the only network egress to Upstox; every
   test that uses it substitutes a fake. This runbook is the only
   place the real wrapper is exercised against real Upstox, and it is
   exercised by a human, in a browser, not by code.
2. **No CI job, no scheduled task, no deployment step invokes this
   runbook.** It is documentation for a human, not an executable
   script. If a future contributor finds themselves wanting to
   automate it, they should instead raise a follow-up story that
   either (a) extends the fixture suite if the gap is shape-only, or
   (b) proposes a real Upstox sandbox expansion if Upstox ever
   publishes one — both of which are code changes, not runbook
   automation.
3. **The throwaway account stays throwaway.** It must not be reused for
   any non-validation purpose, must not be linked to a primary
   portfolio, and must be revoked/disconnected at the end of the
   runbook (§9 "Revoke / disconnect").
4. **The credentials in `.env.upstox-validation` are single-use.**
   They are created in §1, used in §2–§7, and deleted in §9
   "Revoke / disconnect" step 3. They are never committed, never
   pasted into chat, and never written to any file under `tests/` or
   `docs/`.
