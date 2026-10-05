import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { contractApi, taskApi } from "@/api/services";
import ContractRow, { type ContractItem } from "@/components/contracts/ContractRow";
import { averageHealthScore, loadContractItems, scheduleContractRefresh } from "@/views/Dashboard";
import type { components } from "@/api/generated/types";

type Summary = components["schemas"]["ContractSummary"];
type TaskResult = components["schemas"]["TaskProgress"]["result"];

function summary(id: string, status: Summary["status"] = "PENDING_REVIEW"): Summary {
  return {
    id, status, fileName: `合同-${id}.pdf`, contractType: "PURCHASE",
    createdAt: "2026-10-05T00:00:00Z", riskCount: 3,
    healthScore: { overall: 0 } as Summary["healthScore"],
  };
}

function page(items: Summary[], total = items.length, hasMore = false, pageNumber = 1) {
  return { items, total, hasMore, page: pageNumber, pageSize: 50 };
}

const contract: ContractItem = { id: "contract-1", name: "采购合同.pdf", type: "采购合同", date: "2026-10-05", status: "待审核", score: 0, riskCount: 3 };

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("Dashboard data", () => {
  it("includes the contract beyond the first 50 results", async () => {
    const first = Array.from({ length: 50 }, (_, index) => summary(String(index + 1)));
    const list = vi.spyOn(contractApi, "list")
      .mockResolvedValueOnce(page(first, 51, true))
      .mockResolvedValueOnce(page([summary("51")], 51, false, 2));
    const items = await loadContractItems();
    expect(items).toHaveLength(51);
    expect(items.find((item) => item.name.includes("合同-51"))?.id).toBe("51");
    expect(list).toHaveBeenNthCalledWith(2, { page: 2, pageSize: 50 });
  });

  it("stops on an empty page even when hasMore is true", async () => {
    const list = vi.spyOn(contractApi, "list").mockResolvedValue(page([], 1, true));
    await expect(loadContractItems()).resolves.toEqual([]);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("reads a completed parse and reloads the final summary", async () => {
    const list = vi.spyOn(contractApi, "list")
      .mockResolvedValueOnce(page([summary("done", "PARSING")]))
      .mockResolvedValueOnce(page([summary("done", "PENDING_REVIEW")]));
    const parse = vi.spyOn(contractApi, "parseStatus").mockResolvedValue({ taskId: "task-1", stage: "finished", percent: 1, status: "DONE" });
    await expect(loadContractItems()).resolves.toMatchObject([{ id: "done", status: "待审核", riskCount: 3 }]);
    expect(parse).toHaveBeenCalledWith("done");
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("shows a failed parse instead of an indefinitely running task", async () => {
    vi.spyOn(contractApi, "list").mockResolvedValue(page([summary("failed", "PARSING")]));
    vi.spyOn(contractApi, "parseStatus").mockResolvedValue({ taskId: "task-2", stage: "failed", percent: 0, status: "FAILED" });
    await expect(loadContractItems()).resolves.toMatchObject([{ status: "解析失败" }]);
  });

  it("uses the real task result when the recorded parse and summary are still running", async () => {
    vi.spyOn(contractApi, "list").mockResolvedValue(page([summary("done", "PARSING")]));
    vi.spyOn(contractApi, "parseStatus").mockResolvedValue({ taskId: "actual-task", stage: "queued", percent: 0, status: "RUNNING" });
    const task = vi.spyOn(taskApi, "get").mockResolvedValue({ taskId: "actual-task", stage: "finished", percent: 1, status: "DONE", result: { status: "DEGRADED" } as unknown as TaskResult });
    await expect(loadContractItems()).resolves.toMatchObject([{ status: "待审核" }]);
    expect(task).toHaveBeenCalledWith("actual-task");
  });

  it("does not treat a failed result envelope as a successful parse", async () => {
    vi.spyOn(contractApi, "list").mockResolvedValue(page([summary("failed", "PARSING")]));
    vi.spyOn(contractApi, "parseStatus").mockResolvedValue({ taskId: "failed-task", stage: "queued", percent: 0, status: "RUNNING" });
    vi.spyOn(taskApi, "get").mockResolvedValue({ taskId: "failed-task", stage: "finished", percent: 1, status: "DONE", result: { status: "FAILED" } as unknown as TaskResult });
    await expect(loadContractItems()).resolves.toMatchObject([{ status: "解析失败" }]);
  });

  it("does not query parse status for a completed contract", async () => {
    vi.spyOn(contractApi, "list").mockResolvedValue(page([summary("final", "FINALIZED")]));
    const parse = vi.spyOn(contractApi, "parseStatus");
    await expect(loadContractItems()).resolves.toMatchObject([{ status: "已完成" }]);
    expect(parse).not.toHaveBeenCalled();
  });

  it("preserves a real synchronization failure", async () => {
    vi.spyOn(contractApi, "list").mockResolvedValue(page([summary("running", "PARSING")]));
    const error = new Error("无权读取解析状态");
    vi.spyOn(contractApi, "parseStatus").mockRejectedValue(error);
    await expect(loadContractItems()).rejects.toBe(error);
  });

  it("keeps zero as a valid score and distinguishes an empty list", () => {
    expect(averageHealthScore([contract])).toBe(0);
    expect(averageHealthScore([])).toBeNull();
  });
});

describe("Dashboard refresh lifecycle", () => {
  it("refreshes after five seconds only while parsing", () => {
    vi.useFakeTimers(); vi.stubGlobal("window", globalThis);
    const refresh = vi.fn();
    const cleanup = scheduleContractRefresh([{ ...contract, status: "解析中" }], refresh);
    vi.advanceTimersByTime(4999); expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1); expect(refresh).toHaveBeenCalledTimes(1);
    cleanup();
  });

  it("clears a pending refresh on cleanup", () => {
    vi.useFakeTimers(); vi.stubGlobal("window", globalThis);
    const refresh = vi.fn();
    scheduleContractRefresh([{ ...contract, status: "解析中" }], refresh)();
    vi.advanceTimersByTime(5000); expect(refresh).not.toHaveBeenCalled();
  });

  it("does not refresh terminal or empty lists", () => {
    vi.useFakeTimers(); vi.stubGlobal("window", globalThis);
    const refresh = vi.fn();
    scheduleContractRefresh([contract, { ...contract, status: "解析失败" }], refresh);
    scheduleContractRefresh([], refresh);
    vi.advanceTimersByTime(10000); expect(refresh).not.toHaveBeenCalled();
  });
});

describe("Contract risk summaries", () => {
  it.each(["table", "cards"] as const)("shows the real total without inventing risk levels in %s view", (view) => {
    const markup = renderToStaticMarkup(<StaticRouter location="/contracts"><ContractRow contract={contract} view={view} /></StaticRouter>);
    expect(markup).toContain("风险 3");
    expect(markup).not.toContain("badge-high");
    expect(markup).not.toContain("badge-low");
  });

  it("distinguishes a missing count from a confirmed zero", () => {
    const render = (riskCount?: number) => renderToStaticMarkup(<StaticRouter location="/contracts"><ContractRow contract={{ ...contract, riskCount }} /></StaticRouter>);
    expect(render()).toContain("风险待同步");
    expect(render(0)).toContain("风险 0");
  });
});
