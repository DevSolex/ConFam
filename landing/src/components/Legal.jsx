import { COMPANY_NAME } from "../site.js";

export function LegalHeader() {
  return (
    <header className="legal-header">
      <nav className="contain" aria-label="Primary">
        <a className="wordmark" href="/">
          ConFam<span className="dot">.</span>
        </a>
      </nav>
    </header>
  );
}

export function DraftBanner() {
  return (
    <p className="draft-banner">DRAFT: needs legal review before public launch.</p>
  );
}

export function LegalFooter() {
  return (
    <footer className="site-footer">
      <div className="contain">
        <div className="footer-legal" style={{ marginTop: 0 }}>
          <span>
            © 2026 {COMPANY_NAME}
          </span>
          <span>
            <a href="/privacy">Privacy policy</a>{" "}
            <a href="/terms">Terms of service</a>
          </span>
        </div>
      </div>
    </footer>
  );
}
