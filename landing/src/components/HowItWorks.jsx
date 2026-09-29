export default function HowItWorks() {
  return (
    <section className="section surface-raised" id="how">
      <div className="contain">
        <span className="section-label">How it works</span>
        <h2 className="section-heading">
          From chat to confirmed sale,<br />in three steps.
        </h2>
        <p className="section-sub">
          Nothing new to learn, nothing to install. You keep selling the way you already sell.
        </p>

        <ol className="steps">
          <li>
            <h3>Type PAY in the ConFam chat</h3>
            <p>
              Send <strong style={{color:"var(--white)"}}>PAY 2500 Jordan</strong> to
              your ConFam number. You get a one-time payment link back in seconds — one per sale.
            </p>
          </li>
          <li>
            <h3>Paste the link to your buyer</h3>
            <p>
              Drop the link into your normal WhatsApp chat with the buyer.
              They open it and pay by card or bank transfer on Paystack's page —
              no app to install.
            </p>
          </li>
          <li>
            <h3>Both of you get confirmation</h3>
            <p>
              ConFam checks the payment against Paystack's own record — never a
              screenshot. You get a WhatsApp notification, the money settles to
              your verified account, and the sale is recorded permanently.
            </p>
          </li>
        </ol>
      </div>
    </section>
  );
}
