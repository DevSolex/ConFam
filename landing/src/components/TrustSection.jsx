export default function TrustSection() {
  return (
    <section className="section band" id="trust">
      <div className="contain">
        <h2 className="section-heading">Why sellers can trust it</h2>
        <p className="soft" style={{ marginTop: "0.5rem" }}>
          Three guarantees that hold whether you take one order a week or a
          hundred a day.
        </p>

        <div className="trust">
          <div className="trust-card">
            <h3>ConFam never holds your money</h3>
            <p>
              Every payment routes to your own verified bank account. There is no
              intermediate ConFam balance for a payment to sit in.
            </p>
          </div>
          <div className="trust-card">
            <h3>A duplicate confirmation counts once</h3>
            <p>
              Confirmations are idempotent on the payment provider's event id. If
              a webhook fires twice, your ledger still shows one sale.
            </p>
          </div>
          <div className="trust-card">
            <h3>The ledger only moves forward</h3>
            <p>
              Amounts are stored as whole-number kobo in an append-only ledger.
              Nothing can be rewritten after the fact.
            </p>
          </div>
        </div>

        <div className="stat-row" role="list">
          <div className="stat" role="listitem">
            <span className="num">0</span>
            <span className="lbl">naira held by ConFam</span>
          </div>
          <div className="stat" role="listitem">
            <span className="num">1</span>
            <span className="lbl">record per payment</span>
          </div>
          <div className="stat" role="listitem">
            <span className="num">kobo</span>
            <span className="lbl">whole-number amounts, no decimals</span>
          </div>
        </div>

        <div className="example-card">
          <div className="card-head">
            Payment confirmed <span className="tag-example">Example</span>
          </div>
          <p>
            You've been paid ₦2,500 for the Jordan order — settled to your
            verified account.
          </p>
          <p className="sub">
            The buyer paid through the link you sent in chat. No screenshot
            involved.
          </p>
          <div className="checks">
            <span>one ledger record</span>
            <span>whole kobo</span>
            <span>WhatsApp notification sent</span>
          </div>
        </div>

        <p className="soft" style={{ marginTop: "1.5rem", fontSize: "0.95rem" }}>
          <em>Later, we hope:</em> your own sales history as evidence when you
          talk to a lender.
        </p>
      </div>
    </section>
  );
}
