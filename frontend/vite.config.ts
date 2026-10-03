/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// The dev server proxies /api to the Python backend, so the browser only ever
// talks to one origin and the backend needs no CORS configuration.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  const backend = env.VITE_BACKEND_URL || "http://127.0.0.1:8001";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { "/api": { target: backend, changeOrigin: false } },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
    },
  };
});
