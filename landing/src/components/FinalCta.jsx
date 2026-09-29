export default function FinalCta() {
  return (
    <section className="section band cta" id="join">
      <div className="split contain">
        <div>
          {/* TODO: replace with real photo — a seller replying to a ConFam ONBOARD message on WhatsApp */}
          <img
            className="photo"
            src="/assets/img/placeholder-cta.svg"
            alt="A seller replying to a ConFam ONBOARD message on WhatsApp"
            width="900"
            height="675"
          />
        </div>
        <div>
          <h2 className="section-heading">Join the private pilot.</h2>
          <p>
            Tell us how you sell on WhatsApp, and we'll set up your account
            during the pilot. It's free — that's the whole point.
          </p>
          {/* REPLACE-WITH-YOUR-EMAIL: point this mailto at your real address before launch */}
          <a
            className="btn arrow"
            href="mailto:REPLACE-WITH-YOUR-EMAIL@example.com?subject=ConFam%20pilot"
          >
            Join the pilot
          </a>
        </div>
      </div>
    </section>
  );
}
