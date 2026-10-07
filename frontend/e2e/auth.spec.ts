import { expect, test } from "@playwright/test";
import { E2E_KEY } from "../playwright.config";
import { connect, shot } from "./helpers";

// The API key: refused when wrong, kept per tab in sessionStorage only, never in a URL or in
// localStorage, forgotten on request, and required again in a new browser context.
test("API key: wrong key refused, kept per tab only, forgotten on request", async ({ page, browser }) => {
  await connect(page, "wrong-key-0000000000");
  await expect(page.getByRole("alert").first()).toContainText("missing or invalid API key");
  await shot(page, "auth", "01-wrong-key");

  await page.getByRole("button", { name: "Forget API key" }).click();
  await expect(page.getByLabel(/API key/)).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);

  await connect(page);
  await expect(page.getByText("Import an offline incident")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  const storage = await page.evaluate(() => ({
    local: localStorage.length,
    session: Object.values(sessionStorage),
    url: location.href,
  }));
  expect(storage.local).toBe(0);
  expect(storage.session).toContain(E2E_KEY);
  expect(storage.url).not.toContain(E2E_KEY);

  // The key never appears in a request URL (it travels in the X-API-Key header).
  const urls: string[] = [];
  page.on("request", (r) => urls.push(r.url()));
  await page.reload();
  await expect(page.getByText("Import an offline incident")).toBeVisible();
  expect(urls.length).toBeGreaterThan(0);
  expect(urls.some((u) => u.includes(E2E_KEY))).toBe(false);

  // A new browser context (another tab or session) has to enter the key again.
  const other = await browser.newContext();
  const page2 = await other.newPage();
  await page2.goto(page.url());
  await expect(page2.getByLabel(/API key/)).toBeVisible();
  await other.close();
});
