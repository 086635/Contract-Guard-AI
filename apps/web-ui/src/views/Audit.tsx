import { useParams } from "react-router-dom";

/** 审核主界面：左栏 PDF 高亮 + 右栏风险/矛盾/印章 + AI 改写对话框。 */
export default function Audit() {
  const { contractId } = useParams();
  return (
    <section>
      <h2>审核{contractId ? ` · ${contractId}` : ""}</h2>
      <p>
        双栏对照 / 风险卡片 / Diff 对照器（components/audit）+ PDF.js BBox 高亮
        （components/pdf-viewer）+ AI 多轮改写对话框（components/chat）。
      </p>
    </section>
  );
}
