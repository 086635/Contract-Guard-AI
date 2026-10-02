import { expect, test } from "@playwright/test";

import { login, logout, uploadAndEnterAudit } from "./helpers";

test("链路 11｜反馈提交后可在反馈中心查询", async ({ page }) => {
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-native.pdf");

  await page.getByLabel("反馈").first().click();
  const note = page.getByPlaceholder("说明判断依据，或填写正确的风险描述");
  await note.fill("C75 E2E 反馈样例");
  await page.getByRole("button", { name: "提交反馈" }).click();
  await expect(page.getByText(/反馈已提交/)).toBeVisible({ timeout: 30_000 });

  await page.goto("/feedback");
  await expect(page.getByText("C75 E2E 反馈样例").first()).toBeVisible({ timeout: 60_000 });
});

test("链路 12｜VIEWER 权限：设置入口隐藏且直连被拒", async ({ page }) => {
  await login(page, "viewer");
  await expect(page.getByRole("link", { name: /工作台设置/ })).toHaveCount(0);

  const denied = await page.request.get("/api/v1/admin/standard-clauses");
  expect([401, 403]).toContain(denied.status());
});

test("链路 13｜登出后受保护路由跳回登录页", async ({ page }) => {
  await login(page, "admin");
  await logout(page);
  await page.goto("/contracts");
  await expect(page).toHaveURL(/\/login/);
});
