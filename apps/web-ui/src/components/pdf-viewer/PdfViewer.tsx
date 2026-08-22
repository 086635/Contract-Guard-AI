/** PDF.js + BBox Canvas 高亮 Overlay（按归一化 BBox 命中高亮，§1.3 / doc-processor.aligner）。 */
export default function PdfViewer({ fileUrl }: { fileUrl: string }) {
  return (
    <div className="pdf-viewer">
      <p>PDF 渲染占位：{fileUrl}</p>
      <p>Canvas Overlay 按 BBox（PDF_POINTS，72dpi，左上原点）高亮风险 / 印章区域。</p>
    </div>
  );
}
