import { expect, test } from "@playwright/test";

import { expectDownloadMagic, login, openReportPage, uploadAndEnterAudit } from "./helpers";

const DOCX_MAGIC = [0x50, 0x4b, 0x03, 0x04];
const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d];

test("链路 9｜生成报告并下载独立 Word（魔数 PK）", async ({ page }) => {
  await login(page, "admin");
  const contractId = await uploadAndEnterAudit(page, "e2e-native.pdf");
  await openReportPage(page, contractId);

  const word = page.getByRole("button", { name: "下载 Word" });
  await expect(word).toBeEnabled({ timeout: 300_000 });
  const download = await Promise.all([page.waitForEvent("download"), word.click()]).then(
    ([event]) => event,
  );
  await expectDownloadMagic(download, DOCX_MAGIC);
});

test("链路 10｜下载独立 PDF（魔数 %PDF-）", async ({ page }) => {
  await login(page, "admin");
  const contractId = await uploadAndEnterAudit(page, "e2e-native.pdf");
  await openReportPage(page, contractId);

  const pdf = page.getByRole("button", { name: "下载 PDF" });
  await expect(pdf).toBeEnabled({ timeout: 300_000 });
  const download = await Promise.all([page.waitForEvent("download"), pdf.click()]).then(
    ([event]) => event,
  );
  await expectDownloadMagic(download, PDF_MAGIC);
});
