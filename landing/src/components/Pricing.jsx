const WA_URL = "https://wa.me/2348051338460?text=Hi%2C%20I%27d%20like%20to%20join%20the%20ConFam%20pilot";

export default function Pricing() {
  return (
    <section className="section" id="pricing">
      <div className="contain">
        <span className="section-label">Pricing</span>
        <h2 className="section-heading">
          Plans that scale with<br />your sales, not your stress.
        </h2>
        <p className="section-sub">
          Free while we learn together. We'll agree pricing with you before
          charging anything.
        </p>

        <div className="pricing-grid">
          {/* Pilot — free */}
          <div className="price-card featured">
            <div>
              <div className="price-badge">Current plan</div>
              <div className="price-tier">Pilot</div>
              <div className="price-tagline">Everything you need to take your first WhatsApp payment.</div>
            </div>
            <div className="price-amount">
              ₦0 <span className="period">/ month</span>
            </div>
            <ul className="price-features">
              <li>Unlimited confirmed orders during the pilot</li>
              <li>WhatsApp PAY command — one link per sale</li>
              <li>Auto-confirmed bank transfers via Paystack</li>
              <li>Naira ledger — every sale recorded permanently</li>
              <li>PDF sales statement on demand (LEDGER command)</li>
              <li>Nigeria &amp; Ghana supported</li>
            </ul>
            <a
              className="btn arrow"
              href={WA_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Join the pilot — free
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
              <li>Everything in Pilot</li>
              <li>Daily automatic reconciliation</li>
              <li>CSV export for your accountant</li>
              <li>Priority WhatsApp support</li>
            </ul>
            <a
              className="btn secondary"
              href={WA_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Join the waitlist
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
              href={WA_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Message us on WhatsApp
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
