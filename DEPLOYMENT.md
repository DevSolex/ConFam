# ConFam — Deployment Guide

This document covers deploying ConFam to a single EC2 instance for pilot testing (~5 merchants). This is not a production-grade multi-instance setup — that comes later when there's a real reason to need it.

---

## Architecture on the instance

```
Internet
    │
  443/80 (HTTPS/HTTP)
    │
  Caddy (reverse proxy, auto-TLS via Let's Encrypt)
    │
    ├── /webhooks/whatsapp* ──→ messaging:8002   (Meta webhook)
    ├── /webhooks/paystack  ──→ settlement-engine:8000 (Paystack webhook only)
    └── /{everything else}  ──→ checkout:8001    (buyer checkout pages)

Internal Docker network only (not reachable from internet):
  settlement-engine:8000  — /merchants, /links, /internal/*
  postgres:5432
```

OQ-022 network isolation is preserved: settlement-engine's internal endpoints are never reachable from the public internet.

---

## Step 1: Launch an EC2 instance

1. In the AWS Console (or CLI), launch an EC2 instance:
   - **AMI**: Ubuntu 24.04 LTS
   - **Instance type**: `t3.small` (2 vCPU, 2GB RAM — adequate for ~5 testers)
   - **Storage**: 20GB gp3
   - **Security group** — inbound rules:
     | Port | Source | Purpose |
     |------|--------|---------|
     | 22   | Your IP only (`x.x.x.x/32`) | SSH |
     | 80   | 0.0.0.0/0 | Let's Encrypt HTTP challenge |
     | 443  | 0.0.0.0/0 | HTTPS (Caddy) |
   - No other ports open publicly.

2. **Elastic IP (recommended):** Allocate an Elastic IP and associate it with the instance. Without this, the instance gets a new public IP on every restart, which means:
   - The sslip.io hostname changes
   - You must update Meta and Paystack webhook URLs again
   - An Elastic IP costs ~$0/month while attached to a running instance

   If you skip the Elastic IP, accept that a restart requires a one-time webhook URL update in both dashboards (documented in Step 6 below).

3. Note the public IP address.

---

## Step 2: Derive the sslip.io hostname

Replace dots in the IP with dashes and append `.sslip.io`:

```
IP:       54.123.45.67
Hostname: 54-123-45-67.sslip.io
```

This hostname resolves to your IP automatically — no signup, no DNS config. Let's Encrypt will issue a real TLS certificate for it.

Test it resolves correctly:
```bash
dig 54-123-45-67.sslip.io +short  # should return 54.123.45.67
```

---

## Step 3: Provision the instance

SSH in and run the setup script:

```bash
ssh -i your-key.pem ubuntu@<instance-ip>

# Option A: run directly from GitHub
curl -fsSL https://raw.githubusercontent.com/DevSolex/ConFam/main/scripts/ec2_setup.sh | bash

# Option B: if repo is already cloned
bash /home/ubuntu/ConFam/scripts/ec2_setup.sh
```

This installs Docker, clones the repo, copies `.env.example` → `.env`, and opens ports 22/80/443 in ufw.

After the script completes, log out and back in (or run `newgrp docker`) so the docker group takes effect.

---

## Step 4: Configure `.env` on the instance

```bash
nano /home/ubuntu/ConFam/.env
```

Fill in every `PLACEHOLDER_` value. Key ones:

```bash
# Instance hostname (from Step 2)
PUBLIC_HOSTNAME=54-123-45-67.sslip.io

# Database passwords — choose your own strong passwords
POSTGRES_SUPERUSER_PASSWORD=choose_a_strong_password
CONFAM_APP_PASSWORD=choose_a_strong_password
CONFAM_MIGRATOR_PASSWORD=choose_a_strong_password

# Paystack — use test keys until you're ready for live
PAYSTACK_SECRET_KEY=sk_test_xxxxxxxxxxxx
PAYSTACK_WEBHOOK_SECRET=sk_test_xxxxxxxxxxxx

# Meta WhatsApp Cloud API
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_ACCESS_TOKEN=your_permanent_system_user_token
WHATSAPP_APP_SECRET=your_app_secret
WHATSAPP_VERIFY_TOKEN=choose_any_string

# Checkout URL — must match the public hostname
CHECKOUT_BASE_URL=https://54-123-45-67.sslip.io

# Sentry (optional but recommended for production)
SENTRY_DSN=your_sentry_dsn

# Cooling-off for payout account changes (48h default)
PAYOUT_ACCOUNT_COOLING_OFF_SECONDS=172800
```

**Important — permanent WhatsApp access token:** The temporary token from the Meta dashboard expires in 24 hours. For a stable deployment, create a System User token:
1. Meta Business Suite → Settings → System Users
2. Create a system user, add your WhatsApp app, generate a permanent token
3. Use that token as `WHATSAPP_ACCESS_TOKEN`

Lock down the file:
```bash
chmod 600 /home/ubuntu/ConFam/.env
```

---

## Step 5: Start the stack

```bash
cd /home/ubuntu/ConFam
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Watch the logs to confirm migrations ran and all services started:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
```

Verify Caddy obtained a TLS certificate (check logs for `certificate obtained successfully`):
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs caddy
```

Health check:
```bash
curl https://54-123-45-67.sslip.io/health
# {"status":"ok","service":"checkout"}
```

---

## Step 6: Update webhook URLs (one-time manual step)

### Paystack
1. Paystack Dashboard → Settings → API Keys & Webhooks
2. Set webhook URL to:
   ```
   https://54-123-45-67.sslip.io/webhooks/paystack
   ```

### Meta WhatsApp
1. Meta App Dashboard → WhatsApp → Configuration → Webhook
2. Set callback URL to:
   ```
   https://54-123-45-67.sslip.io/webhooks/whatsapp
   ```
3. Set verify token to whatever you put in `WHATSAPP_VERIFY_TOKEN`
4. Click "Verify and Save" — Caddy must be running for this to succeed

---

## Step 7: Verify the full flow

1. Send `REGISTER My Business` to your WhatsApp number → should get a reply
2. Send `ONBOARD 0123456789 058` → Paystack verifies bank, subaccount created
3. Send `PAY 750 Test item` → get a checkout URL back
4. Open the URL in a browser → checkout page renders at the sslip.io domain
5. Complete a test payment → receive "Payment received ✅" WhatsApp message

---

## Redeploying (code updates)

From your local machine:
```bash
./deploy.sh ubuntu@54.123.45.67
```

Or directly on the instance:
```bash
cd /home/ubuntu/ConFam
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## If the instance is restarted (no Elastic IP)

The public IP changes → the sslip.io hostname changes → you must:

1. Update `PUBLIC_HOSTNAME` in `/home/ubuntu/ConFam/.env`
2. Update `CHECKOUT_BASE_URL` in `.env`
3. Restart Caddy: `docker compose -f docker-compose.yml -f docker-compose.prod.yml restart caddy`
4. Update Paystack webhook URL (Step 6 above)
5. Update Meta webhook URL (Step 6 above)

**Recommendation:** Allocate an Elastic IP to avoid this entirely.

---

## Teardown

```bash
# Stop the stack (keeps data)
docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# Full reset including database volume
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
```
