import { expect, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { E2E_KEY } from "../playwright.config";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export interface ManifestEntry {
  incident_id: string;
  split: string;
  category: string;
  fault_type: string;
  topology: string;
}

// The dataset manifest is read only to pick representative incidents (by category and
// topology); no assertion about a diagnosis uses ground truth. The dataset is deterministic, so
// a locally generated manifest has the same ids as the one inside the Docker image.
export function manifest(): ManifestEntry[] {
  const file = path.resolve(__dirname, "..", "..", "data", "generated", "synthetic-v1", "manifest.json");
  if (!fs.existsSync(file)) {
    throw new Error("offline dataset missing: run `python -m app.offline.generate` in backend/");
  }
  return (JSON.parse(fs.readFileSync(file, "utf-8")) as { entries: ManifestEntry[] }).entries;
}

export const authHeaders = { "X-API-Key": E2E_KEY };

export async function isImported(request: APIRequestContext, id: string): Promise<boolean> {
  const r = await request.get(`/api/incidents/${id}`, { headers: authHeaders });
  return r.status() === 200;
}

/** First candidate not imported yet, so the suite can run again against a persistent stack. */
export async function firstFree(request: APIRequestContext, ids: string[]): Promise<string> {
  for (const id of ids) if (!(await isImported(request, id))) return id;
  throw new Error(`all candidate incidents are already imported: ${ids.join(", ")}`);
}

export async function connect(page: Page, key = E2E_KEY) {
  await page.goto("/");
  await page.getByLabel(/API key/).fill(key);
  await page.getByRole("button", { name: "Continue" }).click();
}

export async function importIncident(page: Page, id: string) {
  await expect(page.getByText("Import an offline incident")).toBeVisible();
  await page.getByLabel("offline incident").selectOption(id);
  await page.getByRole("button", { name: "Import" }).click();
  await expect(page).toHaveURL(new RegExp(`/incidents/${id}/overview`));
}

export async function tab(page: Page, name: string) {
  await page.getByRole("navigation", { name: "incident sections" }).getByRole("link", { name }).click();
}

export async function runDiagnosis(page: Page) {
  await tab(page, "Diagnosis");
  await page.getByRole("button", { name: "Run diagnosis" }).click();
  await expect(page.getByTestId("job-status")).toContainText("SUCCEEDED", { timeout: 120_000 });
}

export async function shot(page: Page, dir: string, name: string) {
  await page.screenshot({ path: `test-results/${dir}/${name}.png`, fullPage: true });
}
