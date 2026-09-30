# ConFam

**ConFam gives informal chat-commerce merchants a balance sheet.**

> "We are not digitising them — we are giving them a balance sheet."

---

## What it does

Merchants in Nigeria and Ghana sell through WhatsApp every day. ConFam inserts itself at the one moment that breaks the flow: payment collection. The merchant types `PAY 2500 Jordan` in a separate ConFam WhatsApp thread, gets a one-time link back, pastes it to the buyer, and receives a "Payment received ✅" notification the moment Paystack confirms the transfer. Every sale lands permanently in a naira (or GHS) ledger — the underwriting file for future working-capital lending.

ConFam never holds funds. Every payment routes to the merchant's own pre-verified bank account or mobile money number.

---

## Non-negotiable principles

1. **Zero-custody in code, not policy.** A payout can only ever land on a pre-verified, whitelisted account. The capability to redirect a payout elsewhere does not exist in the codebase.
2. **The ledger is append-only and is the product.** Nothing written is ever edited or deleted. It is the asset the entire business model is built on.
3. **No behaviour change for either side.** Merchants keep selling on WhatsApp. ConFam inserts itself only at payment.
4. **Every transaction is traceable end-to-end** — from the WhatsApp message to the ledger row.

The full rules are in `.kiro/steering/engineering_rules.md` (11 rules, all strict).

---

## Current status

Live on Render: **https://confam-xq4m.onrender.com**  
Landing page: **https://con-fam.vercel.app**

**Built and passing:**
- WhatsApp command flow: `REGISTER`, `ONBOARD`, `PAY`, `LEDGER`, `UPDATE`, `CANCEL`
- Interactive tap-to-select: REGISTER sends country buttons (Nigeria/Ghana), bare ONBOARD sends tappable bank list
- Bank-name fuzzy matching (`resolve_bank_name`) + 1-hour cached bank list per country
- Ghana support: GHS currency, mobile money (MTN MoMo, AirtelTigo, Vodafone Cash) in bank list
- Paystack bank rail: link creation → checkout → webhook → ledger write (~2ms)
- Append-only ledger with no-double-count guarantee (50+ transaction test)
- Payout account change flow with 48h cooling-off and instant WhatsApp notification
- PDF sales statement (`LEDGER` command)
- 100+ automated tests passing

**Countries supported:** Nigeria (NGN), Ghana (GHS)  
**Payment rails:** Paystack (bank transfer / card). Stellar/Lobstr in design.

---

## Repository layout

```
app.py                        — unified ASGI entrypoint (single Render service)
confam/
  paystack.py                 — Paystack API client, bank-name resolver, list cache
  interactive.py              — WhatsApp interactive messages, common-bank config
  ledger.py                   — append-only ledger writes
  links.py                    — payment link creation and signing
  payout_accounts.py          — payout account whitelist and cooling-off logic
  db.py                       — connection pool
services/
  messaging/main.py           — WhatsApp webhook handler and command router
  messaging/statement.py      — PDF ledger statement (LEDGER command)
  settlement_engine/
    webhook.py                — Paystack webhook handler
    onboarding.py             — merchant + payout account REST API
    reconciliation.py         — scheduled reconciliation job
  checkout/                   — buyer-facing checkout page
rails/
  stellar/                    — Stellar/SEP-24 scaffold (not yet live)
  bank/                       — Paystack adapter
db/migrations/                — plain SQL migrations (001–015)
alembic/versions/             — Alembic wrappers
tests/                        — pytest suite
landing/                      — React/Vite marketing site (Vercel)
```

---

## Running locally

See `RUNNING.md` for the full local setup, Docker commands, and manual smoke test.

Quick start:
```bash
cp .env.example .env   # fill in passwords
docker compose up --build
curl http://localhost:8001/health
```

---

## Deployment

See `DEPLOYMENT.md`. The app auto-deploys to Render on every push to `main`.

```bash
git push origin main
```
