"""
services/checkout — FastAPI application entry point.

Current scope: render a PaymentLink by link_id, enforce expiry/state,
transition created -> opened, and let the buyer actually pay.

Buyer flow (all buyer-facing, all PCI-safe):
  GET  /{link_id}          the checkout page: amount, merchant, email field,
                           and a button per payment method
  POST /{link_id}/pay      initialize the payment, return Paystack's URL
  GET  /{link_id}/status   poll after the buyer returns from Paystack

The buyer is redirected to Paystack's hosted page to enter any card details.
ConFam never sees them. See services/checkout/pay.py for the PCI note.

Still not implemented: Stellar/Lobstr (OQ-023/OQ-024 — vendor due diligence
unfinished, not an engineering blocker) and inbound transfer approval.

See docs/ARCHITECTURE.md §3.2 and services/checkout/README.md.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from confam.db import close_pool, get_conn
from confam.links import open_link
from services.checkout.link_state import buyer_state, merchant_name_for
from services.checkout.link_state import router as status_router
from services.checkout.middleware import RateLimitMiddleware
from services.checkout.pay import router as pay_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    close_pool()


app = FastAPI(
    title="ConFam Checkout",
    description="Buyer-facing checkout page service.",
    version="0.2.0",
    lifespan=lifespan,
)

# Registered BEFORE the /{link_id} wildcard so FastAPI does not try to read
# "/health" as a link_id.
app.include_router(pay_router)
app.include_router(status_router)

# Rate limiting — in-process sliding window (production: replace with Redis/WAF)
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class CheckoutDetail(BaseModel):
    link_id: str
    description: str
    amount_minor_units: int
    currency: str
    status: str
    expires_at: str
    # Buyer-facing state (waiting/logged/failed/expired) rather than the
    # internal one, so the page and the poll endpoint can never disagree.
    state: str
    # Nullable: merchants registered before migration 009 have no business_name.
    merchant_name: str | None


# ---------------------------------------------------------------------------
# Health check — must be defined BEFORE wildcard /{link_id} routes
# so FastAPI doesn't swallow /health as a link_id.
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "checkout"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/{link_id}/json", response_model=CheckoutDetail)
def get_checkout_json(link_id: str) -> CheckoutDetail:
    """
    JSON view of a link, for tests and future frontend clients.

    Strict about state: anything that must not be offered to a buyer as a live
    checkout is a 410 here, including an already-paid link. The HTML page is
    deliberately more forgiving — see get_checkout_page.
    """
    return _load_and_open(link_id)


@app.get("/{link_id}", response_class=HTMLResponse)
def get_checkout_page(link_id: str, request: Request) -> HTMLResponse:
    """
    Buyer-facing checkout page.

    Unlike /json this does NOT 410 on a terminal link. The buyer arrives here
    from Paystack's callback after paying, and by then the webhook may already
    have marked the link 'logged' — a 410 would greet a buyer who just paid
    with "link unavailable". Instead the page renders the same shell with the
    state filled in, so the same URL works before, during and after payment.

    Only a genuinely unknown link is a 410-class error.
    """
    try:
        detail, payable = _load_for_page(link_id)
    except HTTPException as exc:
        return HTMLResponse(
            content=_render_invalid_page(exc.detail),
            status_code=exc.status_code,
        )

    return HTMLResponse(
        content=_render_checkout_page(detail, request, payable=payable),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Shared logic
# ---------------------------------------------------------------------------

def _load_and_open(link_id: str) -> CheckoutDetail:
    """
    Load a PaymentLink, enforce expiry/state rules, and transition
    created -> opened. Returns CheckoutDetail on success.
    Raises HTTPException on any invalid state.
    """
    with get_conn() as conn:
        # open_link is idempotent: created -> opened if eligible; no-op otherwise.
        link = open_link(conn, link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="Payment link not found")
        name = merchant_name_for(conn, link.merchant_id)

    # Enforce: expired links must not render transaction details.
    if link.is_expired and link.status not in ("paid", "settling", "logged"):
        raise HTTPException(
            status_code=410,
            detail="This payment link has expired and is no longer valid.",
        )

    # Enforce: terminal statuses must not render a live checkout.
    if link.status in ("paid", "settling", "logged"):
        raise HTTPException(
            status_code=410,
            detail="This payment has already been completed.",
        )

    if link.status == "failed":
        raise HTTPException(
            status_code=410,
            detail="This payment link is no longer valid.",
        )

    log.info("checkout_opened", link_id=link.link_id, status=link.status)

    return _to_detail(link, name)


def _load_for_page(link_id: str) -> tuple[CheckoutDetail, bool]:
    """
    Load a link for the HTML page without rejecting terminal states.

    Returns (detail, payable). `payable` is False for anything the pay endpoint
    would refuse, so the page can hide the form instead of offering a button
    that will 410.
    """
    with get_conn() as conn:
        link = open_link(conn, link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="Payment link not found")
        name = merchant_name_for(conn, link.merchant_id)

    state = buyer_state(link)
    # Must match services/checkout/pay.py exactly, or the page would offer a
    # button that the server then refuses.
    payable = (not link.is_expired) and link.status in ("created", "opened")

    log.info("checkout_page_viewed", link_id=link.link_id, state=state, payable=payable)

    return _to_detail(link, name), payable


def _to_detail(link, merchant_name: str | None) -> CheckoutDetail:
    return CheckoutDetail(
        link_id=link.link_id,
        description=link.description,
        amount_minor_units=link.amount_minor_units,
        currency=link.currency,
        status=link.status,
        expires_at=link.expires_at.isoformat(),
        state=buyer_state(link),
        merchant_name=merchant_name,
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
#
# Plain HTML + vanilla JS, no build step, no framework, no third-party assets.
# Buyers open this from a WhatsApp link on a phone, often on mobile data, in
# whatever browser WhatsApp hands them. That drives every decision below:
# inline everything (no extra round trips), no web fonts, 54px+ tap targets,
# 16px inputs (stops iOS zoom-on-focus), and a layout that works at 320px.
#
# The palette is ConFam's (deep green, dark surfaces, one bright accent) so the
# page a buyer pays on is recognisably the same product as the site that sold
# them the thing. See landing/src/styles/site.css for the tokens.

# Stellar/Lobstr is deliberately absent from this page.
# OQ-023  — whether Lobstr accepts an external SEP-24 interactive URL handed to
#           it from a third-party checkout page is untested vendor behaviour;
#           protocol documentation does not answer it.
# OQ-024  — which real anchor provides a usable NGN off-ramp on acceptable
#           terms is unfinished vendor due diligence.
# Both must close before a "Pay with Stellar" button is designed, so there is no
# placeholder row for one here. The "coming soon" line this replaced was worse
# than nothing: it promised a button that was never coming.


# Currency symbols shown to the buyer. "GH₵" (not a bare cedis sign) matches
# what the merchant sees in the WhatsApp confirmation, so the two surfaces of
# the same sale never disagree about the amount.
CURRENCY_SYMBOLS = {"NGN": "₦", "GHS": "GH₵"}


def _format_amount(amount_minor_units: int, currency: str) -> str:
    """
    Format minor units for display.

    Integer arithmetic on purpose: float(1234567)/100 can render as
    12,345.67 but 123456789/100 does not round-trip cleanly, and Rule 6 says
    no floats in monetary values — that holds for the string a buyer reads too.
    """
    symbol = CURRENCY_SYMBOLS.get(currency, "")
    whole, minor = divmod(int(amount_minor_units), 100)
    grouped = f"{whole:,}"
    if minor:
        return f"{symbol}{grouped}.{minor:02d}"
    return f"{symbol}{grouped}"


# Shared stylesheet for both buyer-facing pages. A plain module-level string
# (not an f-string) so the CSS braces need no doubling, and one definition
# instead of two that can drift apart.
#
# Deliberately no @font-face and no web-font link: a buyer opens this from a
# WhatsApp link on mobile data, and a font request that stalls is a payment
# page that stalls. System fonts on the platform, ConFam's palette.
_CHECKOUT_CSS = """
    :root {
      color-scheme: dark;
      --green: #1db974;
      --green-dark: #17a065;
      --green-glow: rgba(29, 185, 116, 0.14);
      --surface: #111f17;
      --raised: #172b20;
      --ink: #0b1812;
      --border: rgba(255, 255, 255, 0.09);
      --border-light: rgba(255, 255, 255, 0.16);
      --white: #ffffff;
      --text: #eef5f0;
      --text-soft: #9bbfaa;
      --text-faint: #6b9279;
      --danger-bg: rgba(248, 113, 113, 0.12);
      --danger-border: rgba(248, 113, 113, 0.4);
      --danger-text: #fca5a5;
    }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      max-width: 480px; margin: 0 auto; padding: 20px 18px 48px;
      color: var(--text); background: var(--surface); line-height: 1.5;
      -webkit-text-size-adjust: 100%;
    }
    .topbar {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 18px;
    }
    .wordmark {
      font-size: 1.15rem; font-weight: 800; letter-spacing: -0.02em; color: var(--white);
    }
    .wordmark .dot { color: var(--green); }
    .secure {
      font-size: 0.75rem; font-weight: 600; color: var(--text-faint);
      display: inline-flex; align-items: center; gap: 0.35rem;
    }
    .secure::before {
      content: ""; width: 0.4rem; height: 0.4rem; border-radius: 50%;
      background: var(--green);
    }
    .card {
      background: var(--raised); border: 1px solid var(--border-light);
      border-radius: 14px; padding: 1.1rem 1.2rem 1.3rem;
    }
    .merchant-row { display: flex; align-items: center; gap: 0.7rem; }
    .avatar {
      width: 2.2rem; height: 2.2rem; border-radius: 50%; flex: none;
      background: var(--green); color: var(--ink);
      display: grid; place-items: center; font-weight: 800; font-size: 0.9rem;
    }
    .merchant { color: var(--white); font-size: 0.95rem; font-weight: 700; margin: 0; }
    .verified { color: var(--text-faint); font-size: 0.78rem; margin: 0.1rem 0 0; }
    .amount {
      font-size: 2.4rem; font-weight: 800; margin: 1rem 0 0.2rem;
      letter-spacing: -0.02em; color: var(--white);
    }
    .description { color: var(--text-soft); margin: 0; font-size: 0.95rem; }
    label {
      display: block; font-weight: 600; font-size: 0.95rem;
      color: var(--white); margin: 22px 0 6px;
    }
    .hint { font-weight: 400; color: var(--text-faint); font-size: 0.85rem; }
    input[type=email] {
      width: 100%; padding: 15px 14px; font-size: 16px;
      border: 1px solid var(--border-light); border-radius: 10px;
      margin-bottom: 18px; background: var(--raised); color: var(--text);
      font-family: inherit;
    }
    input[type=email]::placeholder { color: var(--text-faint); }
    input[type=email]:focus {
      outline: 2px solid var(--green); outline-offset: 1px; border-color: var(--green);
    }
    input[type=email]:disabled { opacity: 0.6; }
    .method { margin-bottom: 10px; }
    .btn {
      display: block; width: 100%; padding: 16px 15px;
      font-size: 1rem; font-weight: 700; font-family: inherit;
      border: 1.5px solid transparent; border-radius: 10px; cursor: pointer;
      background: var(--green); color: var(--ink); text-align: center;
      min-height: 54px;
    }
    .btn:hover:not(:disabled) { background: var(--green-dark); }
    .btn:focus-visible { outline: 3px solid rgba(29, 185, 116, 0.45); outline-offset: 2px; }
    .btn.secondary {
      background: transparent; color: var(--white); border-color: var(--border-light);
    }
    .btn.secondary:hover:not(:disabled) {
      background: var(--green-glow); border-color: var(--green); color: var(--white);
    }
    .btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .method-hint {
      color: var(--text-faint); font-size: 0.8rem; margin: 0.35rem 0 0; text-align: center;
    }
    .err {
      background: var(--danger-bg); border: 1px solid var(--danger-border);
      color: var(--danger-text); padding: 12px 14px; border-radius: 10px;
      margin-top: 16px; font-size: 0.92rem;
    }
    .err[hidden] { display: none; }
    .status {
      margin-top: 20px; padding: 22px 18px; border-radius: 14px; text-align: center;
      background: var(--raised); border: 1px solid var(--border-light);
    }
    .status h2 { margin: 0 0 8px; font-size: 1.15rem; color: var(--white); }
    .status p { margin: 0; color: var(--text-soft); font-size: 0.95rem; }
    .status .mark { font-size: 1.6rem; line-height: 1; margin-bottom: 10px; }
    .status.logged { background: var(--green-glow); border-color: rgba(29, 185, 116, 0.4); }
    .status.logged .mark { color: var(--green); }
    .status.failed, .status.expired {
      background: var(--danger-bg); border-color: var(--danger-border);
    }
    .spinner {
      width: 22px; height: 22px; margin: 0 auto 14px;
      border: 2.5px solid rgba(29, 185, 116, 0.25); border-top-color: var(--green);
      border-radius: 50%; animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
    .foot {
      margin: 22px 0 0; font-size: 0.8rem; color: var(--text-faint); text-align: center;
    }
    .foot code { font-size: 0.78rem; }
    .settles {
      margin: 14px 0 0; font-size: 0.82rem; color: var(--text-soft); text-align: center;
      background: var(--raised); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px 14px;
    }
  """


# Payment methods offered on the page, as (method, button label, hint).
# Card leads because it is the only channel that settles instantly everywhere.
# Labels are kept in sync with METHOD_CHANNELS in services/checkout/pay.py —
# a button that names a channel the server does not accept is a dead end.
_METHOD_ROWS: tuple[tuple[str, str, str], ...] = (
    ("card", "Pay with card", "Visa, Mastercard, Verve"),
    ("bank_transfer", "Pay with bank transfer", "Transfer straight from your bank app"),
    ("bank", "Pay with bank / OPay", "Pay-with-Bank, including OPay"),
    ("ussd", "Pay with USSD", "Dial a code on your phone"),
)


def _offered_methods(currency: str) -> list[tuple[str, str, str]]:
    """
    The methods to render for this link's currency.

    USSD is a Nigerian rail — Paystack does not offer it on GHS transactions —
    so a Ghanaian buyer is never shown a button that can only fail. Every other
    channel is offered to everyone.
    """
    if currency == "NGN":
        return list(_METHOD_ROWS)
    return [row for row in _METHOD_ROWS if row[0] != "ussd"]


def _render_method(method: str, label: str, hint: str) -> str:
    """One method button plus its hint line."""
    variant = "" if method == "card" else " secondary"
    return (
        f'        <div class="method">\n'
        f'          <button class="btn{variant}" type="submit" '
        f'data-method="{method}">{label}</button>\n'
        f'          <p class="method-hint">{hint}</p>\n'
        f"        </div>"
    )


def _api_base(request: Request, link_id: str) -> str:
    """
    Absolute path of this link's API namespace, e.g. "/pay/<link_id>".

    Derived from the request rather than hardcoded, because the checkout app is
    mounted two different ways: standalone at /{link_id} and, in the unified
    Render app, at /pay/{link_id}. Slicing the actual path is correct in both
    without either mount having to tell the renderer where it lives.
    """
    path = request.url.path
    if path.endswith(link_id):
        return path
    return f"/pay/{link_id}"


def _render_checkout_page(
    detail: CheckoutDetail, request: Request, *, payable: bool
) -> str:
    amount_display = _format_amount(detail.amount_minor_units, detail.currency)
    merchant_display = detail.merchant_name or "this merchant"
    avatar = (merchant_display.strip()[:1] or "C").upper()
    api = _api_base(request, detail.link_id)

    methods_html = "\n".join(
        _render_method(method, label, hint)
        for method, label, hint in _offered_methods(detail.currency)
    )

    # Embedded as JSON, not interpolated raw: a link_id is a UUID so this is
    # belt-and-braces, but the escaping below is what stops a description
    # containing "</script>" from breaking out of the block if the page is ever
    # templated from less-controlled data.
    bootstrap = json.dumps(
        {
            "apiBase": api,
            "state": detail.state,
            "payable": payable,
        }
    ).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#0b1812">
  <title>ConFam — Pay {amount_display}</title>
  <style>{_CHECKOUT_CSS}</style>
</head>
<body>
  <main id="main">
    <div class="topbar">
      <span class="wordmark">ConFam<span class="dot">.</span></span>
      <span class="secure">Secure payment</span>
    </div>

    <div class="card">
      <div class="merchant-row">
        <span class="avatar" aria-hidden="true">{_escape(avatar)}</span>
        <div>
          <p class="merchant">Paying {_escape(merchant_display)}</p>
          <p class="verified">This payment settles to their own bank account</p>
        </div>
      </div>
      <p class="amount">{amount_display}</p>
      <p class="description">{_escape(detail.description)}</p>
    </div>

    <div id="error" class="err" role="alert" hidden></div>

    <form id="pay-form" novalidate>
      <label for="email">
        Your email
        <span class="hint">— for your receipt</span>
      </label>
      <!-- type=email gives the phone keyboard an @ key. novalidate means we
           run our own messages instead of the browser's, which are terse and
           inconsistent across platforms, and the server re-validates anyway. -->
      <input
        type="email" id="email" name="email" required
        autocomplete="email" inputmode="email"
        autocapitalize="off" autocorrect="off" spellcheck="false"
        placeholder="you@example.com"
      >
      <div id="methods">
{methods_html}
      </div>
    </form>

    <p class="settles">
      ConFam never holds your money. This payment goes straight to
      {_escape(merchant_display)}'s verified bank account.
    </p>

    <p class="foot">
      You will be taken to Paystack to enter your card details.<br>
      Reference <code>{_escape(detail.link_id)}</code>
    </p>
  </main>

  <script>
  (function () {{
    "use strict";

    var BOOT = {bootstrap};
    var POLL_INTERVAL_MS = 3000;
    // ~5 minutes. Bank transfers can take a while to clear, and a buyer left
    // staring at a spinner forever is worse than one told to check with the
    // merchant. The link itself expires after 30 min.
    var POLL_MAX_MS = 5 * 60 * 1000;
    var MAX_EMAIL_LEN = 254;
    // Mirrors the server check in services/checkout/pay.py. The server is the
    // real gate; this only avoids a pointless round trip on an obvious typo.
    var EMAIL_RE = /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/;

    var form = document.getElementById("pay-form");
    var methods = document.getElementById("methods");
    var emailInput = document.getElementById("email");
    var errorBox = document.getElementById("error");
    var main = document.getElementById("main");
    var pollTimer = null;

    function showError(message) {{
      errorBox.textContent = message;
      errorBox.hidden = false;
    }}

    function clearError() {{
      errorBox.textContent = "";
      errorBox.hidden = true;
    }}

    function setBusy(busy) {{
      var buttons = methods.querySelectorAll("button");
      for (var i = 0; i < buttons.length; i++) {{ buttons[i].disabled = busy; }}
      emailInput.disabled = busy;
    }}

    function showStatus(state, title, message) {{
      if (pollTimer) {{ clearTimeout(pollTimer); pollTimer = null; }}
      if (form) {{ form.remove(); }}
      var foot = document.querySelector(".foot");
      if (foot) {{ foot.remove(); }}
      var settles = document.querySelector(".settles");
      if (settles) {{ settles.remove(); }}

      var div = document.createElement("div");
      div.className = "status " + state;
      if (state === "waiting") {{
        var spinner = document.createElement("div");
        spinner.className = "spinner";
        div.appendChild(spinner);
      }} else {{
        var mark = document.createElement("div");
        mark.className = "mark";
        mark.textContent = STATE_MARKS[state] || "";
        if (mark.textContent) {{ div.appendChild(mark); }}
      }}
      var h = document.createElement("h2");
      h.textContent = title;
      var p = document.createElement("p");
      p.textContent = message;
      div.appendChild(h);
      div.appendChild(p);
      main.appendChild(div);
      clearError();
    }}

    // A status glyph the buyer reads at a glance before the words. Text, not
    // emoji, so it renders identically on every phone.
    var STATE_MARKS = {{ logged: "✓", failed: "✕", expired: "—" }};

    // The polled status is the ONLY thing this page treats as proof of payment.
    // The reference/trxref parameters Paystack appends to the callback URL are
    // not evidence of anything: they came back on a URL the buyer controls, so
    // anyone holding the link could have added them. Only the webhook writes
    // the link status this endpoint reports.
    var STATE_COPY = {{
      waiting: {{
        title: "Confirming your payment...",
        message: "This page will update by itself. You can close it if you like."
      }},
      logged: {{
        title: "Payment received",
        message: "Thank you! The merchant has been notified and your receipt "
                 + "is on its way by email."
      }},
      failed: {{
        title: "Payment did not go through",
        message: "You have not been charged. Please contact the merchant for a new payment link."
      }},
      expired: {{
        title: "This link has expired",
        message: "Ask the merchant for a new payment link. If you were charged, "
                 + "contact them — they can confirm it."
      }}
    }};

    function renderState(state) {{
      var copy = STATE_COPY[state] || STATE_COPY.waiting;
      showStatus(state, copy.title, copy.message);
    }}

    function giveUp() {{
      showStatus(
        "expired",
        "Still confirming",
        "This is taking longer than usual. If you were charged, the merchant " +
        "will confirm it — please do not pay again."
      );
    }}

    function poll(deadline) {{
      fetch(BOOT.apiBase + "/status", {{ headers: {{ "Accept": "application/json" }} }})
        .then(function (r) {{
          if (!r.ok) {{ throw new Error("status " + r.status); }}
          return r.json();
        }})
        .then(function (data) {{
          if (data.state !== "waiting") {{
            renderState(data.state);
            return;
          }}
          if (Date.now() >= deadline) {{ giveUp(); return; }}
          pollTimer = setTimeout(function () {{ poll(deadline); }}, POLL_INTERVAL_MS);
        }})
        .catch(function () {{
          // A failed poll is not a failed payment. Keep retrying quietly rather
          // than telling the buyer something that might strand them into
          // paying twice.
          if (Date.now() >= deadline) {{ giveUp(); return; }}
          pollTimer = setTimeout(function () {{ poll(deadline); }}, POLL_INTERVAL_MS);
        }});
    }}

    function startPolling() {{
      renderState("waiting");
      poll(Date.now() + POLL_MAX_MS);
    }}

    form.addEventListener("submit", function (event) {{
      event.preventDefault();
      clearError();

      var button = event.submitter;
      var method = button ? button.getAttribute("data-method") : null;
      var email = emailInput.value.trim();

      if (!method) {{
        showError("Please choose how you would like to pay.");
        return;
      }}
      if (!email) {{
        showError("Please enter your email address so we can send your receipt.");
        emailInput.focus();
        return;
      }}
      if (email.length > MAX_EMAIL_LEN) {{
        showError("That email address is too long.");
        emailInput.focus();
        return;
      }}
      if (!EMAIL_RE.test(email)) {{
        showError("That does not look like a valid email address.");
        emailInput.focus();
        return;
      }}

      setBusy(true);
      var original = button ? button.textContent : null;
      if (button) {{ button.textContent = "Taking you to Paystack..."; }}

      fetch(BOOT.apiBase + "/pay", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify({{ method: method, email: email }})
      }})
        .then(function (r) {{
          return r.json().catch(function () {{ return {{}}; }})
            .then(function (body) {{ return {{ ok: r.ok, status: r.status, body: body }}; }});
        }})
        .then(function (result) {{
          if (result.ok && result.body && result.body.authorization_url) {{
            window.location.assign(result.body.authorization_url);
            return;
          }}
          setBusy(false);
          if (button && original) {{ button.textContent = original; }}

          // FastAPI puts our message in "detail"; a 422 from body validation
          // puts the per-field reason in "detail" as a list of objects.
          var detail = result.body && result.body.detail;
          if (Array.isArray(detail) && detail.length) {{
            showError(detail[0].msg || "Please check your details and try again.");
          }} else if (typeof detail === "string") {{
            showError(detail);
          }} else if (result.status === 429) {{
            showError("Too many attempts. Please wait a minute and try again.");
          }} else {{
            showError("We could not start that payment. Please try again.");
          }}
        }})
        .catch(function () {{
          setBusy(false);
          if (button && original) {{ button.textContent = original; }}
          showError("No connection to ConFam. Check your network and try again.");
        }});
    }});

    if (!BOOT.payable && BOOT.state !== "waiting") {{
      // Unpayable link opened directly (expired or already handled): show the
      // state instead of a form whose buttons would only return 410.
      renderState(BOOT.state);
    }} else if (new URLSearchParams(window.location.search).has("reference") ||
               new URLSearchParams(window.location.search).has("trxref") ||
               new URLSearchParams(window.location.search).has("redirect")) {{
      // Buyer came back from Paystack. Note we do not read the values, only
      // the fact that Paystack redirected — the status poll decides the truth.
      startPolling();
    }}
  }})();
  </script>
</body>
</html>"""


def _render_invalid_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#0b1812">
  <title>ConFam — Link unavailable</title>
  <style>{_CHECKOUT_CSS}</style>
</head>
<body>
  <main id="main">
    <div class="topbar">
      <span class="wordmark">ConFam<span class="dot">.</span></span>
    </div>
    <div class="status expired">
      <div class="mark">—</div>
      <h2>Link unavailable</h2>
      <p>{_escape(message)}</p>
    </div>
    <p class="settles">
      If you believe this is an error, contact the merchant who sent you this
      link — they can send you a new one.
    </p>
  </main>
</body>
</html>"""


def _escape(text: str) -> str:
    """Minimal HTML escaping to prevent XSS in rendered output."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
