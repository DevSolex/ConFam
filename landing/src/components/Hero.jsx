import PhoneMockup from "./PhoneMockup.jsx";

export default function Hero() {
  return (
    <section className="hero contain" id="top">
      <div className="hero-grid">
        <div>
          <h1>Every WhatsApp sale, paid straight to your bank.</h1>
          <p className="lead">
            ConFam turns a WhatsApp sale into a one-time payment link. Your buyer
            pays by card or bank transfer on Paystack, the money lands in your
            own verified bank account, and nobody ever asks for a screenshot.
          </p>
          <div className="hero-actions">
            <a
              className="btn arrow"
              href="https://wa.me/2348051338460?text=Hi%2C%20I%27d%20like%20to%20join%20the%20ConFam%20pilot"
              target="_blank"
              rel="noopener noreferrer"
            >
              Message us on WhatsApp
            </a>
            <a className="btn secondary" href="#how">
              See how it works
            </a>
          </div>
        </div>

        <div className="hero-media">
          <PhoneMockup variant="pay-flow" />

          {/* Example card (a): payment received notification */}
          <div className="float-card top">
            <div className="card-head">
              Payment received <span className="tag-example">Example</span>
            </div>
            <div className="chat">
              <div className="bubble buyer">
                <span className="mono">PAY 2500 Jordan</span>
              </div>
              <div className="bubble bot">Payment link created.</div>
              <div className="bubble bot">
                <span className="check">✓</span> Payment received — you've
                been paid ₦2,500.
              </div>
            </div>
          </div>

          {/* Example card (b): today's sales */}
          <div className="float-card bottom">
            <div className="card-head">
              Today's sales <span className="tag-example">Example</span>
            </div>
            <div className="ledger-rows">
              <div className="ledger-row">
                <span>PAY 2500 Jordan</span>
                <span className="v">₦2,500</span>
              </div>
              <div className="ledger-row">
                <span>PAY 900 wrapper</span>
                <span className="v">₦900</span>
              </div>
              <div className="ledger-row">
                <span>PAY 3400 ready-to-wear</span>
                <span className="v">₦3,400</span>
              </div>
            </div>
            <div className="ledger-foot">
              <span>3 payments</span>
              <span>1 record each</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
