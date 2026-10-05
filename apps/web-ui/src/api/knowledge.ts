import { z } from "zod";
import httpClient from "@/api/client";

import { schemas } from "@/api/generated/zod";
export type KnowledgeState = z.infer<typeof schemas.KnowledgeSourcesResponse>["data"];
export type KnowledgeResult = z.infer<typeof schemas.KnowledgeSearchResponse>["data"];
export const knowledgeApi = {
  async sources(signal?: AbortSignal): Promise<KnowledgeState> {
    return schemas.KnowledgeSourcesResponse.parse((await httpClient.get("/knowledge/sources", { signal, _operationId: "getKnowledgeSources" })).data).data;
  },
  async search(query: string, library: "all" | "clauses" | "law", contractType: string): Promise<KnowledgeResult> {
    return schemas.KnowledgeSearchResponse.parse((await httpClient.get("/knowledge/search", { params: { query, library, contractType }, _operationId: "searchKnowledge" })).data).data;
  },
};
