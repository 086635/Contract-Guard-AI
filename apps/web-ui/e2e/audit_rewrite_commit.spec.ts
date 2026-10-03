import { expect, test } from "@playwright/test";

import { login, refreshUntilVisible, selectFixture, uploadAndEnterAudit } from "./helpers";

test("链路 6｜SSE 中断后自动重连/轮询并完成解析", async ({ page }) => {
  await login(page, "admin");
  await page.goto("/contracts?upload=1");
  await selectFixture(page, "e2e-no-clause.docx");
  // 必须以「后端已接受任务」为准再 reload：上传面板按钮在点击瞬间即变为「处理中…」
  // （早于 presign/PUT 真正发出），用它做等待信号会在上传途中触发导航，中断三步上传。
  const accepted = page.waitForResponse(
    (res) => res.request().method() === "POST" && res.url().includes("/api/v1/contracts/upload") && res.ok(),
  );
  await page.getByRole("button", { name: "开始解析" }).click();
  // 等任务已被接受并进入解析态，再断开 SSE；避免在上传/refresh cookie 轮换竞态中刷新。
  await accepted;
  await page.reload();
  // reload 后只等应用自行恢复会话；再次 goto 会与 refresh token 轮换竞争。
  await expect(page).toHaveURL(/\/contracts/, { timeout: 30_000 });
  await expect(page.getByLabel("退出登录")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("e2e-no-clause.docx", { exact: false }).first()).toBeVisible({
    timeout: 240_000,
  });
});

test("链路 7｜真实模型改写并加入提交清单", async ({ page }) => {
  test.setTimeout(420_000);
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-audit.pdf");

  const rewrite = page.getByRole("button", { name: "让 AI 改写" }).first();
  await refreshUntilVisible(page, rewrite);
  await rewrite.click();
  const generate = page.getByRole("button", { name: "生成改写建议" });
  await expect(generate).toBeEnabled({ timeout: 60_000 });
  await generate.click();

  const apply = page.getByRole("button", { name: "采用这版修改" });
  await expect(apply).toBeEnabled({ timeout: 240_000 });
  await apply.click();
  await expect(page.getByText("AI 改写建议已加入提交清单")).toBeVisible();
});

test("链路 8｜提交审核批注并落库", async ({ page }) => {
  test.setTimeout(420_000);
  await login(page, "admin");
  await uploadAndEnterAudit(page, "e2e-audit.pdf");

  const accept = page.locator(".accept-toggle").first();
  await refreshUntilVisible(page, accept);
  await accept.click();

  const commit = page.getByRole("button", { name: "提交批注" });
  await expect(commit).toBeEnabled();
  await commit.click();
  await expect(page.getByText(/已提交/)).toBeVisible({ timeout: 60_000 });
});
