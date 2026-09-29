const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
};

export function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z" />
    </svg>
  );
}

export function ShieldCheckIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  );
}

export function LinkIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <path d="M10 14a4 4 0 0 0 5.5.5l2-2a4 4 0 0 0-5.5-5.5L10 9" />
      <path d="M14 10a4 4 0 0 0-5.5-.5l-2 2a4 4 0 0 0 5.5 5.5L14 15" />
    </svg>
  );
}

export function CardIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <rect x="2" y="5" width="20" height="14" rx="2" />
      <path d="M2 10h20" />
    </svg>
  );
}

export function LedgerIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <path d="M5 4h14v16H5z" />
      <path d="M9 9h6M9 13h6M9 17h3" />
    </svg>
  );
}

export function BellIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke}>
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.7 21a2 2 0 0 1-3.4 0" />
    </svg>
  );
}
