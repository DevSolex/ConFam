export default function HowItWorks() {
  return (
    <section className="section" id="how">
      <div className="contain">
        <h2 className="section-heading">How it works</h2>
        <p className="lead">
          Nothing to install and nothing new to learn. You keep selling the way
          you already sell.
        </p>

        <ol className="steps">
          <li>
            <h3>Type PAY in the chat</h3>
            <p>
              Text <strong>PAY 2500 Jordan</strong> to the ConFam account. You get
              back a payment link — one per sale — right away.
            </p>
          </li>
          <li>
            <h3>Buyer pays their way</h3>
            <p>
              The buyer opens the link and pays by card or bank transfer on
              Paystack's page. The amount and the account the seller set are what
              get paid — nothing else.
            </p>
          </li>
          <li>
            <h3>Both of you get confirmation</h3>
            <p>
              The payment confirms against Paystack's record. You get a WhatsApp
              "Payment received", the money settles to your verified bank account,
              and the sale becomes one kobo record in the ledger. No screenshot, no
              double count.
            </p>
          </li>
        </ol>
      </div>
    </section>
  );
}
