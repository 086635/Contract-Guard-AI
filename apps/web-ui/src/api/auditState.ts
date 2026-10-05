import type { components } from "@/api/generated/types";

type Progress = components["schemas"]["TaskProgress"];

/** Only the current completed audit snapshot may supply a score. */
export function getAuditScore(progress: Progress | null): number | undefined {
  if (progress?.status !== "DONE") return undefined;
  const status = progress?.result?.auditStatus;
  const score = progress?.result?.auditScore;
  if (status !== "DONE" && status !== "DEGRADED") return undefined;
  return typeof score === "number" && Number.isFinite(score) && score >= 0 && score <= 100
    ? score : undefined;
}

export function getAuditLabel(progress: Progress | null): string {
  const result = progress?.result;
  if (progress?.status !== "DONE") return "智能审核等待解析完成";
  if (result?.auditStatus === "RUNNING") {
    const percent = typeof result.auditPercent === "number" ? Math.round(result.auditPercent * 100) : 0;
    return `智能审核进行中 · ${percent}%`;
  }
  if (result?.auditStatus === "DEGRADED") return "智能审核已返回 · 参考评分，需人工复核";
  if (result?.auditStatus === "DONE") return "智能审核已完成 · 请确认审核建议";
  if (result?.auditStatus === "FAILED") return "智能审核失败 · 暂无有效评分";
  return "智能审核尚未开始";
}
