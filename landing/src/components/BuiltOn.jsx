const chips = [
  { label: "WhatsApp Cloud API" },
  { label: "Paystack" },
  { label: "Stellar", note: "coming soon" },
  { label: "Lobstr", note: "coming soon" },
];

export default function BuiltOn() {
  return (
    <section className="section surface-raised" id="built-on">
      <div className="contain">
        <span className="section-label">Built on</span>
        <h2 className="section-heading">
          Rails your buyers<br />already trust.
        </h2>
        <p className="section-sub">
          ConFam doesn't ask anyone to learn a new way to pay. It sits on the
          networks Nigerian and Ghanaian buyers and sellers use today.
        </p>

        <div className="chip-row">
          {chips.map(({ label, note }) => (
            <span className="chip" key={label}>
              {label}
              {note && (
                <span style={{
                  fontSize:"0.75rem",
                  color:"var(--text-faint)",
                  borderLeft:"1px solid rgba(255,255,255,0.15)",
                  paddingLeft:"0.5rem",
                  fontWeight:500
                }}>
                  {note}
                </span>
              )}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
