const columns = [
  {
    label: "Product",
    links: [
      { href: "#what-you-get", text: "What you get" },
      { href: "#what-we-do", text: "What we do" },
      { href: "#trust", text: "Trust & guarantees" },
      { href: "#built-on", text: "Built on" },
    ],
  },
  {
    label: "How it works",
    links: [
      { href: "#how", text: "Three steps" },
      { href: "#what-we-do", text: "What we do" },
      { href: "#pricing", text: "Pricing" },
    ],
  },
  {
    label: "Company",
    links: [
      { href: "#join", text: "Start now" },
      { href: "/privacy", text: "Privacy policy" },
      { href: "/terms", text: "Terms of service" },
    ],
  },
];

export default function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="contain">
        <div className="footer-grid">
          <div>
            <a className="wordmark" href="#top">
              ConFam<span className="dot">.</span>
            </a>
            <p className="footer-desc">
              ConFam turns a WhatsApp sale into a one-time payment link and
              settles it straight to the seller's verified bank account.
            </p>
          </div>

          {columns.map(({ label, links }) => (
            <nav className="footer-col" aria-label={label} key={label}>
              <h3>{label}</h3>
              <ul>
                {links.map(({ href, text }) => (
                  <li key={text}>
                    <a href={href}>{text}</a>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>

        <div className="footer-legal">
          <span>
            © 2026 <span className="placeholder-text">[Company legal name]</span>.
            Payments confirmed and settled by Paystack.
          </span>
          <span>
            <a href="/privacy">Privacy policy</a>{" "}
            <a href="/terms">Terms of service</a>{" "}
            {/* REPLACE-WITH-YOUR-EMAIL: point this mailto at your real address before launch */}
            <a href="mailto:REPLACE-WITH-YOUR-EMAIL@example.com">Contact</a>
          </span>
        </div>
      </div>
    </footer>
  );
}
