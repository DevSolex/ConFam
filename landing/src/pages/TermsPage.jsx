import {
  DraftBanner,
  LegalFooter,
  LegalHeader,
} from "../components/Legal.jsx";
import SkipLink from "../components/SkipLink.jsx";

export default function TermsPage() {
  return (
    <>
      <SkipLink />
      <LegalHeader />
      <DraftBanner />
      <main id="main" className="legal-body">
        <h1>Terms of service</h1>

        <p>
          This page is a <strong>draft</strong>. It has not been reviewed by
          legal counsel and must not be relied on until it has been finalised.
        </p>

        {/* TODO: replace every placeholder below with reviewed copy once terms are settled */}

        <h2>1. Who these terms cover</h2>
        <p>
          These terms are between ConFam, operated by{" "}
          <span className="placeholder">[Company legal name]</span> (
          <span className="placeholder">[RC number]</span>), and the seller who
          uses the service.
        </p>

        <h2>2. The service</h2>
        <p>
          ConFam lets a seller create a one-time payment link for a sale made
          over WhatsApp and settle payment to the seller's own verified bank
          account. The service is running now; this draft describes it while
          the final wording is settled with counsel.
        </p>

        <h2>3. Your account and your bank details</h2>
        <p>
          <span className="placeholder">
            [Describe verification of the bank account, duty to keep details
            accurate, and that payments settle to the verified account.]
          </span>
        </p>

        <h2>4. Fees</h2>
        <p>
          The Starter plan is free. We will agree any paid pricing with you in
          writing before we charge anything.
        </p>

        <h2>5. Payments and records</h2>
        <p>
          <span className="placeholder">
            [Describe that payment confirmation comes from the payment provider,
            that duplicate confirmations do not double-count, and the nature of
            the ledger records.]
          </span>
        </p>

        <h2>6. Your obligations</h2>
        <p>
          <span className="placeholder">
            [Describe lawful use, accuracy of information provided, and
            compliance with applicable law.]
          </span>
        </p>

        <h2>7. Our limits</h2>
        <p>
          <span className="placeholder">
            [Describe liability limits and disclaimer of warranties — draft
            only.]
          </span>
        </p>

        <h2>8. Ending the agreement</h2>
        <p>
          <span className="placeholder">
            [Describe how either side may end the relationship.]
          </span>
        </p>

        <h2>9. Law and disputes</h2>
        <p>
          <span className="placeholder">
            [Describe governing law and dispute resolution.]
          </span>
        </p>

        <h2>10. Contact</h2>
        <p>
          Questions about these terms:{" "}
          <span className="placeholder">[contact email or address]</span>.
        </p>
      </main>
      <LegalFooter />
    </>
  );
}
