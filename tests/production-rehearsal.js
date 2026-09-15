const { chromium } = require("@playwright/test");

const baseURL = process.env.LEAGUE_MARKET_URL || "http://127.0.0.1:5072";
const inviteCode = process.env.LEAGUE_MARKET_INVITE_CODE;

if (!inviteCode) {
  throw new Error("LEAGUE_MARKET_INVITE_CODE is required");
}

async function inspectMarket(browser, label, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const pageErrors = [];
  const apiFailures = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/") && response.status() >= 400) {
      apiFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await page.goto(baseURL, { waitUntil: "networkidle" });
  await page.fill("#invite-code", inviteCode);
  await page.locator(".join-advanced summary").click();
  await page.fill("#display-name", `Production QA ${label} ${Date.now()}`);
  await page.click("#join-submit");
  await page.locator("#tour-close").click();

  if (viewport.width <= 980) {
    await page.locator("#nav-toggle").click();
  }
  await page.locator("button[data-tab='markets']").click();
  const playoffMarket = page.locator("#markets-root .playoff-quote");
  await playoffMarket.first().click();
  await page.locator("#market-detail .binary-outcomes").waitFor({ state: "visible" });

  const audit = await page.evaluate(() => {
    const tiles = [...document.querySelectorAll("#market-detail .binary-outcomes .outcome-row")]
      .map((tile) => {
        const rect = tile.getBoundingClientRect();
        return {
          width: Math.round(rect.width),
          top: Math.round(rect.top),
          bottom: Math.round(rect.bottom),
          left: Math.round(rect.left),
          height: Math.round(rect.height)
        };
      });
    return {
      viewport: innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      tiles,
      hasPriceTape: document.querySelector("#market-detail")?.textContent.includes("Price tape") || false,
      commissionerControls: document.querySelectorAll("#market-detail .admin-actions").length,
      marketDetails: document.querySelectorAll("#market-detail .market-intelligence").length,
      settlementNote: document.querySelector("#market-detail .automatic-settlement-note")?.textContent.trim() || "",
      outcomeLabels: [...document.querySelectorAll("#market-detail .binary-outcomes .outcome-row > div:nth-child(2) > strong")].map((node) => node.textContent.trim()),
    };
  });

  await page.screenshot({ path: `/private/tmp/league-market-production-${label}.png`, fullPage: true });
  await context.close();
  return { label, ...audit, pageErrors, apiFailures };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const results = [];
    results.push(await inspectMarket(browser, "mobile", { width: 390, height: 844 }));
    results.push(await inspectMarket(browser, "desktop", { width: 1440, height: 1000 }));
    for (const result of results) {
      if (result.documentWidth !== result.viewport) throw new Error(`${result.label}: horizontal overflow`);
      if (result.tiles.length !== 2) throw new Error(`${result.label}: expected two binary outcomes`);
      if (Math.abs(result.tiles[0].width - result.tiles[1].width) > 1) throw new Error(`${result.label}: unequal tile widths`);
      if (result.viewport <= 560) {
        if (result.tiles[1].top <= result.tiles[0].bottom) throw new Error(`${result.label}: stacked outcome tiles overlap`);
        if (Math.abs(result.tiles[0].left - result.tiles[1].left) > 1) throw new Error(`${result.label}: stacked outcome tiles are offset`);
      } else if (Math.abs(result.tiles[0].top - result.tiles[1].top) > 1) {
        throw new Error(`${result.label}: outcome tiles are not aligned`);
      }
      if (!result.hasPriceTape) throw new Error(`${result.label}: price tape is missing`);
      if (result.commissionerControls !== 0) throw new Error(`${result.label}: commissioner controls leaked into markets`);
      if (result.marketDetails !== 0) throw new Error(`${result.label}: modeled market details dropdown is still visible`);
      if (!result.settlementNote.includes("official Sleeper result")) throw new Error(`${result.label}: automatic settlement status is missing`);
      if (result.outcomeLabels.join(",") !== "YES,NO") throw new Error(`${result.label}: binary outcomes are not in canonical order`);
      if (result.pageErrors.length || result.apiFailures.length) throw new Error(`${result.label}: browser errors detected`);
    }
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
