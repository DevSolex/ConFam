# Running ConFam locally

This document covers everything needed to start the stack, run tests, and manually verify the payment link flow end-to-end. Complete these steps in order the first time.

---

## Prerequisites

- Docker and Docker Compose (Docker Desktop, or `docker` + `docker compose` CLI on Linux)
- `curl` and a browser (for the manual smoke test at the end)
- No local Postgres or Python installation required — everything runs in containers

---

## 1. First-time setup

Copy the environment template and fill in passwords:

```bash
cp .env.example .env
```

Open `.env` and set these four values in Section A — choose any passwords you like:

```
POSTGRES_SUPERUSER_PASSWORD=changeme_superuser
POSTGRES_DB=confam
CONFAM_APP_PASSWORD=changeme_app
CONFAM_MIGRATOR_PASSWORD=changeme_migrator
```

The rest of Section B (DATABASE_URL etc.) is pre-filled to match these defaults and does not need editing for local dev.

The remaining variables (Twilio, Paystack, Stellar, AWS, Sentry) are placeholders — they are not required to start the stack for local development. The services will start without them; features that use them will fail gracefully when those integrations are actually called.

---

## 2. Start the stack

> **Added or changed a dependency in `pyproject.toml`? Run `docker compose build` before `docker compose up`, not just `up`.** Docker caches the dependency install layer — without a rebuild, new packages won't exist inside the containers.

The settlement engine has no host-exposed port by default (OQ-022 network isolation). Use the appropriate compose command:

```bash
# Normal usage — settlement-engine internal only (no direct curl access from host)
docker compose up --build

# Development — exposes settlement-engine on localhost:8000 for curl/ngrok/smoke tests
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

What happens, in order:
1. `postgres` starts and becomes healthy (`pg_isready` passes).
2. `migrate` runs `db/bootstrap_roles.sh`: creates `confam_app` and `confam_migrator` roles, then applies all Alembic migrations. Exits 0.
3. `settlement-engine` starts (internal only unless using docker-compose.dev.yml).
4. `checkout` starts on port 8001, `messaging` on port 8002.

First build takes ~2 minutes (downloading base image, installing Python dependencies). Subsequent starts are fast — Docker caches the dependency layer unless `pyproject.toml` changes.

You should see something like:

```
confam-migrate-1          | [bootstrap] Postgres is ready.
confam-migrate-1          | [bootstrap] Creating roles (idempotent)...
confam-migrate-1          | [bootstrap] Running Alembic migrations as confam_migrator...
confam-migrate-1          | INFO  [alembic.runtime.migration] Running upgrade  -> 0001_initial_schema
confam-migrate-1          | [bootstrap] Done. Schema is up to date.
confam-migrate-1 exited with code 0
confam-settlement-engine-1 | INFO:     Application startup complete.
confam-checkout-1          | INFO:     Application startup complete.
```

---

## 3. Verify the services are up

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"settlement-engine"}

curl http://localhost:8001/health
# {"status":"ok","service":"checkout"}
```

---

## 4. Manual smoke test — onboard a merchant and run a payment end-to-end

No SQL inserts required. All steps use real API endpoints.

### Prerequisites: Meta Cloud API + Paystack credentials in `.env`

```
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_ACCESS_TOKEN=your_access_token
WHATSAPP_APP_SECRET=your_app_secret
WHATSAPP_VERIFY_TOKEN=choose_any_string
PAYSTACK_SECRET_KEY=sk_test_xxxxxxxxxxxxxxxxxxxx
PAYSTACK_WEBHOOK_SECRET=sk_test_xxxxxxxxxxxxxxxxxxxx
```

