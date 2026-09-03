const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5066",
    trace: "retain-on-failure"
  },
  webServer: {
    command: "python3 tests/e2e_server.py",
    url: "http://127.0.0.1:5066/api/health",
    reuseExistingServer: false,
    timeout: 15_000
  }
});
