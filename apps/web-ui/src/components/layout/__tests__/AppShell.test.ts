import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";

import AppShell, { buildNavItems } from "@/components/layout/AppShell";

describe("AppShell navigation", () => {
  it("test_viewer_hides_admin_nav", () => {
    const viewerItems = buildNavItems(null, false);
    const adminItems = buildNavItems(null, true);

    expect(viewerItems.some((item) => item.adminOnly)).toBe(false);
    expect(adminItems.some((item) => item.adminOnly)).toBe(true);
  });

  it.each([
    ["/contracts", "合同中心"],
    ["/contracts/selected-contract/audit", "审核工作台"],
    ["/contracts/selected-contract/report", "审核报告"],
  ])("highlights only the current main navigation on %s", (path, label) => {
    const markup = renderToStaticMarkup(createElement(StaticRouter, { location: path }, createElement(AppShell)));
    const activeLinks = markup.match(/<a\b[^>]*aria-current="page"[^>]*>[\s\S]*?<\/a>/g) ?? [];
    expect(activeLinks).toHaveLength(1);
    expect(activeLinks[0]).toContain(`<b>${label}</b>`);
    expect(activeLinks[0]).toContain("nav-item active");
  });
});
