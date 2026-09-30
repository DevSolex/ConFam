// Single source of truth for the WhatsApp contact link.
//
// The prefill message used to be copy-pasted into six components, which meant
// changing the pitch meant finding all six. It is encoded once here.
export const WHATSAPP_NUMBER = "2348051338460";

export const WHATSAPP_URL = `https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent(
  "Hi, I'd like to start taking WhatsApp payments with ConFam"
)}`;

// Trading name used wherever the site previously showed a "[Company legal
// name]" placeholder: footer, and the operator named in the terms and privacy
// pages. The registered entity name and RC number are still outstanding — those
// placeholders remain until the registration details exist.
export const COMPANY_NAME = "ConFam";

// Public contact address, shown in the footer and as the contact route in both
// legal pages.
export const CONTACT_EMAIL = "thesolex7@gmail.com";