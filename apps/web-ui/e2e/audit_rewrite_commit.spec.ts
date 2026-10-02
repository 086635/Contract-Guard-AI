import { expect, test } from "@playwright/test";

import { fixturePath, login, uploadAndEnterAudit } from "./helpers";

test("链路 6｜SSE 中断后自动重连/轮询并完成解析", async ({ page }) => {
  await login(page, "admin");
  await page.goto("/contracts?upload=1");
  await page.locator('input[type="file"]').setInputFiles(fixturePath("e2e-native.pdf"));
  await page.getByRole("button", { name: "开始解析" }).click();
  // 解析进行中刷新页面：模拟 SSE 断线（C65 切轮询，任务继续）
  await page.reload();
  await page.goto("/contracts");
  await expect(page.getByText("e2e-native.pdf", { exact: false }).first()).toBeVisible({
    timeout: 240_000,
  });
});

test("链路 7｜真实模型改写并加入提交清单", async ({ page }) => {
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-contract.docx");

  await page.getByRole("button", { name: "让 AI 改写" }).first().click();
  const generate = page.getByRole("button", { name: "生成改写建议" });
  await expect(generate).toBeEnabled({ timeout: 60_000 });
  await generate.click();

  const apply = page.getByRole("button", { name: "采用这版修改" });
  await expect(apply).toBeEnabled({ timeout: 240_000 });
  await apply.click();
  await expect(page.getByText("AI 改写建议已加入提交清单")).toBeVisible();
});

test("链路 8｜提交审核批注并落库", async ({ page }) => {
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-native.pdf");

  const accept = page.locator(".accept-toggle").first();
  await expect(accept).toBeVisible({ timeout: 120_000 });
  await accept.click();

  const commit = page.getByRole("button", { name: "提交批注" });
  await expect(commit).toBeEnabled();
  await commit.click();
  await expect(page.getByText(/已提交/)).toBeVisible({ timeout: 60_000 });
});
