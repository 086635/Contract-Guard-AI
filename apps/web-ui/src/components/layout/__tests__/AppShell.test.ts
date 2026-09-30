import { describe, expect, it } from "vitest";

import { buildNavItems } from "@/components/layout/AppShell";

describe("AppShell navigation", () => {
  it("test_viewer_hides_admin_nav", () => {
    const viewerItems = buildNavItems(null, false);
    const adminItems = buildNavItems(null, true);

    expect(viewerItems.some((item) => item.adminOnly)).toBe(false);
    expect(adminItems.some((item) => item.adminOnly)).toBe(true);
  });
});