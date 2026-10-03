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
  await expect(page.locator('[data-testid="pdf-viewer"] .pdf-status')).toBeVisible();

  // BBox 高亮按用户当前选择渲染：先点击“甲方”，再验证坐标图层与页码状态。
  const partyElement = page.locator(".element-item").filter({ hasText: "甲方" }).first();
  await expect(partyElement).toBeVisible({ timeout: 120_000 });
  await partyElement.click();

  const overlays = page.locator(".bbox-overlay");
  await expect(overlays.first()).toBeVisible({ timeout: 30_000 });
  await overlays.first().click({ force: true });
  await expect(page.locator('[data-testid="pdf-viewer"] .pdf-status')).toContainText(/第 \d+ 页/);
});
