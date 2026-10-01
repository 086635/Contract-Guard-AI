/**
 * PDF.js 查看器：Canvas + TextLayer + BBox Overlay（C61）。
 *
 * 设计要点：
 * 1. **坐标语义不在这儿改**：后端给的是 `PDF_POINTS`、**左上原点**、1-based page；
 *    本组件只做「点 → CSS 像素」的等比换算 + 越界夹取（§⑨-1/2）。
 * 2. `pdfjs-dist` 用**动态 import**：模块可在无 DOM 环境导入（单测只验证换算与状态模型）。
 * 3. Overlay 随 `zoom` / `page` 变化重算（React 依赖数组即失效条件）。
 */
import { useEffect, useRef, useState } from "react";
import type { components } from "@/api/generated/types";

type BBox = components["schemas"]["BBox"];

export type Highlight = {
  id: string;
  label: string;
  kind: "risk" | "element" | "seal";
  bbox: BBox;
};

export type ViewportLike = { width: number; height: number };
export type ViewportRect = { left: number; top: number; width: number; height: number };

export type PdfViewerProps = {
  fileUrl: string;
  page: number;
  zoom: number;
  highlights: Highlight[];
  onPageChange?: (page: number) => void;
  onPageCountChange?: (pageCount: number) => void;
};

const MIN_ZOOM = 50;
const MAX_ZOOM = 300;

/** 缩放百分比 → PDF.js viewport scale（限制在 50%~300%，避免 0/NaN 传进渲染）。 */
export function normalizeScale(zoom: number): number {
  if (!Number.isFinite(zoom)) return 1;
  const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
  return clamped / 100;
}

/** 页码夹取到 `[1, pageCount]`（1-based；pageCount 未知时只保证 ≥1）。 */
export function clampPage(page: number, pageCount: number): number {
  const upper = Number.isFinite(pageCount) && pageCount > 0 ? Math.floor(pageCount) : Number.POSITIVE_INFINITY;
  const value = Number.isFinite(page) ? Math.floor(page) : 1;
  return Math.min(upper, Math.max(1, value));
}

/** 只保留当前页的高亮（跨页 BBox 在渲染层按页隔离）。 */
export function highlightsForPage(highlights: Highlight[], page: number): Highlight[] {
  return highlights.filter((item) => item.bbox.page === page);
}

/**
 * `PDF_POINTS`（左上原点）→ 当前 viewport 的 CSS 像素矩形。
 *
 * `viewport` 是该缩放下的画布尺寸（= 页尺寸 × scale），`bbox` 是未缩放的 PDF 点，
 * 因此两边都乘 `scale`；越界部分按画布夹取，退化矩形返回零矩形（调用方应不渲染）。
 */
export function toViewportRect(bbox: BBox, viewport: ViewportLike, scale: number): ViewportRect {
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1;
  const viewportWidth = Math.max(0, Number.isFinite(viewport.width) ? viewport.width : 0);
  const viewportHeight = Math.max(0, Number.isFinite(viewport.height) ? viewport.height : 0);

  const left = Math.min(viewportWidth, Math.max(0, bbox.x * safeScale));
  const top = Math.min(viewportHeight, Math.max(0, bbox.y * safeScale));
  const width = Math.max(0, Math.min(viewportWidth - left, bbox.width * safeScale));
  const height = Math.max(0, Math.min(viewportHeight - top, bbox.height * safeScale));
  if (width <= 0 || height <= 0) return { left: 0, top: 0, width: 0, height: 0 };
  return { left, top, width, height };
}

export default function PdfViewer({ fileUrl, page, zoom, highlights, onPageChange, onPageCountChange }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState<string | null>(null);
  const [viewportSize, setViewportSize] = useState<ViewportLike>({ width: 0, height: 0 });

  const scale = normalizeScale(zoom);

  useEffect(() => {
    let cancelled = false;
    let renderTask: { cancel: () => void; promise: Promise<void> } | null = null;
    let textLayer: { cancel?: () => void } | null = null;
    let loadedDocument: { destroy: () => Promise<void> } | null = null;

    const render = async () => {
      setStatus("loading");
      setMessage(null);
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
        const pdfDocument = await pdfjs.getDocument({ url: fileUrl }).promise;
        loadedDocument = pdfDocument;
        if (cancelled) {
          await pdfDocument.destroy();
          loadedDocument = null;
          return;
        }
        const pageCount = pdfDocument.numPages;
        onPageCountChange?.(pageCount);
        const targetPage = clampPage(page, pageCount);
        if (targetPage !== page) onPageChange?.(targetPage);

        const pdfPage = await pdfDocument.getPage(targetPage);
        const viewport = pdfPage.getViewport({ scale });
        const canvas = canvasRef.current;
        const textLayerHost = textLayerRef.current;
        if (!canvas || !textLayerHost) return;

        const context = canvas.getContext("2d");
        if (!context) throw new Error("canvas context unavailable");
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;

        renderTask = pdfPage.render({ canvasContext: context, viewport });
        await renderTask.promise;
        if (cancelled) return;
        setViewportSize({ width: viewport.width, height: viewport.height });

        textLayerHost.replaceChildren();
        textLayerHost.style.setProperty("--scale-factor", String(viewport.scale));
        textLayerHost.style.width = `${viewport.width}px`;
        textLayerHost.style.height = `${viewport.height}px`;
        const layer = new pdfjs.TextLayer({
          textContentSource: pdfPage.streamTextContent(),
          container: textLayerHost,
          viewport,
        });
        textLayer = layer;
        await layer.render();
        if (cancelled) return;
        setStatus("ready");
      } catch (reason) {
        if (cancelled) return;
        setStatus("error");
        setMessage(reason instanceof Error ? reason.message : "PDF 加载失败");
      }
    };

    void render();
    return () => {
      cancelled = true;
      renderTask?.cancel?.();
      textLayer?.cancel?.();
      // 翻页/缩放会重建 pdf 文档；不释放会让内存与 worker 会话持续累积。
      if (loadedDocument) {
        void loadedDocument.destroy();
        loadedDocument = null;
      }
    };
  }, [fileUrl, page, zoom, onPageChange, onPageCountChange, scale]);

  const visible = highlightsForPage(highlights, page);

  return (
    <div className="pdf-viewer" data-testid="pdf-viewer" data-file-url={fileUrl}>
      <div className="pdf-canvas">
        <div className="pdf-page" style={{ width: viewportSize.width || undefined, height: viewportSize.height || undefined }}>
          <canvas ref={canvasRef} className="pdf-page-canvas" />
          <div ref={textLayerRef} className="pdf-text-layer textLayer" />
          {status === "ready" &&
            visible.map((item) => {
              const rect = toViewportRect(item.bbox, viewportSize, scale);
              if (rect.width <= 0 || rect.height <= 0) return null;
              return (
                <div
                  key={item.id}
                  className={`bbox-overlay ${item.kind}`}
                  data-testid="bbox-overlay"
                  data-bbox-page={item.bbox.page}
                  style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}
                  title={item.label}
                >
                  <span>{item.label}</span>
                </div>
              );
            })}
          {status === "loading" && <div className="pdf-viewer-state">正在加载 PDF…</div>}
          {status === "error" && <div className="pdf-viewer-state error">{message ?? "PDF 加载失败"}</div>}
        </div>
      </div>
      <div className="pdf-status">
        <span>{visible.length > 0 ? `第 ${page} 页命中 ${visible.length} 处高亮` : `第 ${page} 页 · 选择要素或风险后定位原文`}</span>
        <span>左上原点 · PDF_POINTS 等比映射</span>
      </div>
    </div>
  );
}
