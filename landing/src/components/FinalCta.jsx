const WA_URL = "https://wa.me/2348051338460?text=Hi%2C%20I%27d%20like%20to%20join%20the%20ConFam%20pilot";

export default function FinalCta() {
  return (
    <section className="section cta-band" id="join">
      <div className="contain" style={{textAlign:"center"}}>
        <span className="section-label" style={{display:"block", textAlign:"center"}}>
          Join the private pilot
        </span>
        <h2 className="section-heading" style={{maxWidth:"36rem", margin:"0 auto"}}>
          Start selling on WhatsApp today.
        </h2>
        <p style={{
          color:"var(--text-soft)",
          fontSize:"1.05rem",
          maxWidth:"38rem",
          margin:"0.9rem auto 0",
          lineHeight:1.7
        }}>
          Message the bot and you can take your first confirmed payment in the
          next few minutes. No app to install, no store to build. Free during
          the pilot.
        </p>
        <div className="cta-actions">
          <a
            className="btn arrow"
            href={WA_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            Message us on WhatsApp
          </a>
          <a className="btn secondary" href="#how">
            See how it works
          </a>
        </div>
      </div>
    </section>
  );
}
