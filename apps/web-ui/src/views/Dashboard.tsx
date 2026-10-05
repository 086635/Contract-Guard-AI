import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import ContractRow, { type ContractItem } from "@/components/contracts/ContractRow";
import UploadPanel from "@/components/contracts/UploadPanel";
import { contractApi, taskApi } from "@/api/services";
import type { components } from "@/api/generated/types";

const statusLabel: Record<string, ContractItem["status"]> = { UPLOADING: "解析中", PARSING: "解析中", PENDING_REVIEW: "待审核", IN_REVIEW: "待审核", FINALIZED: "已完成" };
const typeLabel: Record<string, string> = { PURCHASE: "采购合同", SALES: "销售合同", NDA: "保密协议", OUTSOURCING: "服务合同", LABOR: "劳动合同", OTHER: "其他合同" };
type ContractSummary = components["schemas"]["ContractSummary"];
const toContractItem = (item: ContractSummary): ContractItem => ({ id: item.id, name: item.fileName, type: typeLabel[item.contractType] ?? item.contractType, date: new Date(item.createdAt).toISOString().slice(0, 10), status: statusLabel[item.status] ?? "待审核", score: item.healthScore.overall, riskCount: item.riskCount, auditReady: item.status === "IN_REVIEW" || item.status === "FINALIZED" });

async function listAllContracts(): Promise<ContractSummary[]> {
  const items: ContractSummary[] = [];
  for (let page = 1; ; page += 1) {
    const data = await contractApi.list({ page, pageSize: 50 });
    items.push(...data.items);
    if (!data.hasMore || data.items.length === 0 || items.length >= data.total) break;
  }
  return items;
}

export async function loadContractItems(): Promise<ContractItem[]> {
  let summaries = await listAllContracts();
  const progress = await Promise.all(summaries.filter((item) => item.status === "PARSING").map(async (item) => {
    const recorded = await contractApi.parseStatus(item.id);
    const task = recorded.status === "RUNNING" ? await taskApi.get(recorded.taskId) : recorded;
    return { id: item.id, task };
  }));
  // 既有 parse-status 负责网关结果收口；完成后重新读取摘要，避免首页一直显示解析中。
  if (progress.some((item) => item.task.status === "DONE")) summaries = await listAllContracts();
  const failed = new Set(progress.filter((item) => item.task.status === "FAILED" || item.task.result?.status === "FAILED").map((item) => item.id));
  const done = new Set(progress.filter((item) => item.task.status === "DONE").map((item) => item.id));
  return summaries.map((item) => {
    const contract = toContractItem(item);
    const task = progress.find((record) => record.id === item.id)?.task;
    contract.errorMessage = typeof task?.result?.errorMessage === "string" ? task.result.errorMessage : undefined;
    contract.progress = task?.percent;
    contract.stage = task?.stage;
    if (failed.has(item.id)) contract.status = "解析失败";
    else if (done.has(item.id) && item.status === "PARSING") contract.status = "待审核";
    return contract;
  });
}

export function averageHealthScore(contracts: ContractItem[]): number | null {
  return contracts.length ? Math.round(contracts.reduce((sum, item) => sum + item.score, 0) / contracts.length) : null;
}

export function scheduleContractRefresh(contracts: ContractItem[], refresh: () => void): () => void {
  if (!contracts.some((item) => item.status === "解析中")) return () => {};
  const timer = window.setTimeout(refresh, 5000);
  return () => window.clearTimeout(timer);
}

