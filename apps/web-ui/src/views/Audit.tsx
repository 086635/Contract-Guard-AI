import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import ElementsPanel from "@/components/audit/ElementsPanel";
import PdfStage from "@/components/audit/PdfStage";
import type { Highlight } from "@/components/pdf-viewer/PdfViewer";
import RiskPanel from "@/components/audit/RiskPanel";
import FeedbackDialog from "@/components/audit/FeedbackDialog";
import AgentChat from "@/components/chat/AgentChat";
import { contractApi, previewApi, reportApi, taskApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];
type Risk = Schemas["EnhancedRiskItem"];
type Element = Schemas["ContractElement"];
type RiskFilter = "ALL" | Schemas["RiskLevel"] | "CONFLICT" | "SEAL";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export default function Audit() {
  const { contractId } = useParams();
  const navigate = useNavigate();
  const selectedId = contractId ?? "";
  const [contract, setContract] = useState<Schemas["Contract"] | null>(null);
  const [elements, setElements] = useState<Element[]>([]);
  const [risks, setRisks] = useState<Risk[]>([]);
  const [conflicts, setConflicts] = useState<Schemas["CrossClauseConflict"][]>([]);
  const [seals, setSeals] = useState<Schemas["SealSignatureDetection"][]>([]);
  const [parseStatus, setParseStatus] = useState<Schemas["TaskProgress"] | null>(null);
  const [preview, setPreview] = useState<Schemas["PreviewData"] | null>(null);
  const [previewRetrying, setPreviewRetrying] = useState(false);
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
  const reportUnsubscribeRef = useRef<(() => void) | null>(null);

  const loadAudit = async () => {
    setLoading(true);
    setError(null);
    if (!selectedId || !UUID_RE.test(selectedId)) {
      setContract(null);
      setElements([]);
      setRisks([]);
      setConflicts([]);
      setSeals([]);
      setParseStatus(null);
      setPreview(null);
      setSelectedRiskId(undefined);
      setSelectedElement(null);
      setError("合同 ID 无效，请从合同中心选择合同。");
      setLoading(false);
      return;
    }

    const results = await Promise.allSettled([
      contractApi.detail(selectedId),
      contractApi.elements(selectedId),
      contractApi.risks(selectedId),
      contractApi.conflicts(selectedId),
      contractApi.seals(selectedId),
      contractApi.parseStatus(selectedId),
      previewApi.get(selectedId),
    ]);
    const [detail, extracted, riskItems, conflictItems, sealItems, status, previewData] = results;
    const failures: unknown[] = [];
    if (detail.status === "fulfilled") setContract(detail.value);
    if (extracted.status === "fulfilled") setElements(extracted.value);
    if (riskItems.status === "fulfilled") {
      setRisks(riskItems.value);
      setSelectedRiskId(riskItems.value[0]?.id);
    }
    if (conflictItems.status === "fulfilled") setConflicts(conflictItems.value);
    if (sealItems.status === "fulfilled") setSeals(sealItems.value);
    if (status.status === "fulfilled") setParseStatus(status.value);
    if (previewData.status === "fulfilled") {
      let nextPreview = previewData.value;
      // DOCX 首次为 PENDING：GET 只建元数据，必须调用幂等 retry 才会派发转换任务。
      if (nextPreview.status === "PENDING") {
        try {
          await previewApi.retry(selectedId);
          nextPreview = await previewApi.get(selectedId);
        } catch (reason) {
          failures.push(reason);
        }
      }
      setPreview(nextPreview);
    }

    results.forEach((result) => {
      if (result.status === "rejected") failures.push(result.reason);
    });
    if (failures.length > 0) {
      const first = failures[0];
      setError(`${failures.length} 个审核数据接口加载失败：${first instanceof Error ? first.message : "请重试"}`);
    }
    setLoading(false);
  };

  const clearReportSubscription = () => {
    reportUnsubscribeRef.current?.();
    reportUnsubscribeRef.current = null;
  };

  useEffect(() => () => clearReportSubscription(), []);

  useEffect(() => {
    setContract(null);
    setElements([]);
    setRisks([]);
    setConflicts([]);
    setSeals([]);
    setParseStatus(null);
    setPreview(null);
    setSelectedRiskId(undefined);
    setSelectedElement(null);
    void loadAudit();
  }, [selectedId]);

  // 预览转换中：每 2 秒刷新一次元数据（与任务轮询同节奏），完成即自动切换到查看器。
  useEffect(() => {
    if (!preview || (preview.status !== "PENDING" && preview.status !== "RUNNING")) return;
    const timer = window.setTimeout(() => {
      void previewApi
        .get(selectedId)
        .then(setPreview)
        .catch(() => undefined);
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [preview, selectedId]);

  const retryPreview = async () => {
    if (!UUID_RE.test(selectedId) || previewRetrying) return;
    setPreviewRetrying(true);
    try {
      await previewApi.retry(selectedId);
      setPreview(await previewApi.get(selectedId));
    } catch (reason) {
      setActionMessage(reason instanceof Error ? reason.message : "预览转换重试失败，请稍后重试。");
    } finally {
      setPreviewRetrying(false);
    }
  };

  const selectedRisk = risks.find((risk) => risk.id === selectedRiskId);
  const highlights = useMemo<Highlight[]>(() => {
    if (selectedElement?.bbox) return [{ id: selectedElement.key, label: selectedElement.label, kind: "element", bbox: selectedElement.bbox }];
    if (selectedRisk?.bbox) return [{ id: selectedRisk.id, label: selectedRisk.title, kind: "risk", bbox: selectedRisk.bbox }];
    return [];
  }, [selectedElement, selectedRisk]);
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
    clearReportSubscription();
    setActionLoading(true);
    setActionMessage("报告生成任务已提交，正在等待渲染完成…");
    try {
      const task = await reportApi.generate(selectedId);
      if (task.status === "DONE") {
        setActionLoading(false);
        navigate(`/contracts/${selectedId}/report`);
        return;
      }
      if (task.status === "FAILED") {
        setActionLoading(false);
        setActionMessage("报告生成失败，请重试。");
        return;
      }
      reportUnsubscribeRef.current = taskApi.subscribe(task.taskId, (progress) => {
        if (progress.status === "DONE") {
          clearReportSubscription();
          setActionLoading(false);
          setActionMessage("报告已生成，正在打开报告中心…");
          navigate(`/contracts/${selectedId}/report`);
        } else if (progress.status === "FAILED") {
          clearReportSubscription();
          setActionLoading(false);
          setActionMessage("报告生成失败，请重试。");
        } else {
          setActionMessage(`报告生成中：${progress.stage} ${Math.round(progress.percent * 100)}%`);
        }
      }, (reason) => {
        // C65：订阅中断 → 底层转 2 秒轮询；权限类错误给出安全提示（不含令牌）。
        setActionMessage(reason instanceof Error && reason.message ? reason.message : "报告进度订阅中断，已切换为轮询。");
      });
    } catch (reason) {
      clearReportSubscription();
      setActionLoading(false);
      setActionMessage(reason instanceof Error ? reason.message : "报告任务创建失败，请稍后重试。");
    }
  };
  const chooseRisk = (risk: Risk) => { setSelectedElement(null); setSelectedRiskId(risk.id); };
  const chooseElement = (element: Element) => { setSelectedElement(element); setSelectedRiskId(undefined); };
  const applyFilter = (next: RiskFilter) => { setFilter(next); setSelectedElement(null); const nextRisk = ["HIGH","MEDIUM","LOW"].includes(next) ? risks.find((risk) => risk.level === next) : risks[0]; if (nextRisk) setSelectedRiskId(nextRisk.id); };
  const highCount = risks.filter((item) => item.level === "HIGH").length;
  const mediumCount = risks.filter((item) => item.level === "MEDIUM").length;
  const lowCount = risks.filter((item) => item.level === "LOW").length;
  const hasAuditData = Boolean(contract || elements.length || risks.length || conflicts.length || seals.length || parseStatus);

  return <div className="audit-page"><div className="audit-topbar"><div><div className="eyebrow">CONTRACT REVIEW · {contract?.id ?? selectedId}</div><h1>{contract?.fileName ?? (loading ? "正在加载合同…" : "合同详情")} <span className="inline-status"><i />{contract?.status === "FINALIZED" ? "已定稿" : "审核中"}</span></h1></div><div className="audit-top-actions"><span className="save-state"><i />{loading ? "正在同步" : parseStatus?.status === "DONE" ? `解析完成 · ${seals.length} 处签署已校验` : "自动保存已开启"}</span><Button variant="secondary" size="sm" icon={<Icon name="refresh" size={16} />} onClick={() => void loadAudit()} disabled={loading}>刷新</Button></div></div>{error && <div className="audit-data-error"><Icon name="activity" size={17} /><span>{error}</span><Button size="sm" variant="secondary" onClick={() => void loadAudit()}>重新加载</Button></div>}{loading ? <div className="audit-loading"><div className="state-spinner" /><span>正在装载合同、风险和坐标图层…</span></div> : !hasAuditData ? <div className="data-state error"><div><p>{error ?? "未找到该合同的审核数据。"}</p><Button size="sm" variant="secondary" onClick={() => void loadAudit()}>重新加载</Button></div></div> : <div className="audit-workspace"><ElementsPanel extractedElements={elements} selectedElementKey={selectedElement?.key} onSelectElement={chooseElement} riskCounts={{ high: highCount, medium: mediumCount, low: lowCount, conflicts: conflicts.length, seals: seals.length }} riskFilter={filter} onRiskFilter={applyFilter} healthScore={contract?.healthScore.overall} /><PdfStage fileName={contract?.fileName ?? "合同文件"} preview={preview} highlights={highlights} previewRetrying={previewRetrying} onRetry={() => void retryPreview()} onFeedback={() => selectedRisk && setFeedbackRisk(selectedRisk)} /><RiskPanel healthScore={contract?.healthScore.overall} risks={risks} conflicts={conflicts} seals={seals} filter={filter} selectedRiskId={selectedRiskId} accepted={accepted} onFilter={applyFilter} onSelectRisk={chooseRisk} onToggleAccepted={(riskId) => setAccepted((current) => ({ ...current, [riskId]: !current[riskId] }))} onCommit={() => void commit()} onGenerateReport={() => void generateReport()} onRewrite={setRewriteRisk} onFeedback={setFeedbackRisk} actionLoading={actionLoading} /></div>}{actionMessage && <div className={`audit-toast ${actionMessage.includes("失败") ? "error" : ""}`} onClick={() => setActionMessage(null)}>{actionMessage}</div>}{rewriteRisk && <AgentChat contractId={selectedId} clauseId={rewriteRisk.clauseId} originalText={rewriteRisk.originalText} onClose={() => setRewriteRisk(null)} onApplied={() => { setAccepted((current) => ({ ...current, [rewriteRisk.id]: true })); setActionMessage("AI 改写建议已加入提交清单。"); }} />}{feedbackRisk && <FeedbackDialog contractId={selectedId} targetId={feedbackRisk.id} targetLabel={feedbackRisk.title} onClose={() => setFeedbackRisk(null)} onSubmitted={setActionMessage} />}</div>;
}
