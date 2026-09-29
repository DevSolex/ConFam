import PhoneMockup from "./PhoneMockup.jsx";

export default function WhyWeBuiltIt() {
  return (
    <section className="section paper-bg" id="why-we-built-it">
      <div className="split reversed contain">
        <div>
          <h2 className="section-heading">Why we built it</h2>
          <p>
            A transfer screenshot asks a seller to trust a picture. A real
            payment and an edited one can look identical, so she either ships
            the goods and hopes, or she makes a genuine customer wait while she
            checks.
          </p>
          <p>
            So we made the payment confirm itself. Money is verified against
            Paystack's event record — the rail's own word, never a picture — and
            settles to the seller's own verified account. There is no ConFam
            balance where money stops on the way there.
          </p>
        </div>
        <div>
          <PhoneMockup variant="payment-received" />
        </div>
      </div>
    </section>
  );
}
