import PhoneMockup from "./PhoneMockup.jsx";

export default function WhatWeDo() {
  return (
    <section className="section paper-bg" id="what-we-do">
      <div className="split contain">
        <div>
          <h2 className="section-heading">What we do</h2>
          <p>
            Sellers already close deals in WhatsApp every day. ConFam does the
            part that usually hurts: turning a sold item into a payment link,
            watching for the money, and confirming it without asking for a
            screenshot.
          </p>
          <p>
            Everything routes to the seller's own verified bank account. There is
            no ConFam balance and no custody step anywhere in the flow.
          </p>
          <ul className="bullets">
            <li>
              REGISTER, ONBOARD and PAY all happen inside the chat you already
              sell in.
            </li>
            <li>
              Every sale gets its own one-time payment link — a paid link can't
              be reused.
            </li>
            <li>
              Buyers pay by card or bank transfer through Paystack.
            </li>
            <li>
              Payments settle straight to your verified account; ConFam never
              holds funds.
            </li>
            <li>
              Confirmation arrives by WhatsApp, and duplicate payments never
              double-count.
            </li>
          </ul>
        </div>
        <div>
          <PhoneMockup variant="register-flow" />
        </div>
      </div>
    </section>
  );
}
