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
          <a
            className="btn"
            href="https://wa.me/2348051338460?text=Hi%2C%20I%27d%20like%20to%20join%20the%20ConFam%20pilot"
            target="_blank"
            rel="noopener noreferrer"
          >
            Message us on WhatsApp
          </a>
        </div>
      </nav>
    </header>
  );
}
