import { expect, test } from "@playwright/test";
import { authHeaders, connect, firstFree, manifest, shot } from "./helpers";

// Deep links and the incident views for a different topology (SQS consumer) than the
// walk-through: every tab loads directly by URL (SPA fallback), shows its data, and the graph
// offers the text list of node roles for screen readers.
test("deep links: every incident tab opens by URL and shows its data", async ({ page, request }) => {
  const ids = manifest()
    .filter((e) => e.split === "dev" && e.category === "standard" && e.topology === "sqs")
    .map((e) => e.incident_id);
  const id = await firstFree(request, ids);
  const created = await request.post("/api/offline/import", {
    headers: authHeaders,
    data: { offline_incident_id: id },
  });
  expect(created.status()).toBe(201);

  await connect(page);
  await page.goto(`/incidents/${id}/metrics`);
  await expect(page).toHaveURL(new RegExp(`/incidents/${id}/metrics$`));
  await expect(page.getByTestId("incident-header")).toContainText("sqs/queue/");
  await expect(page.getByTestId("metric-chart").first()).toBeVisible();

  await page.goto(`/incidents/${id}/timeline`);
  await expect(page.getByTestId("timeline-list")).toBeVisible();

  await page.goto(`/incidents/${id}/logs`);
  await expect(page.getByTestId("result-count")).toBeVisible();

  await page.goto(`/incidents/${id}/cloudtrail`);
  await expect(page.getByTestId("result-count")).toBeVisible();

  await page.goto(`/incidents/${id}/graph`);
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
  await expect(page.locator(".role-affected")).toHaveCount(1);
  await expect(page.getByTestId("graph-nodes")).toContainText("affected");
  expect(await page.getByTestId("graph-nodes").locator("li").count()).toBe(
    await page.locator(".react-flow__node").count(),
  );
  await shot(page, "navigation", "01-graph-sqs");

  // No diagnosis yet: the page says so instead of showing a conclusion.
  await page.goto(`/incidents/${id}/diagnosis`);
  await expect(page.getByRole("button", { name: "Run diagnosis" })).toBeVisible();
  await expect(page.getByTestId("root-cause")).toHaveCount(0);

  await page.goto("/no-such-page");
  await expect(page.getByText("Page not found.")).toBeVisible();
});
