import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import ContractRow, { type ContractItem } from "@/components/contracts/ContractRow";
import UploadPanel from "@/components/contracts/UploadPanel";
import { contractApi } from "@/api/services";

const statusLabel: Record<string, ContractItem["status"]> = { UPLOADING: "解析中", PARSING: "解析中", PENDING_REVIEW: "待审核", IN_REVIEW: "待审核", FINALIZED: "已完成" };
const typeLabel: Record<string, string> = { PURCHASE: "采购合同", SALES: "销售合同", NDA: "保密协议", OUTSOURCING: "服务合同", LABOR: "劳动合同", OTHER: "其他合同" };
const toContractItem = (item: import("@/api/generated/types").components["schemas"]["ContractSummary"]): ContractItem => ({ id: item.id, name: item.fileName, type: typeLabel[item.contractType] ?? item.contractType, date: new Date(item.createdAt).toISOString().slice(0, 10), status: statusLabel[item.status] ?? "待审核", score: item.healthScore.overall, high: item.riskCount ?? 0, medium: 0, low: 0 });

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

  useEffect(() => {
    setQuery(searchParams.get("q") ?? "");
    if (searchParams.get("upload") === "1") { setUploadOpen(true); setSearchParams({}, { replace: true }); }
  }, [searchParams, setSearchParams]);

  const loadContracts = () => {
    setLoading(true); setLoadError(null);
    return contractApi.list({ page: 1, pageSize: 50 })
      .then((data) => setContracts(data.items.map(toContractItem)))
      .catch(() => setLoadError("暂时无法连接 API 网关。请确认后端服务已启动，再重新连接。"))
      .finally(() => setLoading(false));
  };
  useEffect(() => { void loadContracts(); }, []);

  const visibleContracts = useMemo(() => contracts.filter((item) => {
    const statusMatches = statusFilter === "全部合同" || item.status === statusFilter;
    const typeMatches = typeFilter === "全部类型" || item.type === typeFilter;
    const queryMatches = !query.trim() || `${item.name} ${item.id} ${item.type}`.toLowerCase().includes(query.trim().toLowerCase());
    return statusMatches && typeMatches && queryMatches;
  }), [contracts, query, statusFilter, typeFilter]);
  const averageScore = contracts.length ? Math.round(contracts.reduce((sum, item) => sum + item.score, 0) / contracts.length) : 0;
  const pendingCount = contracts.filter((item) => item.status === "待审核").length;
  const parsingCount = contracts.filter((item) => item.status === "解析中").length;

  return <div className="dashboard-page">
    <SectionHeader eyebrow="CONTRACT OPERATIONS" title="合同管理与导入中心" description="统一查看解析进度、风险概览与人工审核状态。" actions={<><Button variant="secondary" icon={<Icon name="refresh" size={16} />} onClick={() => void loadContracts()} disabled={loading}>刷新数据</Button><Button icon={<Icon name="plus" size={17} />} onClick={() => setUploadOpen(true)}>上传新合同</Button></>} />
    <div className="metric-grid">
      <div className="metric-card metric-primary"><div className="metric-icon"><Icon name="file-text" size={19} /></div><span>合同总数</span><strong>{contracts.length}</strong><small>当前工作空间内全部合同</small><div className="metric-spark"><i /><i /><i /><i /><i /><i /><i /><i /></div></div>
      <div className="metric-card"><div className="metric-icon amber"><Icon name="activity" size={19} /></div><span>待人工审核</span><strong>{pendingCount}</strong><small><em className="amber-text">优先处理高风险</em></small><div className="metric-progress"><i style={{ width: `${contracts.length ? pendingCount / contracts.length * 100 : 0}%` }} /></div></div>
      <div className="metric-card"><div className="metric-icon green"><Icon name="shield" size={19} /></div><span>平均健康分</span><strong>{averageScore || "—"}</strong><small>依据当前列表实时计算</small><div className="metric-progress green-track"><i style={{ width: `${averageScore}%` }} /></div></div>
      <div className="metric-card"><div className="metric-icon purple"><Icon name="clock" size={19} /></div><span>解析中任务</span><strong>{parsingCount}</strong><small>通过 SSE 实时更新进度</small><div className="metric-spark purple-spark"><i /><i /><i /><i /><i /><i /><i /><i /></div></div>
    </div>
    <section className="panel contracts-panel contract-center-panel">
      <div className="panel-heading"><div><h2>合同队列</h2><p>{loading ? "正在同步 API 数据…" : `显示 ${visibleContracts.length} / ${contracts.length} 份合同`}</p></div><div className="view-switch"><button className={view === "table" ? "active" : ""} onClick={() => setView("table")} aria-label="表格视图"><Icon name="menu" size={16} /></button><button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")} aria-label="卡片视图"><Icon name="grid" size={16} /></button></div></div>
      <div className="table-toolbar contract-toolbar"><div className="search-field"><Icon name="search" size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索合同名称、编号或类型" />{query && <button onClick={() => setQuery("")} aria-label="清空搜索"><Icon name="x" size={14} /></button>}</div><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} aria-label="合同类型">{["全部类型", ...Array.from(new Set(contracts.map((item) => item.type)))].map((item) => <option key={item}>{item}</option>)}</select><div className="filter-pills">{["全部合同", "待审核", "解析中", "已完成"].map((item) => <button key={item} className={statusFilter === item ? "active" : ""} onClick={() => setStatusFilter(item)}>{item}</button>)}</div></div>
      {view === "table" && <div className="contract-table-head"><span>合同文件</span><span>类型</span><span>风险概览</span><span>健康分</span><span>状态</span><span /></div>}
      <div className={view === "cards" ? "contract-card-grid" : "contract-list"}>{loading ? <div className="data-state"><div className="state-spinner" />正在从 API 网关同步合同…</div> : loadError ? <div className="data-state error"><div><p>{loadError}</p><Button variant="secondary" size="sm" onClick={() => void loadContracts()}>重新连接</Button></div></div> : visibleContracts.length === 0 ? <div className="data-state"><div><Icon name="search" size={22} /><p>没有匹配的合同</p><Button variant="secondary" size="sm" onClick={() => { setQuery(""); setStatusFilter("全部合同"); setTypeFilter("全部类型"); }}>清除筛选</Button></div></div> : visibleContracts.map((contract) => <ContractRow key={contract.id} contract={contract} view={view} />)}</div>
    </section>
    {uploadOpen && <UploadPanel onClose={() => setUploadOpen(false)} onCreated={() => void loadContracts()} />}
  </div>;
}
