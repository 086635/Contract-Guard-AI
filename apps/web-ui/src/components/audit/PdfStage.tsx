/**
 * 审核页 PDF 舞台：预览状态机 + 真实 PDF.js 查看器（C61）。
 *
 * 状态语义（与 C58 `PreviewStatus` 对齐）：
 * · `DONE` + `previewUrl` → 渲染真实 PDF；`FAILED` → 只给「重试转换」，**不显示旧纸张**；
 * · `PENDING`/`RUNNING` → 转换中（由 Audit 轮询刷新，不提供重试按钮）。
 */
import { useEffect, useState } from "react";
import Icon from "@/components/ui/Icon";
import Button from "@/components/ui/Button";
import PdfViewer, { type Highlight, clampPage } from "@/components/pdf-viewer/PdfViewer";
import type { components } from "@/api/generated/types";

type PreviewData = components["schemas"]["PreviewData"];

export type { Highlight };

export type PdfStageProps = {
  fileName: string;
  preview: PreviewData | null;
  highlights: Highlight[];
  onFeedback?: () => void;
  onRetry?: () => void;
  previewRetrying?: boolean;
};

export type PreviewUiState = "loading" | "converting" | "failed" | "unavailable" | "ready";

/** 预览状态模型（唯一处）：把 C58 的 `PreviewStatus` 映射成 UI 分支。 */
export function previewUiState(preview: PreviewData | null): PreviewUiState {
  if (!preview) return "loading";
  if (preview.status === "FAILED") return "failed";
  if (preview.status === "PENDING" || preview.status === "RUNNING") return "converting";
  return preview.previewUrl ? "ready" : "unavailable";
}

const DEFAULT_ZOOM = 100;
const ZOOM_MIN = 50;
const ZOOM_MAX = 300;
const ZOOM_STEP = 10;

export default function PdfStage({ fileName, preview, highlights, onFeedback, onRetry, previewRetrying }: PdfStageProps) {
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);

  // 点击风险/要素 → 跳到其所在页（1-based；页码换算只在查看器内部做夹取）。
  useEffect(() => {
    const target = highlights[0]?.bbox.page;
    if (target) setPage(target);
  }, [highlights]);

  const state = previewUiState(preview);
  const ready = state === "ready";
  const converting = state === "converting";
  const failed = state === "failed";
  const unavailable = state === "unavailable";
  const activeHighlight = ready ? highlights[0] : undefined;

  const toolbar = (
    <div className="pdf-toolbar">
      <div className="pdf-title">
        <Icon name="file-text" size={17} />
        <span>{fileName}</span>
        <small>{ready ? "真实 PDF · 坐标已同步" : converting ? "预览转换中" : failed ? "预览转换失败" : "预览准备中"}</small>
      </div>
      <div className="pdf-tools">
        <button
          onClick={() => setPage((value) => clampPage(value - 1, pageCount))}
          aria-label="上一页"
          disabled={!ready || page <= 1}
        >
          ‹
        </button>
        <span className="page-input">
          <input
            value={page}
            min={1}
            max={Math.max(pageCount, 1)}
            type="number"
            aria-label="页码"
            onChange={(event) => setPage(clampPage(Number(event.target.value), pageCount))}
            disabled={!ready}
          />
          / {pageCount || 1}
        </span>
        <button
          onClick={() => setPage((value) => clampPage(value + 1, pageCount))}
          aria-label="下一页"
          disabled={!ready || (pageCount > 0 && page >= pageCount)}
        >
          ›
        </button>
        <button onClick={() => setZoom((value) => Math.max(ZOOM_MIN, value - ZOOM_STEP))} aria-label="缩小" disabled={!ready}>
          −
        </button>
        <button className="zoom-value" onClick={() => setZoom(DEFAULT_ZOOM)} aria-label="重置缩放" disabled={!ready}>
          {zoom}%
        </button>
        <button onClick={() => setZoom((value) => Math.min(ZOOM_MAX, value + ZOOM_STEP))} aria-label="放大" disabled={!ready}>
          ＋
        </button>
        <button onClick={() => window.print()} aria-label="打印" disabled={!ready}>
          <Icon name="download" size={16} />
        </button>
      </div>
    </div>
  );

  return (
    <section className="pdf-stage">
      {toolbar}
      {ready && preview?.previewUrl ? (
        <div className="pdf-canvas">
          <PdfViewer
            fileUrl={preview.previewUrl}
            page={page}
            zoom={zoom}
            highlights={highlights}
            onPageChange={setPage}
            onPageCountChange={setPageCount}
          />
        </div>
      ) : (
        <div className="pdf-canvas">
          <div className="pdf-preview-state">
            {failed ? (
              <>
                <strong>预览转换失败</strong>
                <span>原始文件已保留。可重试转换，失败不会影响审核数据。</span>
                <Button size="sm" variant="secondary" disabled={previewRetrying} onClick={onRetry} icon={<Icon name="refresh" size={15} />}>
                  {previewRetrying ? "正在重试…" : "重试转换"}
                </Button>
              </>
            ) : converting ? (
              <>
                <strong>正在生成预览</strong>
                <span>DOCX 正在转换为 PDF，完成后会自动刷新（无需手动操作）。</span>
                <span className="state-spinner" />
              </>
            ) : unavailable ? (
              <>
                <strong>预览链接不可用</strong>
                <span>服务端尚未返回可用的预览地址，请重试或稍后刷新。</span>
                <Button size="sm" variant="secondary" disabled={previewRetrying} onClick={onRetry} icon={<Icon name="refresh" size={15} />}>
                  {previewRetrying ? "正在重试…" : "重试转换"}
                </Button>
              </>
            ) : (
              <>
                <strong>正在获取预览</strong>
                <span>正在读取合同预览元数据…</span>
              </>
            )}
          </div>
        </div>
      )}
      <div className="pdf-status">
        <span>
          <i className={`legend-box ${activeHighlight?.kind ?? "risk"}`} />
          {activeHighlight ? `${activeHighlight.label} · 第 ${activeHighlight.bbox.page} 页` : "选择要素或风险后定位原文"}
        </span>
        <span>点击高亮框可提交人工反馈</span>
      </div>
      {activeHighlight && ready && (
        <button className="bbox-feedback" onClick={onFeedback} aria-label="对高亮条款提交反馈">
          对「{activeHighlight.label}」提交反馈
        </button>
      )}
    </section>
  );
}