export default function Dashboard() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [statusFilter, setStatusFilter] = useState("全部合同");
  const [typeFilter, setTypeFilter] = useState("全部类型");
  const [view, setView] = useState<"table" | "cards">("table");
  const [contracts, setContracts] = useState<ContractItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const loadSequence = useRef(0);
  const loadInFlight = useRef(false);

  useEffect(() => {
    setQuery(searchParams.get("q") ?? "");
    if (searchParams.get("upload") === "1") { setUploadOpen(true); const next = new URLSearchParams(searchParams); next.delete("upload"); setSearchParams(next, { replace: true }); }
  }, [searchParams, setSearchParams]);

  const loadContracts = useCallback((background = false) => {
    if (background && loadInFlight.current) return Promise.resolve();
    loadInFlight.current = true;
    const sequence = ++loadSequence.current;
    if (!background) setLoading(true);
    setLoadError(null);
    return loadContractItems()
      .then((items) => { if (sequence === loadSequence.current) setContracts(items); })
      .catch((reason: unknown) => { if (sequence === loadSequence.current) setLoadError(reason instanceof Error ? reason.message : "合同数据同步失败，请重试。"); })
      .finally(() => { if (sequence === loadSequence.current) { loadInFlight.current = false; setLoading(false); } });
  }, []);
  useEffect(() => { void loadContracts(); return () => { loadSequence.current += 1; }; }, [loadContracts]);
  useEffect(() => scheduleContractRefresh(contracts, () => { void loadContracts(true); }), [contracts, loadContracts, loadError]);

  const visibleContracts = useMemo(() => contracts.filter((item) => {
    const statusMatches = statusFilter === "全部合同" || item.status === statusFilter;
    const typeMatches = typeFilter === "全部类型" || item.type === typeFilter;
    const queryMatches = !query.trim() || `${item.name} ${item.id} ${item.type}`.toLowerCase().includes(query.trim().toLowerCase());
    return statusMatches && typeMatches && queryMatches;
  }), [contracts, query, statusFilter, typeFilter]);
  const averageScore = averageHealthScore(contracts.filter((item) => item.auditReady));
  const pendingCount = contracts.filter((item) => item.status === "待审核").length;
  const parsingCount = contracts.filter((item) => item.status === "解析中").length;

  return <div className="dashboard-page">
    <SectionHeader eyebrow="CONTRACT OPERATIONS" title="合同管理与导入中心" description="统一查看解析进度、风险概览与人工审核状态。" actions={<><Button variant="secondary" icon={<Icon name="refresh" size={16} />} onClick={() => void loadContracts()} disabled={loading}>刷新数据</Button><Button icon={<Icon name="plus" size={17} />} onClick={() => setUploadOpen(true)}>上传新合同</Button></>} />
    <div className="workflow-guide"><button onClick={() => setUploadOpen(true)}><b>01 上传合同</b><span>保留原文件，启动解析</span></button><Link to="/audit"><b>02 查看解析与审核</b><span>{pendingCount} 份待审核 · {contracts.filter((item) => item.status === "解析失败").length} 份失败可重试</span></Link><Link to="/report"><b>03 生成与导出报告</b><span>选择合同，查看真实审核数据</span></Link><Link to="/feedback"><b>04 人工反馈</b><span>校正要素或风险，保留复核记录</span></Link></div>
    <div className="metric-grid">
      <div className="metric-card metric-primary"><div className="metric-icon"><Icon name="file-text" size={19} /></div><span>合同总数</span><strong>{contracts.length}</strong><small>当前工作空间内全部合同</small><div className="metric-spark"><i /><i /><i /><i /><i /><i /><i /><i /></div></div>
      <div className="metric-card"><div className="metric-icon amber"><Icon name="activity" size={19} /></div><span>待人工审核</span><strong>{pendingCount}</strong><small><em className="amber-text">优先处理高风险</em></small><div className="metric-progress"><i style={{ width: `${contracts.length ? pendingCount / contracts.length * 100 : 0}%` }} /></div></div>
      <div className="metric-card"><div className="metric-icon green"><Icon name="shield" size={19} /></div><span>平均健康分</span><strong>{averageScore ?? "—"}</strong><small>仅计算已返回完整审核结果的合同</small><div className="metric-progress green-track"><i style={{ width: `${averageScore ?? 0}%` }} /></div></div>
      <div className="metric-card"><div className="metric-icon purple"><Icon name="clock" size={19} /></div><span>解析中任务</span><strong>{parsingCount}</strong><small>解析中每 5 秒同步状态</small><div className="metric-spark purple-spark"><i /><i /><i /><i /><i /><i /><i /><i /></div></div>
    </div>
    <section className="panel contracts-panel contract-center-panel">
      <div className="panel-heading"><div><h2>合同队列</h2><p>{loading ? "正在同步 API 数据…" : `显示 ${visibleContracts.length} / ${contracts.length} 份合同`}</p></div><div className="view-switch"><button className={view === "table" ? "active" : ""} onClick={() => setView("table")} aria-label="表格视图"><Icon name="menu" size={16} /></button><button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")} aria-label="卡片视图"><Icon name="grid" size={16} /></button></div></div>
      <div className="table-toolbar contract-toolbar"><div className="search-field"><Icon name="search" size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索合同名称、编号或类型" />{query && <button onClick={() => setQuery("")} aria-label="清空搜索"><Icon name="x" size={14} /></button>}</div><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} aria-label="合同类型">{["全部类型", ...Array.from(new Set(contracts.map((item) => item.type)))].map((item) => <option key={item}>{item}</option>)}</select><div className="filter-pills">{["全部合同", "待审核", "解析中", "解析失败", "已完成"].map((item) => <button key={item} className={statusFilter === item ? "active" : ""} onClick={() => setStatusFilter(item)}>{item}</button>)}</div></div>
      {view === "table" && <div className="contract-table-head"><span>合同文件</span><span>类型</span><span>风险概览</span><span>健康分</span><span>状态</span><span /></div>}
      <div className={view === "cards" ? "contract-card-grid" : "contract-list"}>{loading ? <div className="data-state"><div className="state-spinner" />正在从 API 网关同步合同…</div> : loadError ? <div className="data-state error"><div><p>{loadError}</p><Button variant="secondary" size="sm" onClick={() => void loadContracts()}>重新连接</Button></div></div> : visibleContracts.length === 0 ? <div className="data-state"><div><Icon name="search" size={22} /><p>没有匹配的合同</p><Button variant="secondary" size="sm" onClick={() => { setQuery(""); setStatusFilter("全部合同"); setTypeFilter("全部类型"); }}>清除筛选</Button></div></div> : visibleContracts.map((contract) => <ContractRow key={contract.id} contract={contract} view={view} />)}</div>
    </section>
    {uploadOpen && <UploadPanel onClose={() => setUploadOpen(false)} onCreated={() => void loadContracts()} />}
  </div>;
}
