export default function WhyWeBuiltIt() {
  return (
    <section className="section surface-raised" id="why-we-built-it">
      <div className="split reversed contain">
        <div className="split-text">
          <span className="section-label">Why we built it</span>
          <h2 className="section-heading">
            Built for the seller,<br />not the spreadsheet.
          </h2>
          <p>
            The screenshot problem is not a payments problem — it is a trust
            problem. A real transfer and an edited one look identical. So the
            seller ships and hopes, or makes a paying customer wait while she
            calls the bank.
          </p>
          <p>
            We started at the money and worked backwards. Payments confirm
            against Paystack's own event record — never against an image. Funds
            settle to the seller's own verified account. There is no ConFam
            balance in the middle for anything to get stuck in.
          </p>
        </div>

        {/* Confirmation card — real content, no SVG */}
        <div className="stat-card" style={{maxWidth:"26rem", margin:"0 auto"}}>
          <div className="stat-card-label">Payment confirmed · example</div>
          <div className="stat-card-value">
            <span className="currency">₦</span>12,500
          </div>
          <div className="stat-card-meta" style={{marginTop:"0.5rem"}}>
            Ada's Kitchen · 2 × jollof plate
          </div>
          <div style={{
            marginTop:"1rem",
            paddingTop:"1rem",
            borderTop:"1px solid rgba(255,255,255,0.08)",
            display:"flex",
            flexDirection:"column",
            gap:"0.4rem",
            fontSize:"0.85rem",
            color:"var(--text-soft)"
          }}>
            <span style={{color:"var(--green)"}}>✓ Confirmed by Paystack — not a screenshot</span>
            <span>✓ Settled to your verified bank account</span>
            <span>✓ One record written — cannot be edited</span>
          </div>
        </div>
      </div>
    </section>
  );
}
