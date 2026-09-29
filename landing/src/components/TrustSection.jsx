export default function TrustSection() {
  return (
    <section className="section" id="trust">
      <div className="contain">
        <span className="section-label">Why sellers can trust it</span>
        <h2 className="section-heading">
          The money moves<br />before you trust anyone.
        </h2>
        <p className="section-sub">
          Three guarantees that hold whether you take one order a week or a hundred a day.
        </p>

        <div className="trust-grid">
          <div className="trust-card">
            <h3>ConFam never holds your money</h3>
            <p>
              Every payment routes to your own verified bank account. There is
              no intermediate ConFam balance for a payment to sit in — it is
              structurally impossible, not just policy.
            </p>
          </div>
          <div className="trust-card">
            <h3>A duplicate confirmation counts once</h3>
            <p>
              Confirmations are idempotent on Paystack's event id. If a webhook
              fires twice, or a network retry delivers it again, your ledger
              still shows exactly one sale.
            </p>
          </div>
          <div className="trust-card">
            <h3>Books that only move forward</h3>
            <p>
              Amounts are stored as whole-number kobo in an append-only ledger.
              Nothing can be rewritten after the fact — not by support, not by
              an admin script, not by anyone.
            </p>
          </div>
        </div>

        <div className="metric-row">
          <div className="metric">
            <span className="metric-num">₦0</span>
            <span className="metric-lbl">held by ConFam — ever</span>
          </div>
          <div className="metric">
            <span className="metric-num">1</span>
            <span className="metric-lbl">ledger record per payment</span>
          </div>
          <div className="metric">
            <span className="metric-num">~2ms</span>
            <span className="metric-lbl">confirmation to ledger write</span>
          </div>
        </div>
      </div>
    </section>
  );
}
