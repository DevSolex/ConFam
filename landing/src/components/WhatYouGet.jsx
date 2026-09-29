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
    body: "The whole flow — REGISTER, ONBOARD, PAY — lives in the chat where the sale happened.",
  },
  {
    Icon: ShieldCheckIcon,
    title: "Verified bank account",
    body: "An account is checked against the account holder's name before ConFam will send money to it.",
  },
  {
    Icon: LinkIcon,
    title: "One link per sale",
    body: "Each sale gets its own one-time payment link, so a paid link can't be resold.",
  },
  {
    Icon: CardIcon,
    title: "Card and bank checkout",
    body: "Buyers pay by card or bank transfer on Paystack's hosted page — no app to install.",
  },
  {
    Icon: LedgerIcon,
    title: "Append-only ledger",
    body: "Every payment is one whole-number kobo record. Once written, a record can't be edited.",
  },
  {
    Icon: BellIcon,
    title: "WhatsApp confirmation",
    body: "The seller gets a WhatsApp message the moment a payment confirms. Duplicate confirmations count once.",
  },
];

export default function WhatYouGet() {
  return (
    <section className="section" id="what-you-get">
      <div className="contain">
        <h2 className="section-heading">What you get</h2>
        <p className="lead">
          Everything you need to get paid for a WhatsApp sale, with nothing you
          have to manage by hand.
        </p>

        <div className="feature-grid">
          {features.map(({ Icon, title, body }) => (
            <div className="feature" key={title}>
              <div className="icon" aria-hidden="true">
                <Icon />
              </div>
              <h3>{title}</h3>
              <p>{body}</p>
            </div>
          ))}
        </div>

        <p className="note-line">
          <em>In development:</em> stablecoin payments from Stellar wallets such
          as Lobstr — you still receive naira in your bank.
        </p>
      </div>
    </section>
  );
}
