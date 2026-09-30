import { WHATSAPP_URL } from "../site.js";

export default function AnnouncementBar() {
  return (
    <p className="announce">
      ConFam is live in Nigeria &amp; Ghana — take your first confirmed payment
      today.{" "}
      <a href={WHATSAPP_URL} target="_blank" rel="noopener noreferrer">
        Start now →
      </a>
    </p>
  );
}