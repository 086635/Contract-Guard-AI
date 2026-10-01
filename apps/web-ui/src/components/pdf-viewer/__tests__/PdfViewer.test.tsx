/**
 * C61 PDF.js 查看器单测（node 环境：纯映射 + 状态模型 + 接线守卫）。
 *
 * 环境说明：本仓库 vitest 无 jsdom/测试库，因此不渲染组件；改为
 * 「纯函数断言 + 源码接线守卫」——后者与后端 `test_api_no_mock_guard` 同款做法。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { previewUiState } from "@/components/audit/PdfStage";
import {
  clampPage,
  highlightsForPage,
  normalizeScale,
  toViewportRect,
  type Highlight,
} from "@/components/pdf-viewer/PdfViewer";
import type { components } from "@/api/generated/types";

type PreviewData = components["schemas"]["PreviewData"];
type BBox = components["schemas"]["BBox"];

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const VIEWER_SOURCE = read("../PdfViewer.tsx");
const STAGE_SOURCE = read("../../audit/PdfStage.tsx");
const AUDIT_SOURCE = read("../../../views/Audit.tsx");
const SERVICES_SOURCE = read("../../../api/services.ts");

const bbox = (overrides: Partial<BBox> = {}): BBox => ({
  page: 1,
  x: 72,
  y: 130,
  width: 180,
  height: 12,
  coordinateSystem: "PDF_POINTS",
  ...overrides,
});

const preview = (overrides: Partial<PreviewData> = {}): PreviewData => ({
  contractId: "00000000-0000-0000-0000-0000000000d1",
  status: "DONE",
  previewUrl: "https://minio.test/contracts/2026/10/x.pdf?sig=1",
  expiresIn: 900,
  pageCount: null,
  converterVersion: null,
  createdAt: "2026-10-01T00:00:00Z",
  ...overrides,
});

describe("C61 PDF 查看器", () => {
  it("test_pdf_preview_uses_real_url", () => {
    // 查看器把真实签名 URL 交给 pdfjs（不是占位文本、不是 iframe）
    expect(VIEWER_SOURCE).toContain("pdfjs.getDocument({ url: fileUrl })");
    expect(VIEWER_SOURCE).toContain("data-file-url={fileUrl}");
    expect(VIEWER_SOURCE).not.toContain("<iframe");

    // 舞台把后端 previewUrl 原样透传给查看器
    expect(STAGE_SOURCE).toContain("fileUrl={preview.previewUrl}");

    // 预览 URL 来自 C58 的预览接口与短期签名 URL
    expect(SERVICES_SOURCE).toContain("`/contracts/${contractId}/preview`");
    expect(AUDIT_SOURCE).toContain("previewApi.get(selectedId)");

    // 旧硬编码纸张（固定采购合同文本/编号/金额）必须已移除
    for (const legacy of ["采购合同", "HT-2026-0818", "伍拾万元整", "paper-header"]) {
      expect(STAGE_SOURCE).not.toContain(legacy);
      expect(VIEWER_SOURCE).not.toContain(legacy);
    }
  });

  it("test_bbox_scale_mapping", () => {
    // 1x：PDF_POINTS 与 CSS 像素 1:1（左上原点，不做 Y 翻转）
    expect(toViewportRect(bbox(), { width: 595, height: 842 }, 1)).toEqual({
      left: 72,
      top: 130,
      width: 180,
      height: 12,
    });

    // 2x：等比放大
    expect(toViewportRect(bbox(), { width: 1190, height: 1684 }, 2)).toEqual({
      left: 144,
      top: 260,
      width: 360,
      height: 24,
    });

    // 越界夹取：右侧/底部不越出画布
    expect(toViewportRect(bbox({ x: 580, y: 830, width: 100, height: 100 }), { width: 595, height: 842 }, 1)).toEqual({
      left: 580,
      top: 830,
      width: 15,
      height: 12,
    });

    // 完全越界 / 退化 ⇒ 零矩形（调用方不渲染）
    expect(toViewportRect(bbox({ x: 700, y: 900 }), { width: 595, height: 842 }, 1)).toEqual({
      left: 0,
      top: 0,
      width: 0,
      height: 0,
    });
    expect(toViewportRect(bbox({ width: 0 }), { width: 595, height: 842 }, 1).width).toBe(0);

    // 非法 scale 退回 1，避免 NaN 传进样式
    expect(toViewportRect(bbox(), { width: 595, height: 842 }, Number.NaN).width).toBe(180);

    // 跨页隔离：只渲染当前页的 BBox
    const items: Highlight[] = [
      { id: "r1", label: "风险 1", kind: "risk", bbox: bbox({ page: 1 }) },
      { id: "r2", label: "风险 2", kind: "risk", bbox: bbox({ page: 3 }) },
    ];
    expect(highlightsForPage(items, 1).map((item) => item.id)).toEqual(["r1"]);
    expect(highlightsForPage(items, 3).map((item) => item.id)).toEqual(["r2"]);
  });

  it("test_page_navigation", () => {
    expect(clampPage(1, 8)).toBe(1);
    expect(clampPage(0, 8)).toBe(1);
    expect(clampPage(9, 8)).toBe(8);
    expect(clampPage(4, 8)).toBe(4);
    expect(clampPage(Number.NaN, 8)).toBe(1);
    // 页数未知（尚未加载完成）时只保证 ≥1
    expect(clampPage(3, 0)).toBe(3);
    expect(clampPage(-5, 0)).toBe(1);

    // 缩放夹取在 50%~300%，非法值退回 1
    expect(normalizeScale(100)).toBe(1);
    expect(normalizeScale(0)).toBe(0.5);
    expect(normalizeScale(1000)).toBe(3);
    expect(normalizeScale(Number.NaN)).toBe(1);
  });

  it("test_preview_failed_retry", () => {
    // 状态模型：FAILED 既不显示旧纸张也不静默当成功
    expect(previewUiState(null)).toBe("loading");
    expect(previewUiState(preview({ status: "PENDING", previewUrl: null }))).toBe("converting");
    expect(previewUiState(preview({ status: "RUNNING", previewUrl: null }))).toBe("converting");
    expect(previewUiState(preview({ status: "FAILED", previewUrl: null }))).toBe("failed");
    expect(previewUiState(preview({ status: "DONE", previewUrl: null }))).toBe("unavailable");
    expect(previewUiState(preview())).toBe("ready");

    // 失败态提供「重试转换」入口，且重试真的打到后端的 retry 接口
    expect(STAGE_SOURCE).toContain("重试转换");
    expect(STAGE_SOURCE).toContain("onClick={onRetry}");
    expect(AUDIT_SOURCE).toContain("previewApi.retry(selectedId)");
    expect(SERVICES_SOURCE).toContain("`/contracts/${contractId}/preview/retry`");

    // 失败态不得渲染硬编码纸张（旧实现的问题）
    expect(STAGE_SOURCE).not.toContain('className="paper"');
  });
});
