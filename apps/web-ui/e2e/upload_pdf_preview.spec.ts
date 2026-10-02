import { expect, test } from "@playwright/test";

import { login, uploadAndEnterAudit } from "./helpers";

test("链路 3｜上传 PDF 并完成解析进入审核页", async ({ page }) => {
  await login(page, "admin");
  const contractId = await uploadAndEnterAudit(page, "e2e-native.pdf");
  await expect(page.getByText(contractId, { exact: false })).toBeVisible();
});

test("链路 4｜PDF.js 渲染真实页并可按 BBox 高亮定位", async ({ page }) => {
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-native.pdf");

  await expect(page.locator(".pdf-page-canvas")).toBeVisible({ timeout: 90_000 });
  await expect(page.locator(".pdf-status")).toBeVisible();

  const overlays = page.locator(".bbox-overlay");
  expect(await overlays.count()).toBeGreaterThan(0);
  await overlays.first().click({ force: true });
  await expect(page.locator(".pdf-status")).toContainText(/第 \d+ 页/);
});
