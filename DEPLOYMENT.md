# ConFam — Deployment Guide (Render)

Pilot deployment for ~5 WhatsApp testers. Free tier. Not for real merchant traffic.

---

## Architecture on Render

One Docker web service serves all three internal services on a single port via `app.py`.
Migrations run at container start via `db/render_bootstrap.sh` before uvicorn.

```
Internet → HTTPS (Render-managed TLS)
    │
  confam-xq4m.onrender.com
    │
    ├── /webhooks/whatsapp  — messaging (Meta webhook, public)
    ├── /webhooks/paystack  — settlement engine (Paystack webhook, public)
    ├── /pay/{link_id}      — checkout pages (buyers, public)
    └── /links, /merchants  — admin endpoints (X-Admin-Key required)
```

### Single-role DB difference from local setup

Render's managed Postgres provides one DB user — not a superuser.
The confam_app/confam_migrator role split cannot be created.
Migration 012 adapts RLS policies to work with a single user.

What is preserved: append-only trigger on ledger_entries (fires for all users),
FORCE ROW LEVEL SECURITY on payout_accounts (owner still subject to policies),
DELETE still restricted to active_from > now().

What is weaker: the running app connects as the full DB owner, not the restricted
confam_app role. Known gap for the pilot. Restore the role split when upgrading
to a dedicated Postgres instance.

---

## Step 1: Connect GitHub repo to Render

1. dashboard.render.com → New → Blueprint
2. Connect GitHub → select DevSolex/ConFam
3. Render detects render.yaml automatically → click Apply

---

## Step 2: Set secrets

After the Blueprint deploys, go to the confam service → Environment.
Fill in every sync:false variable:

  ADMIN_API_KEY           — openssl rand -base64 32  (protects admin routes)
  PAYSTACK_SECRET_KEY     — from Paystack dashboard (test key for pilot)
  PAYSTACK_WEBHOOK_SECRET — same as or derived from Paystack secret key
  WHATSAPP_PHONE_NUMBER_ID — Meta App → WhatsApp → API Setup
  WHATSAPP_ACCESS_TOKEN   — permanent System User token (NOT the 24h temp token)
  WHATSAPP_APP_SECRET     — Meta App → Settings → Basic → App Secret
  WHATSAPP_VERIFY_TOKEN   — any string you choose
  CHECKOUT_BASE_URL       — https://confam-xq4m.onrender.com/pay
  PAYMENT_LINK_SIGNING_SECRET — openssl rand -base64 32
  SENTRY_DSN              — optional (sentry.io)

IMPORTANT: Use a permanent System User token for WHATSAPP_ACCESS_TOKEN.
The temporary dashboard token expires in 24 hours.
Meta Business Suite → Settings → System Users → create user → add app → generate token.

---

## Step 3: Deploy

Render deploys automatically on push to main.
Watch logs for:
  [render-bootstrap] Postgres is ready.
  [render-bootstrap] Running Alembic migrations...
  [render-bootstrap] Done. Starting application...
  INFO: Application startup complete.

Health check:
  curl https://confam-xq4m.onrender.com/health

---

## Step 4: Update webhook URLs (you do this manually)

Paystack: Dashboard → Settings → API Keys & Webhooks
  https://confam-xq4m.onrender.com/webhooks/paystack

Meta WhatsApp: App Dashboard → WhatsApp → Configuration → Webhook
  Callback URL: https://confam-xq4m.onrender.com/webhooks/whatsapp
  Verify token: whatever you set for WHATSAPP_VERIFY_TOKEN
  Click "Verify and Save"

---

## Calling admin endpoints

  curl -X POST https://confam-xq4m.onrender.com/merchants \
    -H "X-Admin-Key: your-admin-key" \
    -H "Content-Type: application/json" \
    -d '{"whatsapp_number": "+234...", "business_name": "My Store"}'

  curl -X POST https://confam-xq4m.onrender.com/links \
    -H "X-Admin-Key: your-admin-key" \
    -H "Content-Type: application/json" \
    -d '{"merchant_id": "...", "amount_minor_units": 75000, "currency": "NGN", "description": "Item"}'

---

## Free tier constraints

SERVICE SLEEP: Render free services sleep after ~15 min idle. ~60s wake-up time.
  Workaround: external pinger (UptimeRobot free, cron-job.org) hitting /health
  every 10 minutes. Testing workaround only — not a production setup.

POSTGRES EXPIRY: Free Postgres expires 30 days after creation (14-day grace).
  Before expiry:
    Option A: Upgrade to Render paid Postgres ($7/month) — no data loss
    Option B: Create new free DB, update DATABASE_URL, re-run migrations,
              re-onboard merchants (data loss — acceptable for pilot)

INSTANCE HOURS: ~750 free hours/month per workspace = one always-on service.
  Do not add a second free web service or the first gets suspended mid-month.

---

## Redeploying

  git push origin main   # triggers auto-deploy

Or: Render dashboard → confam → Manual Deploy → Deploy latest commit.

---

## Schema migrations on Render

Migrations run automatically at container start via `db/render_bootstrap.sh`.
To verify all migrations are applied after a new deploy:

  curl -s https://confam-xq4m.onrender.com/health
  # {"status":"ok","service":"confam"}

If a migration fails, the bootstrap script exits non-zero and Render marks the
deploy as failed. Check the deploy logs in the Render dashboard.

Current migrations applied (001–015):
  001  create_merchants
  002  create_payout_accounts
  003  create_payment_links
  004  create_rail_events
  005  create_ledger_entries
  006  harden_ledger_append_only
  007  create_roles_and_grants
  008  add_subaccount_to_payout_accounts
  009  add_merchant_onboarding_fields
  010  payout_account_change_flow
  011  payout_accounts_rls_cancel
  012  render_single_role_adaptation
  013  flag_rail_event_disposition
  014  add_merchant_country          ← Nigeria/Ghana country field
  015  pending_bank_selection        ← tap-to-select ONBOARD state
