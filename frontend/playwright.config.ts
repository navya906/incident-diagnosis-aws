import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// End-to-end walk-through on offline data (smoke-test / synthetic): a real backend
// (python -m app.demo: SQLite, offline dataset, stub LLM) and the built frontend (vite preview).
const API_PORT = Number(process.env.E2E_API_PORT ?? 8765);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 4199);
export const E2E_KEY = process.env.E2E_API_KEY ?? "e2e-key-0123456789";
const backend = path.resolve(__dirname, "..", "backend");
const python =
  process.env.E2E_PYTHON ??
  path.join(backend, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const db = path.resolve(__dirname, "test-results", "e2e.db");

export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    screenshot: "on",
    trace: "retain-on-failure",
    viewport: { width: 1400, height: 900 },
  },
  webServer: [
    {
      command: `"${python}" -m app.demo --port ${API_PORT} --db "${db}" --fresh`,
      cwd: backend,
      url: `http://127.0.0.1:${API_PORT}/health`,
      timeout: 180_000,
      reuseExistingServer: false,
      env: { CLOUDDIAG_DEMO_KEY: E2E_KEY },
    },
    {
      command: `npm run build && npx vite preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      timeout: 240_000,
      reuseExistingServer: false,
      env: { API_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
