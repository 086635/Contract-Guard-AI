/** 风险卡片：展示 EnhancedRiskItem（含 CoT 推理链与 RAG 溯源，§8.4）。 */
export default function RiskCard({ title, level, suggestion }: {
  title: string;
  level: string;
  suggestion: string;
}) {
  return (
    <div className="risk-card" style={{ borderLeft: "4px solid #e54" }}>
      <strong>{title}</strong>
      <span className="risk-level">{level}</span>
      <p>{suggestion}</p>
    </div>
  );
}
