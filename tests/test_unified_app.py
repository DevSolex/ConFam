"""
tests/test_unified_app.py

Tests for the unified ASGI entrypoint (app.py) used on Render.

Coverage:
  1. All three route groups respond on the unified app
  2. Admin key: missing → 403, wrong → 403, correct → passes middleware
  3. ADMIN_API_KEY unset → 503 (fail closed)
  4. Public routes work without any key
  5. Checkout routes respond under /pay prefix
  6. checkout_url includes /pay prefix
"""

import os
import uuid

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient


def _make_app(monkeypatch, admin_key="test-admin-key"):
    monkeypatch.setenv("ADMIN_API_KEY", admin_key)
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
    monkeypatch.setenv("CHECKOUT_BASE_URL", "https://confam.onrender.com/pay")
    monkeypatch.setenv("PAYMENT_LINK_EXPIRY_SECONDS", "1800")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_secret")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
    import importlib

    import app as _app
    importlib.reload(_app)
    return _app.app


# ---------------------------------------------------------------------------
# Health (public)
# ---------------------------------------------------------------------------

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_returns_200_no_key(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Admin key middleware
# ---------------------------------------------------------------------------

class TestAdminKeyMiddleware:
    @pytest.mark.asyncio
    async def test_missing_key_returns_403(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.post("/links", json={
                "merchant_id": str(uuid.uuid4()),
                "amount_minor_units": 1000, "currency": "NGN", "description": "t"
            })
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_wrong_key_returns_403(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.post("/links",
                headers={"X-Admin-Key": "wrong-key"},
                json={"merchant_id": str(uuid.uuid4()),
                      "amount_minor_units": 1000, "currency": "NGN", "description": "t"}
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_correct_key_passes_middleware(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.post("/links",
                headers={"X-Admin-Key": "test-admin-key"},
                json={"merchant_id": str(uuid.uuid4()),
                      "amount_minor_units": 1000, "currency": "NGN", "description": "t"}
            )
        # 404 = middleware passed, endpoint rejected nonexistent merchant
        assert resp.status_code != 403, "Key was incorrectly rejected"

    @pytest.mark.asyncio
    async def test_unset_admin_key_returns_503(self, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("CHECKOUT_BASE_URL", "http://localhost/pay")
        monkeypatch.setenv("PAYMENT_LINK_EXPIRY_SECONDS", "1800")
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "s")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "t")
        import importlib

        import app as _app
        importlib.reload(_app)
        async with AsyncClient(
            transport=ASGITransport(app=_app.app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/links",
                headers={"X-Admin-Key": "any-key"},
                json={"merchant_id": str(uuid.uuid4()),
                      "amount_minor_units": 1000, "currency": "NGN", "description": "t"}
            )
        assert resp.status_code == 503
        assert resp.json()["error"] == "service_unavailable"


# ---------------------------------------------------------------------------
# Public routes — no key needed
# ---------------------------------------------------------------------------

class TestPublicRoutes:
    @pytest.mark.asyncio
    async def test_webhook_verify_no_key(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "abc123",
            })
        assert resp.status_code == 200
        assert resp.text == "abc123"

    @pytest.mark.asyncio
    async def test_checkout_page_no_key_returns_404_not_403(self, monkeypatch):
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/pay/{uuid.uuid4()}/json")
        assert resp.status_code == 404
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Checkout /pay prefix + URL shape
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCheckoutPayPrefix:
    @pytest.mark.asyncio
    async def test_checkout_url_includes_pay_prefix(self, monkeypatch):
        """Links created via the unified app must have /pay/ in the checkout URL."""
        application = _make_app(monkeypatch)

        # Insert a test merchant
        db_url = os.environ.get("TEST_DATABASE_URL", "")
        if not db_url:
            pytest.skip("TEST_DATABASE_URL not set")
        conn = psycopg2.connect(db_url.replace("postgresql+psycopg2://", "postgresql://")
                                if "postgresql+psycopg2" in db_url else db_url)
        run_id = uuid.uuid4().hex[:10]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO merchants (whatsapp_number, confam_thread_id, status) "
                "VALUES (%s, %s, 'active') RETURNING merchant_id",
                (f"+234{run_id}", f"unified-{run_id}"),
            )
            merchant_id = str(cur.fetchone()[0])
        conn.commit()
        conn.close()

        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            resp = await client.post("/links",
                headers={"X-Admin-Key": "test-admin-key"},
                json={"merchant_id": merchant_id, "amount_minor_units": 50000,
                      "currency": "NGN", "description": "prefix test"},
            )
        assert resp.status_code == 201
        url = resp.json()["checkout_url"]
        assert "/pay/" in url, f"Expected /pay/ in URL, got: {url}"
        assert "onrender.com/pay/" in url

    @pytest.mark.asyncio
    async def test_all_three_services_respond(self, monkeypatch):
        """Smoke: health, webhook verify, and checkout 404 all return expected codes."""
        application = _make_app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            h = await client.get("/health")
            w = await client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "xyz",
            })
            c = await client.get(f"/pay/{uuid.uuid4()}/json")
        assert h.status_code == 200   # settlement-engine health
        assert w.status_code == 200   # messaging webhook
        assert c.status_code == 404   # checkout (link not found — correct)
