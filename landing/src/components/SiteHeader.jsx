import { WHATSAPP_URL } from "../site.js";

export default function SiteHeader() {
  return (
    <header className="site-header">
      <nav className="nav contain" aria-label="Primary">
        <a className="wordmark" href="#top">
          ConFam<span className="dot">.</span>
        </a>

        <ul className="nav-links" role="list">
          <li><a href="#features">Features</a></li>
          <li><a href="#how">How it works</a></li>
          <li><a href="#pricing">Pricing</a></li>
        </ul>

        <div className="nav-cta">
          <a className="btn" href={WHATSAPP_URL} target="_blank" rel="noopener noreferrer">
            Start now
          </a>
        </div>
      </nav>
    </header>
  );
}
