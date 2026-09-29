import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import { contractApi, reportApi, taskApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const statusOf = (reason: unknown): number | undefined => {
  if (typeof reason !== "object" || reason === null || !("response" in reason)) return undefined;
  const response = (reason as { response?: { status?: number } }).response;
  return response?.status;
};

const isNotFound = (reason: unknown): boolean => statusOf(reason) === 404;

export default function Report() {
  const { contractId } = useParams();
  const selectedId = contractId ?? "";
  const [contract, setContract] = useState<Schemas["Contract"] | null>(null);
  const [report, setReport] = useState<Schemas["ReportData"] | null>(null);
  const [task, setTask] = useState<Schemas["TaskProgress"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeSection, setActiveSection] = useState("overview");
  const sectionRefs = {
    overview: useRef<HTMLDivElement>(null),
    risks: useRef<HTMLDivElement>(null),
    missing: useRef<HTMLDivElement>(null),
    seals: useRef<HTMLDivElement>(null),
  };
  const reportSubscriptionRef = useRef<(() => void) | null>(null);
  const activeRef = useRef(true);

  const clearReportSubscription = () => {
    reportSubscriptionRef.current?.();
    reportSubscriptionRef.current = null;
  };

  const load = async () => {
    if (!selectedId || !UUID_RE.test(selectedId)) {
      setContract(null);
      setReport(null);
      setTask(null);
      setError("合同 ID 无效，请从合同中心选择合同。");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    const [detailResult, reportResult] = await Promise.allSettled([
      contractApi.detail(selectedId),
      reportApi.get(selectedId),
    ]);
    if (!activeRef.current) return;
    if (detailResult.status === "fulfilled") {
      setContract(detailResult.value);
    } else {
      setError("合同详情加载失败，请稍后重试。");
    }
    if (reportResult.status === "fulfilled") {
      setReport(reportResult.value);
      setMessage(null);
    } else if (isNotFound(reportResult.reason)) {
      setReport(null);
    } else {
      setError(reportResult.reason instanceof Error ? reportResult.reason.message : "报告数据加载失败，请稍后重试。");
    }
    setLoading(false);
  };

  useEffect(() => {
    activeRef.current = true;
    clearReportSubscription();
    setReport(null);
    setTask(null);
    setError(null);
    setMessage(null);
    void load();
    return () => {
      activeRef.current = false;
      clearReportSubscription();
    };
  }, [selectedId]);

  const goTo = (key: keyof typeof sectionRefs) => {
    setActiveSection(key);
    sectionRefs[key].current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const regenerate = async () => {
    if (!selectedId || task?.status === "RUNNING") return;
    clearReportSubscription();
    setError(null);
    setMessage("报告生成任务已提交，等待完成…");
    try {
      const next = await reportApi.generate(selectedId);
      if (!activeRef.current) return;
      setTask(next);
      if (next.status === "FAILED") {
        setError("报告生成失败，请重试。");
        return;
      }
      if (next.status === "DONE") {
        setMessage("报告已生成，页面数据已刷新。");
        void load();
        return;
      }
      reportSubscriptionRef.current = taskApi.subscribe(next.taskId, (progress) => {
        if (!activeRef.current) return;
        setTask(progress);
        if (progress.status === "DONE") {
          setMessage("报告已生成，页面数据已刷新。");
          clearReportSubscription();
          void load();
        } else if (progress.status === "FAILED") {
          setError("报告生成失败，请重试。");
          clearReportSubscription();
        }
      });
    } catch (reason) {
      if (!activeRef.current) return;
      setError(reason instanceof Error ? reason.message : "报告生成失败，请稍后重试。");
    }
  };

  const openDownload = () => {
    if (!report?.downloadUrl) {
      setError("报告下载地址尚未生成，请先生成报告。");
      return;
    }
    try {
      const parsed = new URL(report.downloadUrl, window.location.origin);
      window.open(parsed.toString(), "_blank", "noopener,noreferrer");
    } catch {
      setError("报告下载地址无效，请重新生成报告。");
    }
  };

  const generating = task?.status === "RUNNING" || report?.status === "RUNNING";
  const failed = task?.status === "FAILED" || report?.status === "FAILED";
  const canDownload = Boolean(report?.downloadUrl) && !generating;
  const reportStateLabel = generating ? "报告生成中" : failed ? "生成失败" : report?.status === "DONE" ? "审核已完成" : "尚未生成";

  return (
    <div className="report-page">
      {error && <div className="audit-data-error"><Icon name="activity" size={17} /><span>{error}</span><Button size="sm" variant="secondary" onClick={() => void load()}>重新加载</Button></div>}
      {message && <div className="settings-message">{message}<button onClick={() => setMessage(null)} aria-label="关闭提示"><Icon name="x" size={14} /></button></div>}
      <SectionHeader
        eyebrow={`REPORT CENTER · ${report?.status ?? "LOADING"}`}
        title="审核报告与导出中心"
        description={`${contract?.fileName ?? "正在读取合同"} · ${report?.generatedAt ? new Date(report.generatedAt).toLocaleString("zh-CN") : "等待报告数据"}`}
        actions={
          <>
            <Button variant="secondary" icon={<Icon name="refresh" size={16} />} onClick={() => void regenerate()} disabled={generating}>{generating ? `生成中 ${Math.round((task?.percent ?? 0) * 100)}%` : "重新生成"}</Button>
            <Button icon={<Icon name="download" size={16} />} onClick={openDownload} disabled={!canDownload}>下载报告</Button>
          </>
        }
      />
      {generating && <div className="report-progress"><span><i style={{ width: `${Math.max(4, (task?.percent ?? 0) * 100)}%` }} /></span><b>{task?.stage ?? "正在生成报告"}</b><em>{Math.round((task?.percent ?? 0) * 100)}%</em></div>}
      {loading ? (
        <div className="audit-loading"><div className="state-spinner" /><span>正在读取报告与合同数据…</span></div>
      ) : !report ? (
        <div className="data-state">
          <div>
            <Icon name="book" size={22} />
            <p>{failed ? "报告生成失败，请重新生成。" : "该合同尚未生成报告。"}</p>
            <Button variant="secondary" size="sm" onClick={() => void regenerate()}>生成报告</Button>
          </div>
        </div>
      ) : (
        <div className="report-layout">
          <aside className="report-sidebar panel">
            <div className="report-side-title">报告目录</div>
            <button className={activeSection === "overview" ? "active" : ""} onClick={() => goTo("overview")}>风险概览 <span>01</span></button>
            <button className={activeSection === "risks" ? "active" : ""} onClick={() => goTo("risks")}>重点风险 <span>02</span></button>
            <button className={activeSection === "missing" ? "active" : ""} onClick={() => goTo("missing")}>缺失条款 <span>03</span></button>
            <button className={activeSection === "seals" ? "active" : ""} onClick={() => goTo("seals")}>签署校验 <span>04</span></button>
            <div className="report-version"><small>报告版本</small><b>v{contract?.version ?? 1}.0 · 当前版本</b><span>{report.generatedAt ? new Date(report.generatedAt).toLocaleString("zh-CN") : "读取中…"}</span><small>仅当前可用版本</small></div>
          </aside>
          <article className="report-paper panel">
            <div className="report-cover">
              <div className="report-logo"><span><Icon name="shield" size={20} /></span>CONTRACTGUARD <small>AI LEGAL WORKSPACE</small></div>
              <Badge tone={report.status === "DONE" ? "success" : failed ? "high" : "info"} dot>{reportStateLabel}</Badge>
              <h2>合同智能审核报告</h2><p>{contract?.fileName ?? "合同文件"}</p>
              <div className="cover-meta"><span>合同类型<b>{contract?.contractType ?? "—"}</b></span><span>审核版本<b>v{contract?.version ?? 1}.0</b></span><span>报告日期<b>{report.generatedAt ? new Date(report.generatedAt).toLocaleDateString("zh-CN") : "—"}</b></span></div>
            </div>
            <div className="report-section" ref={sectionRefs.overview}><div className="report-section-heading"><span>01</span><div><h3>风险概览</h3><p>基于合同文本、法条知识库及规则引擎综合评估</p></div></div><div className="report-score-grid"><div className="big-score"><div className="score-ring large">{contract?.healthScore.overall ?? "—"}<small>/100</small></div><div><b>综合健康分</b><span>优先处理高风险项，再完成条款一致性复核</span></div></div>{(["HIGH","MEDIUM","LOW"] as const).map((level) => <div className={`report-stat ${level.toLowerCase()}`} key={level}><b>{contract?.risks.filter((item) => item.level === level).length ?? 0}</b><span>{level === "HIGH" ? "高风险" : level === "MEDIUM" ? "中风险" : "低风险"}</span></div>)}</div></div>
            <div className="report-section" ref={sectionRefs.risks}><div className="report-section-heading"><span>02</span><div><h3>重点风险清单</h3><p>共 {contract?.risks.length ?? 0} 个风险条款需要人工确认</p></div></div>{contract?.risks.length ? contract.risks.map((risk) => <div className="report-risk-line" key={risk.id}><Badge tone={risk.level === "HIGH" ? "high" : risk.level === "MEDIUM" ? "medium" : "low"} dot>{risk.level === "HIGH" ? "高风险" : risk.level === "MEDIUM" ? "中风险" : "低风险"}</Badge><div><b>{risk.title}</b><p>{risk.suggestion}</p><small>{risk.legalBasis}</small></div><span>待确认</span></div>) : <div className="compact-empty">未发现需要列示的风险</div>}</div>
            <div className="report-section" ref={sectionRefs.missing}><div className="report-section-heading"><span>03</span><div><h3>缺失条款提醒</h3><p>与标准合同模板对比后的缺口</p></div></div>{contract?.missingClauses.length ? contract.missingClauses.map((item) => <div className="report-detail-card" key={item.id}><b>{item.title}</b><p>{item.reason}</p><span>{item.suggestion}</span></div>) : <div className="compact-empty">未发现关键条款缺失</div>}</div>
            <div className="report-section" ref={sectionRefs.seals}><div className="report-section-heading"><span>04</span><div><h3>签署合规校验</h3><p>印章、签名与合同主体一致性检查</p></div></div>{contract?.seals.map((seal) => <div className="report-detail-card" key={seal.id}><div><Badge tone={seal.status === "VALID" ? "success" : "high"} dot>{seal.status}</Badge><b>{seal.extractedText ?? "未识别签章"}</b></div><p>{seal.complianceNote}</p><span>第 {seal.page} 页 · 置信度 {Math.round(seal.confidence * 100)}%</span></div>)}</div>
          </article>
        </div>
      )}
    </div>
  );
}