Get Meta credentials from [developers.facebook.com](https://developers.facebook.com): create a Meta App → add WhatsApp product → copy Phone Number ID and access token.

Restart after editing `.env`: `docker compose up -d`

> **Added or changed a dependency in `pyproject.toml`?** Run `docker compose build` before `docker compose up`.

---

### Step 1: Expose the messaging service and register the Meta webhook

```bash
ngrok http 8002
```

In Meta App dashboard (WhatsApp → Configuration → Webhook):
- **Callback URL:** `https://<ngrok-id>.ngrok.io/webhooks/whatsapp`
- **Verify token:** the value you set for `WHATSAPP_VERIFY_TOKEN`

Click "Verify and Save". The handshake completes automatically.

### Step 2: Create a merchant

```bash
curl -s -X POST http://localhost:8000/merchants \
  -H "Content-Type: application/json" \
  -d '{"whatsapp_number": "+234XXXXXXXXXX", "business_name": "Adaeze Fashion Store"}' \
  | python3 -m json.tool
```

Copy the `merchant_id`.

### Step 3: Submit and verify a payout account

```bash
curl -s -X POST http://localhost:8000/merchants/<merchant_id>/payout-account \
  -H "Content-Type: application/json" \
  -d '{"bank_account_number": "0123456789", "bank_code": "058"}' \
  | python3 -m json.tool
```

Expected: `{"merchant_status": "active", "paystack_subaccount_code": "ACCT_...", ...}`

### Step 4: Set your WhatsApp number as confam_thread_id

Meta sends your number as **bare digits** — e.g. `2348012345678` for +234 801 234 5678 (no `+`, no `whatsapp:` prefix):

```bash
docker compose exec postgres psql -U postgres -d confam \
  -c "UPDATE merchants SET confam_thread_id = '2348012345678' WHERE merchant_id = '<merchant_id>';"
```

### Step 5: Add yourself as a Meta test recipient

In Meta App dashboard → WhatsApp → API Setup, add your personal number as a test recipient (required for the dev access token to reach you).

### Step 6: Send PAY from your WhatsApp

Send this to your Meta test number from your personal WhatsApp:

```
PAY 750 Ankara fabric x2
```

You should receive a reply with the checkout URL within seconds.

### Step 7: Initiate the Paystack payment

```bash
curl -s -X POST http://localhost:8001/<link_id>/pay/bank \
  -H "Content-Type: application/json" \
  -d '{"email": "buyer@example.com"}' | python3 -m json.tool
```

Open the `authorization_url` and pay with test card `4084 0840 8408 4081` (any future expiry, CVV `408`).

### Step 8: Receive the Paystack webhook

```bash
ngrok http 8000
# Set Paystack webhook URL to: https://<id>.ngrok.io/webhooks/paystack
```

After payment, Paystack sends `charge.success` → ledger entry written → "Payment received ✅" WhatsApp arrives.

### Step 9: Verify the ledger entry

```bash
docker compose exec postgres psql -U postgres -d confam \
  -c "SELECT link_id, amount_minor_units, entry_type FROM ledger_entries ORDER BY created_at DESC LIMIT 3;"
docker compose exec postgres psql -U postgres -d confam \
  -c "SELECT link_id, status FROM payment_links ORDER BY created_at DESC LIMIT 3;"
```

---
## 5. Run the test suite in Docker


```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm test
```

This runs all tests inside the Docker network against the containerised Postgres, using the same `confam_app`/`confam_migrator` role separation as CI. Expected output:

```
33 passed, 4 skipped
```

The 4 skips are intentional placeholders for tests that require the settlement engine's payment confirmation logic — those will be replaced in the next task.

---

## 6. Tear down and reset

Stop the stack (keeps data):
```bash
docker compose down
```

Stop and wipe the database volume (full reset — use when you want a clean migration run):
```bash
docker compose down -v
```

After `down -v`, the next `docker compose up` will re-run `migrate` from scratch.

---

## 7. Running tests outside Docker (venv)

If you have a local Python 3.11+ environment and a local Postgres instance:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Set environment variables to point at your local Postgres
export DATABASE_URL="postgresql://confam_app:yourpassword@localhost:5432/confam"
export TEST_DATABASE_URL="postgresql://confam_app:yourpassword@localhost:5432/confam"
export ALEMBIC_DATABASE_URL="postgresql+psycopg2://confam_migrator:yourpassword@localhost:5432/confam"
export PAYMENT_LINK_EXPIRY_SECONDS=1800
export CHECKOUT_BASE_URL="http://localhost:8001"

pytest tests/ -v
```

---

## Ports

| Service           | Host port | Notes                          |
|-------------------|-----------|--------------------------------|
| settlement-engine | 8000      | `POST /links`, `GET /health`   |
| checkout          | 8001      | `GET /{link_id}`, `GET /health`|
| postgres          | 5434      | Exposed for direct psql access |

Postgres is on **5434** (not 5432) to avoid conflicting with any locally-installed Postgres instance.
