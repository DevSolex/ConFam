const chips = [
  { label: "WhatsApp Cloud API" },
  { label: "Paystack" },
  { label: "Stellar", state: "in development", dev: true },
  { label: "Lobstr", state: "in development", dev: true },
];

export default function BuiltOn() {
  return (
    <section className="section" id="built-on">
      <div className="contain">
        <h2 className="section-heading">
          Built on rails your buyers already trust
        </h2>
        <p className="lead">
          ConFam sits on the networks Nigerian buyers and sellers use today, and
          adds new ones only when they're ready.
        </p>

        <div className="chip-row">
          {chips.map(({ label, state, dev }) => (
            <span className={dev ? "chip dev" : "chip"} key={label}>
              {label} {state && <span className="state">{state}</span>}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
