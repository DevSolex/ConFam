// Single source of truth for the WhatsApp contact link.
//
// The prefill message used to be copy-pasted into six components, which meant
// changing the pitch meant finding all six. It is encoded once here.
export const WHATSAPP_NUMBER = "2348051338460";

export const WHATSAPP_URL = `https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent(
  "Hi, I'd like to start taking WhatsApp payments with ConFam"
)}`;