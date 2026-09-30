import { COMPANY_NAME, CONTACT_EMAIL } from "../site.js";
import {
  DraftBanner,
  LegalFooter,
  LegalHeader,
} from "../components/Legal.jsx";
import SkipLink from "../components/SkipLink.jsx";

export default function PrivacyPage() {
  return (
    <>
      <SkipLink />
      <LegalHeader />
      <DraftBanner />
      <main id="main" className="legal-body">
        <h1>Privacy policy</h1>

        <p>
          This page is a <strong>draft</strong>. It has not been reviewed by
          legal counsel and must not be relied on until it has been finalised.
        </p>

        {/* TODO: replace every placeholder below with reviewed copy once privacy requirements are settled */}

        <p>
          {COMPANY_NAME} is a company registered in Nigeria (
          <span className="placeholder">[RC number]</span>) with its registered
          office at <span className="placeholder">[registered address]</span>. We
          process personal data in accordance with the Nigeria Data Protection
          Act and the General Data Protection Regulation where it applies.
        </p>

        <h2>What we collect</h2>
        <ul>
          <li>
            <span className="placeholder">
              [Describe what is collected — e.g. WhatsApp phone number, merchant
              and buyer details, payment amounts and references.]
            </span>
          </li>
          <li>
            <span className="placeholder">
              [Describe what is not collected — e.g. card numbers (card details
              are handled by our payment processor).]
            </span>
          </li>
        </ul>

        <h2>How we use it</h2>
        <ul>
          <li>
            <span className="placeholder">
              [Describe purposes — e.g. to create and confirm payments, to show
              you your sales records, to send confirmations.]
            </span>
          </li>
          <li>
            <span className="placeholder">[Describe legal bases.]</span>
          </li>
        </ul>

        <h2>Who we share it with</h2>
        <p>
          <span className="placeholder">
            [Describe processors — e.g. our payment processor who needs your
            account details to settle funds.]
          </span>
        </p>

        <h2>Retention</h2>
        <p>
          <span className="placeholder">
            [Describe how long data is kept and why, including any regulatory
            record-keeping obligations.]
          </span>
        </p>

        <h2>Your rights</h2>
        <p>
          <span className="placeholder">
            [Describe access, correction, deletion and objection rights and how to
            exercise them.]
          </span>
        </p>

        <h2>Contact</h2>
        <p>
          Questions about this policy:{" "}
          <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
        </p>
      </main>
      <LegalFooter />
    </>
  );
}
