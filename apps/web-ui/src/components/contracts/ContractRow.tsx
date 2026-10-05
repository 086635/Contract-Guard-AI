import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Dialog from "@/components/ui/Dialog";
import Icon from "@/components/ui/Icon";
import { contractApi, previewApi } from "@/api/services";
import { useAuthStore } from "@/stores/authStore";

export type ContractItem = { id: string; name: string; type: string; date: string; status: "已完成" | "解析中" | "解析失败" | "待审核"; score: number; riskCount?: number; auditReady?: boolean; errorMessage?: string; progress?: number; stage?: string };

export default function ContractRow({ contract, view = "table" }: { contract: ContractItem; view?: "table" | "cards" }) {
  const navigate = useNavigate();
  const canEdit = useAuthStore((state) => state.hasRole("ADMIN") || state.hasRole("REVIEWER"));
  const [actions, setActions] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [fileUrl, setFileUrl] = useState<string | null>(null);
  const retry = async () => {
    setBusy(true); setMessage(null);
    try { const task = await contractApi.retryParse(contract.id); setMessage(`重新解析已提交：${task.stage}，原合同和文件保持。`); navigate(`/contracts/${contract.id}/audit`); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "重试失败，请稍后再试"); }
    finally { setBusy(false); }
  };
  const openActions = () => {
    setActions(true); setMessage(null); setFileUrl(null);
    void previewApi.get(contract.id).then((data) => { setFileUrl(data.previewUrl ?? null); if (!data.previewUrl) setMessage("文件预览尚未就绪，请进入审核工作台查看或重试转换。"); }).catch((reason) => setMessage(reason instanceof Error ? reason.message : "文件地址读取失败"));
  };
  const open = () => navigate(`/contracts/${contract.id}/audit`);
  const statusTone = contract.status === "已完成" ? "success" : contract.status === "解析中" ? "info" : contract.status === "解析失败" ? "high" : "medium";
  const riskSummary = <Badge>{contract.riskCount === undefined ? "风险待同步" : `风险 ${contract.riskCount}`}</Badge>;
  const more = <button className="row-more" aria-label="更多操作" title={`操作：${contract.name}`} onClick={(event) => { event.stopPropagation(); openActions(); }}><Icon name="more" size={18} /></button>;
  const row = view === "cards" ? <article className="contract-card" tabIndex={0} onClick={open} onKeyDown={(event) => { if (event.key === "Enter" && event.target === event.currentTarget) open(); }}><div className="contract-card-top"><div className="file-icon"><Icon name="file-text" size={20} /></div><Badge tone={statusTone} dot>{contract.status}</Badge>{more}</div><div className="contract-primary"><b>{contract.name}</b><span>{contract.id}</span></div><div className="contract-card-meta"><span>{contract.type}</span><span>{contract.date}</span></div><div className="risk-counts">{riskSummary}</div><div className="contract-card-foot"><span>当前结果健康分</span><strong>{contract.auditReady === false ? "—" : contract.score}<small>/100</small></strong></div></article> : <div className="contract-row" tabIndex={0} onClick={open} onKeyDown={(event) => { if (event.key === "Enter" && event.target === event.currentTarget) open(); }}><div className="contract-file-cell"><div className="file-icon"><Icon name="file-text" size={20} /></div><div className="contract-primary"><b>{contract.name}</b><span>{contract.id} · {contract.date}</span>{contract.errorMessage && <small className="error-text">{contract.errorMessage}</small>}</div></div><div className="contract-type">{contract.type}</div><div className="risk-counts">{riskSummary}</div><div className="score"><strong>{contract.auditReady === false ? "—" : contract.score}</strong><span>/100</span></div><div className="contract-status"><Badge tone={statusTone} dot>{contract.status}</Badge>{contract.status === "解析中" && contract.progress !== undefined && <small>{Math.round(contract.progress * 100)}%</small>}</div>{more}</div>;
  return <>{row}{actions && <Dialog title="合同操作" onClose={() => setActions(false)}><h3>{contract.name}</h3><p>{contract.status} · {contract.stage ?? "查看当前数据"}</p>{contract.errorMessage && <p className="error-text">失败原因：{contract.errorMessage}</p>}<div className="dialog-action-list"><Link to={`/contracts/${contract.id}/audit`}>查看审核与原文 →</Link><Link to={`/contracts/${contract.id}/report`}>查看或生成报告 →</Link>{fileUrl && <a href={fileUrl} target="_blank" rel="noreferrer">{contract.name.toLowerCase().endsWith(".pdf") ? "打开 PDF 原件" : "打开 PDF 预览"} ↗</a>}<Button variant="secondary" disabled={busy || !canEdit || contract.status === "已完成" || contract.status === "解析中"} onClick={() => void retry()}>{busy ? "提交中…" : "重新解析原文件"}</Button></div><p className="muted">{!canEdit ? "查看者不能重新解析。" : contract.status === "已完成" ? "已定稿合同请上传新版本。" : contract.status === "解析中" ? "解析任务正在运行，无需重复提交。" : "重试使用已保存的文件，无需重复上传。"}</p>{message && <p role="status">{message}</p>}</Dialog>}</>;
}
