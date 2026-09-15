const { test, expect } = require("@playwright/test");

test("join, seed, trade, resolve, and view leaderboard", async ({ page }) => {
  test.setTimeout(90000);
  const dashboardRequests = [];
  const fundRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/admin/dashboard")) dashboardRequests.push(request.url());
    if (request.url().endsWith("/api/fund")) fundRequests.push(request.url());
  });
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const runId = Date.now();
  const marketTitle = `Playwright Coin Toss ${runId}`;
  const traderName = `Playwright ${runId}`;

  await page.fill("#invite-code", "theleague");
  await page.locator(".join-advanced summary").click();
  await page.fill("#display-name", traderName);
  await page.click("#join-submit");

  await expect(page.locator("text=The League Market").first()).toBeVisible();
  await expect(page).toHaveTitle("The League Market");
  await expect(page.locator("#side-panel .logo-mark svg.lucide-chart-candlestick")).toHaveCount(1);
  await expect(page.locator("#primary-navigation svg.lucide")).toHaveCount(4);
  await expect(page.locator("#utility-navigation svg.lucide")).toHaveCount(3);
  await expect(page.locator(".topbar-actions svg.lucide")).toHaveCount(4);
  await expect(page.locator("#refresh-button")).toHaveAttribute("title", "Sync market data");
  await expect(page.locator("#onboarding-overlay")).toBeVisible();
  await expect(page.locator("#tour-content")).toContainText("Your desk starts here");
  await page.click("#tour-close");
  await expect(page.locator("#onboarding-overlay")).toBeHidden();
  await page.hover("#feedback-button");
  await page.waitForTimeout(150);
  const feedbackTooltip = await page.locator("#feedback-button").evaluate((button) => {
    const style = getComputedStyle(button, "::after");
    return {
      content: style.content,
      display: style.display,
      opacity: style.opacity
    };
  });
  expect(feedbackTooltip.content).toContain("Send feedback");
  expect(feedbackTooltip.display).not.toBe("none");
  expect(Number(feedbackTooltip.opacity)).toBeGreaterThan(0);
  await expect(page.locator("#side-panel")).toBeVisible();
  expect(fundRequests).toHaveLength(0);
  await page.click("#nav-toggle");
  await expect(page.locator("#app-view")).toHaveClass(/nav-collapsed/);
  await page.click("#nav-toggle");
  await expect(page.locator("#app-view")).not.toHaveClass(/nav-collapsed/);

  await page.click("button[data-tab='admin']");
  await expect(page.locator("#admin-lock-view")).toBeVisible();
  expect(dashboardRequests).toHaveLength(0);
  await page.fill("#admin-code", "commissioner");
  await page.click("#admin-unlock");
  await expect(page.locator("#admin-inbox-view")).toBeVisible();
  await expect(page.locator("#admin-summary")).toContainText(/attention|Nothing/);
  expect(dashboardRequests.length).toBeGreaterThan(0);
  const identityAction = page.locator(".admin-inbox-item").filter({ hasText: traderName }).locator("button");
  await expect(identityAction).toBeVisible();
  await identityAction.click();
  await expect(page.locator("#admin-action-dialog")).toBeVisible();
  await expect(page.locator("#admin-action-title")).toContainText("Sleeper");
  await page.click("#admin-action-close");
  await expect(identityAction).toBeFocused();
  await page.click("#admin-settings-open");
  await expect(page.locator("#admin-settings-view")).toBeVisible();
  await expect(page.locator("#league-id-input")).toHaveValue("1326428061876371456");
  await page.click("#setup-league");
  await expect(page.locator("#admin-status")).toContainText(/Published|markets/, { timeout: 60000 });
  await expect(page.locator("#setup-result")).toContainText("Model");
  await page.click("[data-admin-settings-tab='people']");
  await expect(page.locator("#managers-root .manager-card").first()).toBeVisible();
  await expect(page.locator("#managers-root")).toContainText("Owner");
  await expect(page.locator("#participants-root .participant-card").first()).toBeVisible();
  await expect(page.locator("#invites-root")).toContainText("theleague");
  await page.click("[data-admin-settings-tab='markets']");
  await page.fill("#manual-title", marketTitle);
  await page.fill("#manual-outcomes", "YES\nNO");
  await page.fill("#manual-rule", "YES wins if the commissioner says the test passes.");
  await page.locator("#manual-market-form button[type='submit']").click();
  await expect(page.locator("#admin-status")).toContainText("Manual market created");

  await page.click("button[data-tab='home']");
  await expect(page.locator("#home-root .home-hero")).toBeVisible();
  await expect(page.locator("#home-root .exchange-curve")).toHaveCount(0);
  await expect(page.locator("#home-root .book-map")).toBeVisible();
  await expect(page.locator("#home-root .pixel-matrix span")).toHaveCount(60);
  await expect(page.locator("[data-home-market-filter='movers']")).toHaveAttribute("aria-pressed", "true");
  await page.click("[data-home-market-filter='weekly']");
  await expect(page.locator("[data-home-market-filter='weekly']")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("[data-home-widget='watch'] .market-quote").first()).toContainText("Week 1");
  await page.locator("[data-home-widget='tape'] > summary").click();
  await expect(page.locator("[data-home-widget='tape']")).not.toHaveAttribute("open", "");
  await page.click("button[data-tab='markets']");
  await expect(page.locator("#markets-root .asset-row").first()).toBeVisible();
  const championQuote = page.locator("#markets-root .market-quote").filter({ hasText: "League Champion" }).first();
  await expect(championQuote.locator(".market-icon-avatar.champion svg.lucide-trophy")).toHaveCount(1);
  await championQuote.click();
  await expect(page.locator("#market-detail .outcome-list .player-avatar.team")).toHaveCount(12);
  const teamAvatarBoxes = await page.locator("#market-detail .outcome-list .player-avatar.team").evaluateAll((avatars) =>
    avatars.slice(0, 4).map((avatar) => {
      const rect = avatar.getBoundingClientRect();
      return { width: Math.round(rect.width), height: Math.round(rect.height) };
    })
  );
  expect(teamAvatarBoxes.every((box) => box.width === 42 && box.height === 42)).toBe(true);
  await expect(page.locator("#markets-root .playoff-quote").first()).toContainText("to make playoffs");
  const playoffAvatarBox = await page.locator("#markets-root .playoff-quote .team-market-avatar").first().evaluate((avatar) => {
    const rect = avatar.getBoundingClientRect();
    return { width: Math.round(rect.width), height: Math.round(rect.height) };
  });
  expect(playoffAvatarBox).toEqual({ width: 38, height: 38 });
  await expect(page.locator("#markets-root .playoff-quote .side-chip").first()).toContainText(/favored/i);
  await page.locator("#markets-root .playoff-quote").first().click();
  await expect(page.locator("#market-detail .outcome-list .outcome-token.yes")).toHaveCount(1);
  await expect(page.locator("#market-detail .outcome-list .outcome-token.no")).toHaveCount(1);
  await expect(page.locator("#market-detail .outcome-list")).toHaveClass(/binary-outcomes/);
  await expect(page.locator("#market-detail .admin-actions")).toHaveCount(0);
  await expect(page.locator("#market-detail .market-intelligence")).toHaveCount(0);
  await expect(page.locator("#market-detail .automatic-settlement-note")).toContainText("official Sleeper result");
  await expect(page.locator("#market-detail .binary-outcomes .outcome-row > div:nth-child(2) > strong")).toHaveText(["YES", "NO"]);
  const binaryTiles = await page.locator("#market-detail .binary-outcomes .outcome-row").evaluateAll((tiles) =>
    tiles.map((tile) => ({ width: tile.getBoundingClientRect().width, top: tile.getBoundingClientRect().top }))
  );
  expect(binaryTiles).toHaveLength(2);
  expect(Math.abs(binaryTiles[0].width - binaryTiles[1].width)).toBeLessThan(2);
  expect(Math.abs(binaryTiles[0].top - binaryTiles[1].top)).toBeLessThan(2);
  await expect(page.locator("#market-detail")).toContainText("Price tape");
  await page.locator("#markets-root .asset-row").filter({ hasText: "Week 1 Lowest" }).first().click();
  await expect(page.locator("#market-detail .automatic-settlement-note")).toContainText("Tuesday at 1:00 AM ET");
  await page.locator("#markets-root .asset-row").filter({ hasText: marketTitle }).first().click();
  await expect(page.locator("#market-detail .market-intelligence")).toHaveCount(1);
  await expect(page.locator("#market-detail .admin-actions summary")).toContainText("Commissioner controls");
  await page.locator(".outcome-row").first().locator("[data-order]").click();
  await expect(page.locator("#order-sheet")).toBeVisible();
  await expect(page.locator("#order-amount-label")).toHaveText("Credits to spend");
  await expect(page.locator("#order-shares")).toHaveValue("100");
  await expect(page.locator("#order-estimate")).toContainText("Contracts received");
  await expect(page.locator("#order-estimate")).toContainText("Implied probability");
  const orderOutcomeLabel = await page.locator("#order-estimate .order-summary .outcome-token").evaluate((token) => {
    const label = token.querySelector("span");
    const range = document.createRange();
    range.selectNodeContents(label);
    const tokenRect = token.getBoundingClientRect();
    const textRect = range.getBoundingClientRect();
    range.detach();
    return {
      x: Math.abs((tokenRect.left + tokenRect.right - textRect.left - textRect.right) / 2),
      y: Math.abs((tokenRect.top + tokenRect.bottom - textRect.top - textRect.bottom) / 2)
    };
  });
  expect(orderOutcomeLabel.x).toBeLessThan(1);
  expect(orderOutcomeLabel.y).toBeLessThan(3);
  await page.click("#order-submit");
  await expect(page.locator("#order-sheet")).toBeVisible();
  await expect(page.locator("#order-submit")).toContainText("Place Buy");
  await page.click("#order-submit");
  await expect(page.locator("#order-sheet")).toBeHidden();
  await expect(page.locator("#summary-cash")).not.toHaveText("-");
  await expect(page.locator("#ticker-track")).toContainText("BUY");

  await page.click("button[data-tab='account']");
  await page.click("#refresh-identity");
  await expect(page.locator("#identity-root .identity-card").first()).toBeVisible();
  await expect(page.locator("#identity-root")).toContainText("Current claim");

  await page.click("button[data-tab='admin']");
  await page.click("[data-admin-settings-tab='system']");
  await page.click("#demo-season");
  await page.click("button[data-tab='portfolio']");
  await expect(page.locator(".allocation-card")).toBeVisible();
  await expect(page.locator(".demo-banner")).toContainText("Demo season loaded");
  await expect(page.locator(".allocation-total")).toContainText("Total");
  await expect(page.locator("#portfolio-root")).toContainText("sh");

  await page.click("button[data-tab='admin']");
  await expect(page.locator("[data-admin-settings-tab='fund']")).toHaveCount(0);
  expect(fundRequests).toHaveLength(0);

  await page.click("button[data-tab='markets']");
  await page.locator("#market-detail .admin-actions summary").click();
  await page.locator("[data-admin-action='close']").click();
  await expect(page.locator("#market-detail .market-state-banner.closed")).toContainText("Trading is closed");
  await page.click("button[data-tab='admin']");
  await page.click("#admin-settings-back");
  const resolutionAction = page.locator(".admin-inbox-item").filter({ hasText: marketTitle }).locator("button");
  await expect(resolutionAction).toBeVisible();
  await resolutionAction.click();
  await expect(page.locator("#admin-action-dialog")).toBeVisible();
  await page.locator("[data-admin-resolve-outcome]").filter({ hasText: "YES" }).click();
  await expect(page.locator("#admin-action-dialog")).toBeHidden();
  await page.click("button[data-tab='markets']");
  await expect(page.locator("#market-detail .market-state-banner.resolved")).toContainText("Settled");
  await page.click("button[data-tab='leaderboard']");
  await expect(page.locator("#leaderboard-root")).toContainText(traderName);
});
