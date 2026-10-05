import { afterEach, describe, expect, it, vi } from "vitest";
import httpClient from "@/api/client";
import { knowledgeApi } from "@/api/knowledge";

afterEach(() => vi.restoreAllMocks());
describe("knowledge preview", () => {
  it("passes selected library and keeps actual citation content and source", async () => {
    const get = vi.spyOn(httpClient, "get").mockResolvedValue({ data: { code: "SUCCESS", traceId: "test", serverTime: "2026-10-05T12:00:00Z", data: { status: "OK", warnings: [], contextTokens: 50,
      items: [{ sourceType: "LAW_ARTICLE", sourceTitle: "民法典第五百七十七条", contentChunk: "真实法条正文", relevanceScore: .81, sourceUrl: "https://www.gov.cn/law", scope: "全国" }] } } });
    const result = await knowledgeApi.search("逾期付款", "law", "PURCHASE");
    expect(get).toHaveBeenCalledWith("/knowledge/search", { params: { query: "逾期付款", library: "law", contractType: "PURCHASE" }, _operationId: "searchKnowledge" });
    expect(result.items[0].contentChunk).toBe("真实法条正文");
    expect(result.items[0].sourceUrl).toBe("https://www.gov.cn/law");
  });
  it("rejects malformed responses instead of displaying invented citations", async () => {
    vi.spyOn(httpClient, "get").mockResolvedValue({ data: { code: "SUCCESS", traceId: "test", serverTime: "2026-10-05T12:00:00Z", data: { status: "OK", warnings: [], items: [{ relevanceScore: "high" }] } } });
    await expect(knowledgeApi.search("合同责任", "all", "PURCHASE")).rejects.toThrow();
  });
  it("preserves empty recall and its actual warnings", async () => {
    vi.spyOn(httpClient, "get").mockResolvedValue({ data: { code: "SUCCESS", traceId: "test", serverTime: "2026-10-05T12:00:00Z", data: { status: "EMPTY", warnings: ["本地服务不可用"], contextTokens: 0, items: [] } } });
    const result = await knowledgeApi.search("合同责任", "clauses", "PURCHASE");
    expect(result.status).toBe("EMPTY");
    expect(result.items).toEqual([]);
    expect(result.warnings).toEqual(["本地服务不可用"]);
  });
});
