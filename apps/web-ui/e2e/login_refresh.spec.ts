import { expect, test } from "@playwright/test";

import { login, logout } from "./helpers";

test("链路 1｜登录后进入合同工作台", async ({ page }) => {
  await login(page, "admin");
  await expect(page.getByLabel("退出登录")).toBeVisible();
  await expect(page.getByRole("heading", { name: /合同管理与导入中心/ })).toBeVisible();
  await logout(page);
});

test("链路 2｜刷新页面后会话自动恢复", async ({ page }) => {
  await login(page, "admin");
  await page.reload();
  await expect(page).toHaveURL(/\/contracts/);
  await expect(page.getByLabel("退出登录")).toBeVisible();
  await logout(page);
});
