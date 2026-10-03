import { expect, test } from "@playwright/test";

import { login, uploadAndEnterAudit } from "./helpers";

test("链路 5｜上传 DOCX → LibreOffice 转换 PDF → 真实预览渲染", async ({ page }) => {
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-contract.docx");

  // DOCX 先经 convert 队列产出 PDF，再交给 PDF.js 渲染（不允许占位纸张）
  await expect(page.locator(".pdf-page-canvas")).toBeVisible({ timeout: 300_000 });
  await expect(page.locator('[data-testid="pdf-viewer"] .pdf-status')).toContainText(/第 \d+ 页/);
});
