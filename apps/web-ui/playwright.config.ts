import { defineConfig, devices } from "@playwright/test";

/**
 * C75 正式浏览器验收配置。
 *
 * - 目标环境：prod compose（nginx TLS / API / Worker / PG / Redis / MinIO / Qdrant）或等价真实栈；
 *   默认 baseURL 指向本地 Vite（开发联调），CI 用 `CG_E2E_BASE_URL` 指向真实入口。
 * - 失败必须留证据：trace / screenshot / video 全部 retain-on-failure。
 * - 串行执行（workers=1）：共享同一真实栈与测试账号，避免相互污染。
 */
const baseURL = process.env.CG_E2E_BASE_URL ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
    ["json", { outputFile: "playwright-report/results.json" }],
  ],
  outputDir: "test-results",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 30_000,
    navigationTimeout: 30_000,
    ignoreHTTPSErrors: true,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
