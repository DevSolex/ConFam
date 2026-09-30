import { WHATSAPP_URL } from "../site.js";

export default function FinalCta() {
  return (
    <section className="section cta-band" id="join">
      <div className="contain" style={{textAlign:"center"}}>
        <span className="section-label" style={{display:"block", textAlign:"center"}}>
          Get started
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
          Message us on WhatsApp and you can take your first confirmed payment
          in the next few minutes. No app to install, no store to build. Free
          to start.
        </p>
        <div className="cta-actions">
          <a
            className="btn arrow"
            href={WHATSAPP_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            Start now
          </a>
          <a className="btn secondary" href="#how">
            See how it works
          </a>
        </div>
      </div>
    </section>
  );
}