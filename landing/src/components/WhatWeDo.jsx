export default function WhatWeDo() {
  return (
    <section className="section surface-raised" id="what-we-do">
      <div className="split contain">
        <div className="split-text">
          <span className="section-label">What we do</span>
          <h2 className="section-heading">
            Your chat is already the shop.<br />We made it the checkout.
          </h2>
          <p>
            Nigerian sellers close deals in WhatsApp every day, then lose an
            hour chasing transfer screenshots. ConFam keeps the conversation
            exactly where it is and handles the awkward half — the link, the
            confirming, and the recording.
          </p>
          <ul className="bullets">
            <li>Type PAY in the ConFam chat — get a one-time payment link back in seconds</li>
            <li>Your buyer pays by card or bank transfer on Paystack's page</li>
            <li>Payment confirms against Paystack's record, never a screenshot</li>
            <li>Money settles straight to your own verified bank account</li>
            <li>Every sale becomes one permanent record in your ledger</li>
          </ul>
        </div>

        {/* Real chat demo — no SVG */}
        <div className="chat-demo">
          <div className="chat-demo-header">
            <div className="chat-demo-avatar">CF</div>
            <div>
              <div className="chat-demo-name">ConFam</div>
              <div className="chat-demo-status">online</div>
            </div>
          </div>
          <div className="chat-demo-body">
            <div className="msg sent">REGISTER Adaeze Fashion Store <span className="tick">✓✓</span></div>
            <div className="msg recv">
              ✅ Business registered!<br />
              <span style={{fontSize:"0.82rem", color:"#555"}}>Next: ONBOARD to set up your payout account.</span>
            </div>
            <div className="msg sent">ONBOARD 0123456789 GTBank <span className="tick">✓✓</span></div>
            <div className="msg recv">
              ✅ Payout account set up!<br />
              <span style={{fontSize:"0.82rem", color:"#555"}}>You're ready. Send PAY to take your first payment.</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
