import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const page = (name) => fileURLToPath(new URL(name, import.meta.url));

// Multi-page build: one HTML entry per route, no client-side router.
export default defineConfig({
  plugins: [react()],
  // Multi-page, not a client-side router. "mpa" drops Vite's SPA fallback so a
  // mistyped URL 404s instead of silently rendering the home page.
  appType: "mpa",
  build: {
    rollupOptions: {
      input: {
        main: page("./index.html"),
        privacy: page("./privacy.html"),
        terms: page("./terms.html"),
      },
    },
  },
});
