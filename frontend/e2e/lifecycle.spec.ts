import { expect, test } from "@playwright/test";
import { authHeaders, connect, firstFree, importIncident, manifest, shot } from "./helpers";

// Lifecycle rules enforced by the server and shown in the UI: closing an unresolved incident
// needs a note; the refusal is displayed; with a note the close succeeds and is terminal.
test("lifecycle: closing an unresolved incident needs a note, then CLOSED is terminal", async ({ page, request }) => {
  const ids = manifest()
    .filter((e) => e.split === "dev" && e.category === "standard" && e.topology === "lambda")
    .map((e) => e.incident_id);
  const id = await firstFree(request, ids);

  await connect(page);
  await importIncident(page, id);
  const lifecycle = page.getByTestId("lifecycle");
  await expect(lifecycle).toContainText("DETECTED");
  await expect(lifecycle.getByRole("button", { name: "Move to INVESTIGATING" })).toBeVisible();
  // Only allowed moves are offered.
  await expect(lifecycle.getByRole("button", { name: "Move to RESOLVED" })).toHaveCount(0);

  await lifecycle.getByRole("button", { name: "Move to CLOSED" }).click();
  await expect(lifecycle.getByRole("alert")).toContainText(/note/i);
  await expect(lifecycle).toContainText("DETECTED");
  await shot(page, "lifecycle", "01-close-refused");

  await page.getByLabel("transition note").fill("false alarm: load test, verified with the owner");
  await lifecycle.getByRole("button", { name: "Move to CLOSED" }).click();
  await expect(page.getByTestId("incident-header")).toContainText("CLOSED");
  await expect(page.getByTestId("transitions")).toContainText("false alarm: load test");
  await expect(lifecycle.getByRole("button", { name: /Move to/ })).toHaveCount(0);
  await shot(page, "lifecycle", "02-closed");

  // The server agrees, and refuses any further move.
  const r = await request.post(`/api/incidents/${id}/transitions`, {
    headers: authHeaders,
    data: { to: "INVESTIGATING" },
  });
  expect(r.status()).toBe(409);
});
