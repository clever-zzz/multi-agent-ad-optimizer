/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The dev server proxies API calls so the browser never needs CORS in development
// and no secret ever reaches the client bundle. `vite preview` reuses the same map
// so the production bundle can be smoke-tested against a real API before deploy.
const backend = process.env.VITE_BACKEND_URL ?? "http://localhost:8000";

const apiProxy = {
  "/api": { target: backend, changeOrigin: true },
  "/healthz": { target: backend, changeOrigin: true },
  "/readyz": { target: backend, changeOrigin: true },
  "/metrics": { target: backend, changeOrigin: true },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    strictPort: false,
    proxy: apiProxy,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2022",
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});