const { test, expect } = require("@playwright/test");

test("mobile navigation and dialogs remain keyboard accessible", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();

  await page.fill("#display-name", `Keyboard QA ${Date.now()}`);
  await page.fill("#invite-code", "theleague");
  await page.click("text=Enter Market");

  await expect(page.locator("#onboarding-overlay")).toBeVisible();
  await expect(page.locator("#onboarding-overlay")).toHaveAttribute("aria-labelledby", "tour-title");
  await expect(page.locator("#tour-close")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.locator("#tour-next")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#onboarding-overlay")).toBeHidden();

  const collapsedHeight = await page.locator("#side-panel").evaluate((element) => Math.round(element.getBoundingClientRect().height));
  expect(collapsedHeight).toBeLessThanOrEqual(70);
  await expect(page.locator("#primary-navigation")).toBeHidden();

  await page.click("#nav-toggle");
  await expect(page.locator("#nav-toggle")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#primary-navigation")).toBeVisible();
  await page.click("button[data-tab='markets']");
  await expect(page.locator("button[data-tab='markets']")).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#primary-navigation")).toBeHidden();
  await expect(page.locator("#nav-toggle")).toHaveAttribute("aria-expanded", "false");

  const audit = await page.evaluate(() => {
    const visible = (element) => element.getClientRects().length > 0;
    const unlabeled = [...document.querySelectorAll("input, select, textarea")]
      .filter(visible)
      .filter((element) => !element.labels?.length && !element.getAttribute("aria-label") && !element.getAttribute("aria-labelledby"))
      .map((element) => element.id || element.tagName);
    const ids = [...document.querySelectorAll("[id]")].map((element) => element.id);
    return {
      unlabeled,
      duplicateIds: ids.filter((id, index) => ids.indexOf(id) !== index),
      bodyWidth: document.documentElement.scrollWidth,
      viewport: innerWidth,
      sidePanelHeight: Math.round(document.querySelector("#side-panel").getBoundingClientRect().height)
    };
  });

  expect(audit.unlabeled).toEqual([]);
  expect(audit.duplicateIds).toEqual([]);
  expect(audit.bodyWidth).toBe(audit.viewport);
  expect(audit.sidePanelHeight).toBeLessThanOrEqual(70);
});

test("admin all-clear state stays simple on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let dashboardRequests = 0;
  await page.route("**/api/admin/dashboard?**", async (route) => {
    dashboardRequests += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        all_clear: true,
        summary: "Nothing needs attention",
        league: { id: "1326428061876371456", name: "The League" },
        actions: [],
        automation: {
          pipeline: { label: "Data pipeline", state: "healthy", last_success_at: new Date().toISOString() },
          lifecycle: { label: "Market lifecycle", state: "healthy", last_success_at: new Date().toISOString() },
          backup: { label: "Database backup", state: "healthy", last_success_at: new Date().toISOString() }
        },
        managers: [],
        job_history: [],
        recent_activity: [],
        environment: "test"
      })
    });
  });

  await page.goto("/");
  await page.fill("#display-name", `All Clear QA ${Date.now()}`);
  await page.fill("#invite-code", "theleague");
  await page.click("text=Enter Market");
  await expect(page.locator("#onboarding-overlay")).toBeVisible();
  await page.click("#tour-close");
  await page.click("#nav-toggle");
  await page.click("button[data-tab='admin']");
  expect(dashboardRequests).toBe(0);
  await page.fill("#admin-code", "commissioner");
  await page.click("#admin-unlock");

  await expect(page.locator("#admin-inbox-view")).toBeVisible();
  await expect(page.locator("#admin-inbox-root")).toContainText("Nothing needs your attention");
  await expect(page.locator("#admin-automation-root .healthy")).toHaveCount(3);
  const dimensions = await page.locator("#admin-inbox-view").evaluate(() => ({
    bodyWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth
  }));
  expect(dimensions.bodyWidth).toBe(dimensions.viewportWidth);
});
