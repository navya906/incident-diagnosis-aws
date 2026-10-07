import { expect, test } from "@playwright/test";
import { connect, firstFree, importIncident, manifest, runDiagnosis, shot, tab } from "./helpers";

// "Never display a conclusion without its evidence" (D102) on a real backend: an incident whose
// telemetry was stripped (insufficient-evidence case) must not show a root cause. The two ids
// are the dev insufficient-evidence cases for which the deterministic stub answers
// insufficient_evidence (exp-fbeacbc7cd9b, Full); the manifest check below fails loudly if the
// dataset ever changes.
const CANDIDATES = ["inc-78d6b21417", "inc-e9c84f0f9d"];

test("evidence rule: insufficient evidence shows no root cause, only what is missing", async ({ page, request }) => {
  const entries = manifest();
  for (const id of CANDIDATES) {
    expect(entries.find((e) => e.incident_id === id)?.category).toBe("insufficient_evidence");
  }
  const id = await firstFree(request, CANDIDATES);

  await connect(page);
  await importIncident(page, id);
  await runDiagnosis(page);

  const insufficient = page.getByTestId("diagnosis-insufficient");
  await expect(insufficient).toBeVisible();
  await expect(insufficient).toContainText("Insufficient evidence");
  await expect(page.getByTestId("root-cause")).toHaveCount(0);
  await expect(page.getByTestId("supporting")).toHaveCount(0);
  await shot(page, "evidence-rule", "01-insufficient");

  // The overview and the incident list never show a root cause, only status and severity.
  await tab(page, "Overview");
  await expect(page.getByTestId("lifecycle")).toContainText("DIAGNOSED");
  await expect(page.getByTestId("root-cause")).toHaveCount(0);
  await page.getByRole("link", { name: "← all incidents" }).click();
  await expect(page.getByTestId("incident-table")).toContainText(id);
  await expect(page.getByTestId("incident-table")).not.toContainText("insufficient_evidence");
});
