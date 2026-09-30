import { WHATSAPP_URL } from "../site.js";

export default function Pricing() {
  return (
    <section className="section" id="pricing">
      <div className="contain">
        <span className="section-label">Pricing</span>
        <h2 className="section-heading">
          Plans that scale with<br />your sales, not your stress.
        </h2>
        <p className="section-sub">
          Free to start taking payments. We'll agree any paid plan with you in
          writing before we charge anything.
        </p>

        <div className="pricing-grid">
          {/* Starter — free */}
          <div className="price-card featured">
            <div>
              <div className="price-badge">Current plan</div>
              <div className="price-tier">Starter</div>
              <div className="price-tagline">Everything you need to take your first WhatsApp payment.</div>
            </div>
            <div className="price-amount">
              ₦0 <span className="period">/ month</span>
            </div>
            <ul className="price-features">
              <li>Unlimited confirmed orders</li>
              <li>WhatsApp PAY command — one link per sale</li>
              <li>Auto-confirmed bank transfers via Paystack</li>
              <li>Naira ledger — every sale recorded permanently</li>
              <li>PDF sales statement on demand (LEDGER command)</li>
              <li>Nigeria &amp; Ghana supported</li>
            </ul>
            <a
              className="btn arrow"
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Start free on WhatsApp
            </a>
          </div>

          {/* Growth — coming soon */}
          <div className="price-card">
            <div>
              <div className="price-tier">Growth</div>
              <div className="price-tagline">For sellers watching the numbers and running both payment rails.</div>
            </div>
            <div className="price-amount">
              Coming soon
            </div>
            <ul className="price-features">
              <li>Everything in Starter</li>
              <li>Daily automatic reconciliation</li>
              <li>CSV export for your accountant</li>
              <li>Priority WhatsApp support</li>
            </ul>
            <a
              className="btn secondary"
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Get notified
            </a>
          </div>

          {/* Enterprise */}
          <div className="price-card">
            <div>
              <div className="price-tier">Enterprise</div>
              <div className="price-tagline">Running a co-operative, a marketplace, or many merchant lines?</div>
            </div>
            <div className="price-amount" style={{fontSize:"1.3rem"}}>
              Let's talk
            </div>
            <ul className="price-features">
              <li>Custom limits and reporting</li>
              <li>Multiple merchant lines</li>
              <li>Dedicated onboarding</li>
              <li>SLA and direct support</li>
            </ul>
            <a
              className="btn secondary"
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Talk to us
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
