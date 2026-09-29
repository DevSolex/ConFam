/**
 * PhoneMockup.jsx
 *
 * Renders a styled SVG phone frame containing WhatsApp-style chat bubbles
 * showing real ConFam conversation content.
 *
 * No external images, no stock photography, no licensing questions.
 * Pure inline SVG — renders at any size, works in all browsers.
 *
 * Props:
 *   variant: 'pay-flow' | 'register-flow' | 'payment-received' | 'ledger'
 */

const PHONE_W = 280;
const PHONE_H = 520;
const FRAME_R = 28;
const SCREEN_X = 10;
const SCREEN_Y = 10;
const SCREEN_W = PHONE_W - 20;
const SCREEN_H = PHONE_H - 20;
const SCREEN_R = 20;

const WA_GREEN = "#075E54";
const WA_GREEN_LIGHT = "#25D366";
const BUBBLE_SENT_BG = "#E2FFC7";
const BUBBLE_SENT_TEXT = "#1a2e1a";
const BUBBLE_RECV_BG = "#ffffff";
const BUBBLE_RECV_TEXT = "#1a1a1a";
const CHAT_BG = "#ECE5DD";
const FRAME_BG = "#1a1a2e";

const HEADER_H = 48;
const LINE_H = 13;
const PAD = 7;
const BUBBLE_GAP = 8;

// Conversation data for each variant.
// sent: true = right-aligned merchant bubble (light green)
// sent: false = left-aligned ConFam bot bubble (white)
const VARIANTS = {
  "pay-flow": [
    { sent: true,  lines: ["PAY 2500 Jordan"] },
    { sent: false, lines: ["Payment link created ✅", "pay.confam.co/a1b2c3", "Expires in 30 mins."] },
    { sent: false, lines: ["Payment received ✅", "Amount: ₦2,500.00", "Jordan — confirmed."] },
  ],
  "register-flow": [
    { sent: true,  lines: ["REGISTER Adaeze Fashion Store"] },
    { sent: false, lines: ["✅ Business registered!", "Next step: send ONBOARD"] },
    { sent: true,  lines: ["ONBOARD 0123456789 GTBank"] },
    { sent: false, lines: ["✅ Payout account set up!", "You're ready to accept", "payments."] },
  ],
  "payment-received": [
    { sent: false, lines: ["Payment received ✅", "Amount: ₦3,400.00", "Paystack is settling."] },
    { sent: false, lines: ["3 payments today", "Total: ₦6,800.00"] },
  ],
  "ledger": [
    { sent: true,  lines: ["LEDGER"] },
    { sent: false, lines: ["📄 Your sales statement", "3 sales — ₦6,800.00 total", "[PDF attached]"] },
  ],
};

// Estimate bubble width from the longest line in it.
// Using ~5.5px per character at font-size 8.5 + 2×PAD.
function bubbleWidth(lines) {
  const maxLen = Math.max(...lines.map((l) => l.length));
  return Math.min(Math.max(maxLen * 5.5 + PAD * 2, 50), SCREEN_W - 24);
}

export default function PhoneMockup({ variant = "pay-flow" }) {
  const bubbleData = VARIANTS[variant] || VARIANTS["pay-flow"];

  // Layout: compute Y position for each bubble sequentially.
  const chatStartY = SCREEN_Y + HEADER_H + 10;
  let curY = chatStartY;
  const laid = bubbleData.map((b) => {
    const lines = b.lines || [];
    const h = lines.length * LINE_H + PAD * 2;
    const bw = bubbleWidth(lines);
    const entry = { ...b, y: curY, h, bw };
    curY += h + BUBBLE_GAP;
    return entry;
  });

  const clipId = `screen-${variant}`;

  return (
    <svg
      viewBox={`0 0 ${PHONE_W} ${PHONE_H}`}
      width="100%"
      height="auto"
      role="img"
      aria-label={`ConFam WhatsApp conversation — ${variant.replace(/-/g, " ")}`}
      xmlns="http://www.w3.org/2000/svg"
      style={{ display: "block" }}
    >
      {/* Phone frame */}
      <rect width={PHONE_W} height={PHONE_H} rx={FRAME_R} fill={FRAME_BG} />

      {/* Screen background */}
      <rect
        x={SCREEN_X} y={SCREEN_Y}
        width={SCREEN_W} height={SCREEN_H}
        rx={SCREEN_R}
        fill={CHAT_BG}
      />

      {/* Clip path so bubbles don't overflow the screen edges */}
      <defs>
        <clipPath id={clipId}>
          <rect
            x={SCREEN_X} y={SCREEN_Y}
            width={SCREEN_W} height={SCREEN_H}
            rx={SCREEN_R}
          />
        </clipPath>
      </defs>

      <g clipPath={`url(#${clipId})`}>
        {/* WhatsApp-style green header bar */}
        <rect x={SCREEN_X} y={SCREEN_Y} width={SCREEN_W} height={HEADER_H} fill={WA_GREEN} />

        {/* Avatar circle */}
        <circle cx={SCREEN_X + 22} cy={SCREEN_Y + HEADER_H / 2} r={14} fill={WA_GREEN_LIGHT} />
        <text
          x={SCREEN_X + 22}
          y={SCREEN_Y + HEADER_H / 2 + 4}
          textAnchor="middle"
          fontSize="10"
          fontWeight="bold"
          fill="white"
          fontFamily="system-ui, -apple-system, sans-serif"
        >
          CF
        </text>

        {/* Header name */}
        <text
          x={SCREEN_X + 42}
          y={SCREEN_Y + 21}
          fontSize="10"
          fontWeight="bold"
          fill="white"
          fontFamily="system-ui, -apple-system, sans-serif"
        >
          ConFam
        </text>
        <text
          x={SCREEN_X + 42}
          y={SCREEN_Y + 35}
          fontSize="8"
          fill="rgba(255,255,255,0.78)"
          fontFamily="system-ui, -apple-system, sans-serif"
        >
          online
        </text>

        {/* Chat bubbles */}
        {laid.map((b, i) => {
          const bx = b.sent
            ? SCREEN_X + SCREEN_W - 10 - b.bw
            : SCREEN_X + 10;
          const bg = b.sent ? BUBBLE_SENT_BG : BUBBLE_RECV_BG;
          const fg = b.sent ? BUBBLE_SENT_TEXT : BUBBLE_RECV_TEXT;

          return (
            <g key={i}>
              <rect x={bx} y={b.y} width={b.bw} height={b.h} rx={7} fill={bg} />
              {b.lines.map((line, li) => (
                <text
                  key={li}
                  x={bx + PAD}
                  y={b.y + PAD + 9 + li * LINE_H}
                  fontSize="8.5"
                  fill={fg}
                  fontFamily="system-ui, -apple-system, sans-serif"
                >
                  {line}
                </text>
              ))}
            </g>
          );
        })}
      </g>

      {/* Front-camera notch */}
      <circle cx={PHONE_W / 2} cy={SCREEN_Y} r={4} fill={FRAME_BG} />
    </svg>
  );
}
