import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import ElementsPanel from "@/components/audit/ElementsPanel";
import PdfStage from "@/components/audit/PdfStage";
import RiskPanel from "@/components/audit/RiskPanel";
import FeedbackDialog from "@/components/audit/FeedbackDialog";
import AgentChat from "@/components/chat/AgentChat";
import { contractApi, reportApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];
type Risk = Schemas["EnhancedRiskItem"];
type Element = Schemas["ContractElement"];
type RiskFilter = "ALL" | Schemas["RiskLevel"] | "CONFLICT" | "SEAL";
const DEFAULT_CONTRACT_ID = "00000000-0000-0000-0000-000000000001";

export default function Audit() {
  const { contractId } = useParams();
  const navigate = useNavigate();
  const selectedId = contractId ?? DEFAULT_CONTRACT_ID;
  const [contract, setContract] = useState<Schemas["Contract"] | null>(null);
  const [elements, setElements] = useState<Element[]>([]);
  const [risks, setRisks] = useState<Risk[]>([]);
  const [conflicts, setConflicts] = useState<Schemas["CrossClauseConflict"][]>([]);
  const [seals, setSeals] = useState<Schemas["SealSignatureDetection"][]>([]);
  const [parseStatus, setParseStatus] = useState<Schemas["TaskProgress"] | null>(null);
  const [selectedRiskId, setSelectedRiskId] = useState<string>();
  const [selectedElement, setSelectedElement] = useState<Element | null>(null);
  const [filter, setFilter] = useState<RiskFilter>("ALL");
  const [accepted, setAccepted] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [rewriteRisk, setRewriteRisk] = useState<Risk | null>(null);
  const [feedbackRisk, setFeedbackRisk] = useState<Risk | null>(null);

  const loadAudit = () => {
    setLoading(true); setError(null);
    return Promise.all([contractApi.detail(selectedId), contractApi.elements(selectedId), contractApi.risks(selectedId), contractApi.conflicts(selectedId), contractApi.seals(selectedId), contractApi.parseStatus(selectedId)])
      .then(([detail, extracted, riskItems, conflictItems, sealItems, status]) => { setContract(detail); setElements(extracted); setRisks(riskItems); setConflicts(conflictItems); setSeals(sealItems); setParseStatus(status); setSelectedRiskId((current) => current ?? riskItems[0]?.id); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "审核数据加载失败，请重试。"))
      .finally(() => setLoading(false));
  };
  useEffect(() => { void loadAudit(); }, [selectedId]);

  const selectedRisk = risks.find((risk) => risk.id === selectedRiskId);
  const highlight = useMemo(() => selectedElement?.bbox ? { label: selectedElement.label, kind: "element" as const, bbox: selectedElement.bbox } : selectedRisk ? { label: selectedRisk.title, kind: "risk" as const, bbox: selectedRisk.bbox } : null, [selectedElement, selectedRisk]);
  const commit = async () => {
    const items = risks.filter((risk) => accepted[risk.id]).map((risk) => ({ id: risk.id, accept: true }));
    if (!items.length || actionLoading) return;
    setActionLoading(true); setActionMessage(null);
    try { const result = await contractApi.commit(selectedId, { version: contract?.version, items }); setActionMessage(`已提交 ${result.successIds.length} 项批注，当前审核草稿已保存。`); setAccepted({}); }
    catch (reason) { setActionMessage(reason instanceof Error ? reason.message : "提交失败，请稍后重试。"); }
    finally { setActionLoading(false); }
  };
  const generateReport = async () => {
    if (actionLoading) return;
    setActionLoading(true); setActionMessage(null);
    try { await reportApi.generate(selectedId); navigate(`/contracts/${selectedId}/report`); }
    catch (reason) { setActionMessage(reason instanceof Error ? reason.message : "报告任务创建失败，请稍后重试。"); setActionLoading(false); }
  };
  const chooseRisk = (risk: Risk) => { setSelectedElement(null); setSelectedRiskId(risk.id); };
  const chooseElement = (element: Element) => { setSelectedElement(element); setSelectedRiskId(undefined); };
  const applyFilter = (next: RiskFilter) => { setFilter(next); setSelectedElement(null); const nextRisk = ["HIGH","MEDIUM","LOW"].includes(next) ? risks.find((risk) => risk.level === next) : risks[0]; if (nextRisk) setSelectedRiskId(nextRisk.id); };
  const highCount = risks.filter((item) => item.level === "HIGH").length;
  const mediumCount = risks.filter((item) => item.level === "MEDIUM").length;
  const lowCount = risks.filter((item) => item.level === "LOW").length;

  return <div className="audit-page"><div className="audit-topbar"><div><div className="eyebrow">CONTRACT REVIEW · {contract?.id ?? selectedId}</div><h1>{contract?.fileName ?? (loading ? "正在加载合同…" : "采购合同")} <span className="inline-status"><i />{contract?.status === "FINALIZED" ? "已定稿" : "审核中"}</span></h1></div><div className="audit-top-actions"><span className="save-state"><i />{loading ? "正在同步" : parseStatus?.status === "DONE" ? `解析完成 · ${seals.length} 处签署已校验` : "自动保存已开启"}</span><Button variant="secondary" size="sm" icon={<Icon name="refresh" size={16} />} onClick={() => void loadAudit()} disabled={loading}>刷新</Button></div></div>{error ? <div className="audit-data-error"><Icon name="activity" size={17} /><span>{error}</span><Button size="sm" variant="secondary" onClick={() => void loadAudit()}>重新加载</Button></div> : loading ? <div className="audit-loading"><div className="state-spinner" /><span>正在装载合同、风险和坐标图层…</span></div> : <div className="audit-workspace"><ElementsPanel extractedElements={elements} selectedElementKey={selectedElement?.key} onSelectElement={chooseElement} riskCounts={{ high: highCount, medium: mediumCount, low: lowCount, conflicts: conflicts.length, seals: seals.length }} riskFilter={filter} onRiskFilter={applyFilter} healthScore={contract?.healthScore.overall} /><PdfStage fileName={contract?.fileName ?? "合同文件"} highlight={highlight} onFeedback={() => selectedRisk && setFeedbackRisk(selectedRisk)} /><RiskPanel healthScore={contract?.healthScore.overall} risks={risks} conflicts={conflicts} seals={seals} filter={filter} selectedRiskId={selectedRiskId} accepted={accepted} onFilter={applyFilter} onSelectRisk={chooseRisk} onToggleAccepted={(riskId) => setAccepted((current) => ({ ...current, [riskId]: !current[riskId] }))} onCommit={() => void commit()} onGenerateReport={() => void generateReport()} onRewrite={setRewriteRisk} onFeedback={setFeedbackRisk} actionLoading={actionLoading} /></div>}{actionMessage && <div className={`audit-toast ${actionMessage.includes("失败") ? "error" : ""}`} onClick={() => setActionMessage(null)}>{actionMessage}</div>}{rewriteRisk && <AgentChat contractId={selectedId} clauseId={rewriteRisk.clauseId} originalText={rewriteRisk.originalText} onClose={() => setRewriteRisk(null)} onApplied={() => { setAccepted((current) => ({ ...current, [rewriteRisk.id]: true })); setActionMessage("AI 改写建议已加入提交清单。"); }} />}{feedbackRisk && <FeedbackDialog contractId={selectedId} targetId={feedbackRisk.id} targetLabel={feedbackRisk.title} onClose={() => setFeedbackRisk(null)} onSubmitted={setActionMessage} />}</div>;
}
