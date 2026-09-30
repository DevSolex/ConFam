import { WHATSAPP_URL } from "../site.js";

export default function Hero() {
  return (
    <section className="hero contain" id="top">
      <div className="hero-grid">
        {/* Left: headline + CTA */}
        <div>
          <span className="hero-eyebrow">Live in Nigeria &amp; Ghana</span>
          <h1>
            Sell on WhatsApp.<br />
            Get paid without <span className="accent">the screenshot.</span>
          </h1>
          <p className="hero-sub">
            ConFam turns a WhatsApp sale into a one-time payment link.
            Your buyer pays by card or bank transfer. The money lands in
            your own verified bank account — confirmed in seconds, recorded
            forever.
          </p>
          <div className="hero-actions">
            <a
              className="btn arrow"
              href={WHATSAPP_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Start on WhatsApp
            </a>
            <a className="btn secondary" href="#how">
              See how it works
            </a>
          </div>
        </div>

        {/* Right: real stat cards — no SVG mockup */}
        <div className="hero-stats">
          {/* Confirmation card */}
          <div className="stat-card">
            <div className="stat-card-label">Latest payment</div>
            <div className="stat-card-value">
              <span className="currency">₦</span>2,500
            </div>
            <div className="stat-card-meta">Jordan — 1 pair sneakers</div>
            <div className="stat-card-badge">Confirmed by Paystack</div>
          </div>

          {/* Today's sales */}
          <div className="stat-card wide">
            <div className="stat-item">
              <div className="stat-item-num">₦6,800</div>
              <div className="stat-item-lbl">Collected today</div>
            </div>
            <div className="stat-item">
              <div className="stat-item-num">3</div>
              <div className="stat-item-lbl">Orders — 1 record each</div>
            </div>
          </div>

          {/* Chat preview */}
          <div className="chat-demo">
            <div className="chat-demo-header">
              <div className="chat-demo-avatar">CF</div>
              <div>
                <div className="chat-demo-name">ConFam</div>
                <div className="chat-demo-status">online</div>
              </div>
            </div>
            <div className="chat-demo-body">
              <div className="msg sent">PAY 2500 Jordan <span className="tick">✓✓</span></div>
              <div className="msg recv">
                Payment link created ✅<br />
                <span style={{fontSize:"0.82rem", color:"#555"}}>pay.confam.co/a1b2c3 · expires 30 min</span>
              </div>
              <div className="msg recv">
                Payment received ✅<br />
                <span className="amount">₦2,500</span> — settled to your account.
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
