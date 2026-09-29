import PhoneMockup from "./PhoneMockup.jsx";

export default function FinalCta() {
  return (
    <section className="section band cta" id="join">
      <div className="split contain">
        <div>
          <PhoneMockup variant="ledger" />
        </div>
        <div>
          <h2 className="section-heading">Join the private pilot.</h2>
          <p>
            Tell us how you sell on WhatsApp, and we'll set up your account
            during the pilot. It's free — that's the whole point.
          </p>
          <a
            className="btn arrow"
            href="https://wa.me/2348051338460?text=Hi%2C%20I%27d%20like%20to%20join%20the%20ConFam%20pilot"
            target="_blank"
            rel="noopener noreferrer"
          >
            Message us on WhatsApp
          </a>
        </div>
      </div>
    </section>
  );
}
