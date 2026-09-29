import {
  BellIcon,
  CardIcon,
  ChatIcon,
  LedgerIcon,
  LinkIcon,
  ShieldCheckIcon,
} from "./icons.jsx";

const features = [
  {
    Icon: ChatIcon,
    title: "WhatsApp checkout",
    body: "REGISTER, ONBOARD, PAY — the whole setup lives in the same chat where you sell.",
  },
  {
    Icon: ShieldCheckIcon,
    title: "Verified bank account",
    body: "Your account number is checked against the account holder name before ConFam will ever pay it.",
  },
  {
    Icon: LinkIcon,
    title: "One link per sale",
    body: "Every order gets its own one-time link. A paid link expires immediately and can't be reused.",
  },
  {
    Icon: CardIcon,
    title: "Card and bank transfer",
    body: "Buyers pay on Paystack's hosted page — card or bank transfer, no app for them to install.",
  },
  {
    Icon: LedgerIcon,
    title: "Append-only ledger",
    body: "Every payment is one permanent kobo record. Written once, never edited — yours to export.",
  },
  {
    Icon: BellIcon,
    title: "Instant confirmation",
    body: "You get a WhatsApp message the moment a payment confirms. A duplicate webhook still counts once.",
  },
];

export default function WhatYouGet() {
  return (
    <section className="section" id="features">
      <div className="contain">
        <span className="section-label">What you get</span>
        <h2 className="section-heading">
          Everything you need to get paid.<br />Nothing to manage by hand.
        </h2>

        <div className="feature-grid">
          {features.map(({ Icon, title, body }) => (
            <div className="feature" key={title}>
              <div className="feature-icon" aria-hidden="true">
                <Icon />
              </div>
              <h3>{title}</h3>
              <p>{body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
