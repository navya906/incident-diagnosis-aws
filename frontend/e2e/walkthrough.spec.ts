import { expect, test, type Page } from "@playwright/test";
import { E2E_KEY } from "../playwright.config";
import { firstFree, manifest, shot as save, tab } from "./helpers";

// One scripted walk-through of the whole UI on an offline incident (smoke-test / synthetic).
// The dataset manifest is read only to pick a representative "standard" incident with
// CloudTrail activity; nothing about the expected diagnosis is asserted from ground truth.
function candidates(): string[] {
  const ids = manifest()
    .filter((x) => x.split === "dev" && x.category === "standard" && x.fault_type === "deployment_failure" && x.topology === "ecs")
    .map((x) => x.incident_id);
  if (!ids.length) throw new Error("no suitable dev incident in the dataset");
  return ids;
}

async function shot(page: Page, name: string) {
  await save(page, "walkthrough", name);
}

test("walk-through: import, diagnose, inspect every view, resolve and close", async ({ page, request }) => {
  const incidentId = await firstFree(request, candidates());

  // 1. Connect with the API key (kept in sessionStorage only).
  await page.goto("/");
  await page.getByLabel(/API key/).fill(E2E_KEY);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Import an offline incident")).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  await shot(page, "01-incidents");

  // 2. Import an offline incident.
  await page.getByLabel("offline incident").selectOption(incidentId);
  await page.getByRole("button", { name: "Import" }).click();
  await expect(page).toHaveURL(new RegExp(`/incidents/${incidentId}/overview`));
  await expect(page.getByTestId("lifecycle")).toContainText("DETECTED");
  await shot(page, "02-overview-detected");

  // 3. Run the diagnosis (async job) and read it.
  await tab(page, "Diagnosis");
  await page.getByRole("button", { name: "Run diagnosis" }).click();
  await expect(page.getByTestId("job-status")).toContainText("SUCCEEDED", { timeout: 120_000 });
  const root = page.getByTestId("root-cause");
  await expect(root).toBeVisible();
  await expect(root.getByTestId("claim-label")).toHaveText("INFERENCE");
  const claims = page.getByTestId("supporting").getByTestId("evidence-claim");
  expect(await claims.count()).toBeGreaterThan(0);
  // Every supporting claim shows its label and the event it rests on.
  for (let i = 0; i < (await claims.count()); i++) {
    await expect(claims.nth(i).getByTestId("claim-label")).toHaveText(/FACT|INFERENCE/);
    await expect(claims.nth(i)).not.toContainText("Cited evidence not found");
  }
  await expect(page.getByTestId("evidence-timeline")).toContainText("alarm");
  await expect(page.getByTestId("resource")).toBeVisible();
  await expect(page.getByTestId("dependency-path")).toBeVisible();
  await expect(page.getByTestId("alternatives")).toBeVisible();
  await expect(page.getByTestId("contradicting")).toBeVisible();
  await expect(page.getByTestId("recommendations").getByTestId("claim-label").first()).toHaveText("RECOMMENDATION");
  await expect(page.getByTestId("severity")).toContainText("deterministic engine");
  await expect(page.getByText("smoke-test / synthetic").first()).toBeVisible();
  await shot(page, "03-diagnosis");

  // 4. The job moved the lifecycle (system transitions with timestamps).
  await tab(page, "Overview");
  await expect(page.getByTestId("lifecycle")).toContainText("DIAGNOSED");
  await expect(page.getByTestId("transitions")).toContainText("by system");
  await expect(page.getByTestId("ttdiag")).not.toHaveText("—");

  // 5. Interactive timeline.
  await tab(page, "Timeline");
  await expect(page.getByTestId("timeline-list")).toBeVisible();
  const before = await page.getByTestId("timeline-count").textContent();
  await page.getByLabel("time window").selectOption("all");
  await expect(page.getByTestId("timeline-count")).not.toHaveText(before ?? "");
  await page.getByTestId("timeline-list").locator("li").first().click();
  await expect(page.getByTestId("timeline-list").locator("pre").first()).toBeVisible();
  await shot(page, "04-timeline");

  // 6. Metrics around the incident window.
  await tab(page, "Metrics");
  await expect(page.getByTestId("metric-chart").first()).toBeVisible();
  await expect(page.getByTestId("metric-chart").first().locator("svg").first()).toBeVisible();
  await shot(page, "05-metrics");

  // 7. Searchable logs.
  await tab(page, "Logs");
  await expect(page.getByTestId("result-count")).toBeVisible();
  await page.getByLabel("severity").selectOption("ERROR");
  await expect(page.getByTestId("event-row").first()).toBeVisible();
  await page.getByLabel("search logs").fill("Failed");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("result-count")).toBeVisible();
  await shot(page, "06-logs");

  // 8. CloudTrail view.
  await tab(page, "CloudTrail");
  await expect(page.getByTestId("event-row").first()).toBeVisible();
  await page.getByLabel("search cloudtrail").fill("UpdateService");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("event-row").first()).toContainText("UpdateService");
  await shot(page, "07-cloudtrail");

  // 9. Dependency graph with highlighted roles.
  await tab(page, "Graph");
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
  expect(await page.locator(".react-flow__node").count()).toBeGreaterThanOrEqual(4);
  await expect(page.locator(".role-affected")).toHaveCount(1);
  expect(await page.locator(".role-can_cause").count()).toBeGreaterThan(0);
  await expect(page.getByTestId("graph-legend")).toContainText("affected (alarmed)");
  await shot(page, "08-graph");

  // 10. Mitigate, resolve, close.
  await tab(page, "Overview");
  for (const to of ["MITIGATING", "RESOLVED", "CLOSED"]) {
    await page.getByRole("button", { name: `Move to ${to}` }).click();
    await expect(page.getByTestId("lifecycle")).toContainText(to);
    await expect(page.getByTestId("incident-header")).toContainText(to); // one source of truth
  }
  await expect(page.getByTestId("ttr")).not.toHaveText("—");
  await shot(page, "09-closed");

  // 11. The incident list reflects it.
  await page.getByRole("link", { name: "← all incidents" }).click();
  await expect(page.getByTestId("incident-table")).toContainText("CLOSED");
  await expect(page.getByTestId("lifecycle-metrics")).not.toContainText("…");
  await shot(page, "10-incidents-closed");
});
