import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { buildNavItems } from "@/components/layout/AppShell";
import ElementsPanel from "@/components/audit/ElementsPanel";
import { feedbackApi } from "@/api/services";
import { getAuditLabel, getAuditScore } from "@/api/auditState";
import type { components } from "@/api/generated/types";

afterEach(() => vi.restoreAllMocks());

describe("actual audit scores", () => {
  const snapshot = (auditStatus: string, auditScore?: unknown) => ({ taskId: "parse", stage: "done", percent: 1, status: "DONE", result: { auditStatus, auditScore } }) as unknown as components["schemas"]["TaskProgress"];
  it("shows a valid partial score with an explicit review warning", () => {
    expect(getAuditScore(snapshot("DEGRADED", 72))).toBe(72);
    expect(getAuditLabel(snapshot("DEGRADED", 72))).toContain("参考评分，需人工复核");
  });
  it("preserves a real zero and a real full score", () => {
    expect(getAuditScore(snapshot("DONE", 0))).toBe(0);
    expect(getAuditScore(snapshot("DONE", 100))).toBe(100);
  });
  it.each(["RUNNING", "FAILED", "NOT_STARTED"])("hides stale scores when audit is %s", (status) => {
    expect(getAuditScore(snapshot(status, 100))).toBeUndefined();
  });
  it.each([undefined, null, true, "80", NaN, Infinity, -1, 101])("rejects invalid score %s", (score) => {
    expect(getAuditScore(snapshot("DEGRADED", score))).toBeUndefined();
  });
  it("hides a previous score while reparsing and shows real audit progress", () => {
    expect(getAuditScore({ ...snapshot("DONE", 80), status: "RUNNING" })).toBeUndefined();
    expect(getAuditLabel({ ...snapshot("RUNNING"), result: { auditStatus: "RUNNING", auditPercent: .25 } as unknown as components["schemas"]["TaskProgress"]["result"] })).toContain("25%");
    expect(getAuditLabel(snapshot("FAILED"))).toContain("暂无有效评分");
  });
});

describe("real application entry points", () => {
  it("opens the actual selection pages before choosing a contract", () => {
    const items = buildNavItems(null, false);
    expect(items.find((item) => item.label === "审核工作台")?.to).toBe("/audit");
    expect(items.find((item) => item.label === "审核报告")?.to).toBe("/report");
  });
  it("keeps the selected contract when switching to report", () => {
    expect(buildNavItems("real-id", true).find((item) => item.label === "审核报告")?.to).toBe("/contracts/real-id/report");
  });
  it("preserves zero scores and does not present clause chunks as key elements", () => {
    const markup = renderToStaticMarkup(<ElementsPanel healthScore={0} extractedElements={[{ key: "clause-0001", label: "一、", value: "一、付款", confidence: 1, isCustom: false, bbox: null }, { key: "party_a", label: "甲方", value: "真实公司", confidence: .9, isCustom: false, bbox: null }]} onSelectElement={() => {}} riskCounts={{ high: 0, medium: 0, low: 0, conflicts: 0, seals: 0 }} riskFilter="ALL" onRiskFilter={() => {}} />);
    expect(markup).toContain('class="score-ring">0');
    expect(markup).toContain("真实公司");
    expect(markup).not.toContain("一、付款");
    expect(markup).not.toContain("标的与交付");
  });
});

describe("complete feedback data", () => {
  it("loads feedback beyond the first page for statistics and export", async () => {
    type Data = components["schemas"]["FeedbackListData"];
    const list = vi.spyOn(feedbackApi, "list");
    list.mockResolvedValueOnce({ items: Array.from({ length: 100 }, (_, i) => ({ id: String(i) })), total: 101, page: 1, pageSize: 100, hasMore: true } as Data);
    list.mockResolvedValueOnce({ items: [{ id: "last-record" }], total: 101, page: 2, pageSize: 100, hasMore: false } as Data);
    expect((await feedbackApi.listAll()).items.at(-1)?.id).toBe("last-record");
    expect(list).toHaveBeenNthCalledWith(2, { page: 2, pageSize: 100 });
  });
  it("stops when a later feedback page is empty", async () => {
    const list = vi.spyOn(feedbackApi, "list");
    list.mockResolvedValueOnce({ items: [], total: 1, page: 1, pageSize: 100, hasMore: true });
    list.mockResolvedValueOnce({ items: [], total: 1, page: 2, pageSize: 100, hasMore: false });
    expect((await feedbackApi.listAll()).items).toEqual([]);
    expect(list).toHaveBeenCalledTimes(2);
  });
});
