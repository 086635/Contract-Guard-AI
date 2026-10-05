import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import SectionHeader from "@/components/ui/SectionHeader";
import Button from "@/components/ui/Button";
import Badge from "@/components/ui/Badge";
import { loadContractItems, scheduleContractRefresh } from "@/views/Dashboard";
import type { ContractItem } from "@/components/contracts/ContractRow";

export default function ContractHub({ mode }: { mode: "audit" | "report" }) {
  const [items, setItems] = useState<ContractItem[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setError(null); setLoading(true);
    try { setItems(await loadContractItems()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "合同加载失败"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { let active = true; void loadContractItems().then((data) => { if (active) setItems(data); }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "加载失败"); }).finally(() => { if (active) setLoading(false); }); return () => { active = false; }; }, []);
  useEffect(() => scheduleContractRefresh(items, () => { void load(); }), [items, load]);
  const visible = items.filter((item) => `${item.name} ${item.id}`.toLowerCase().includes(query.trim().toLowerCase()));
  return <div className="dashboard-page"><SectionHeader eyebrow={mode === "audit" ? "REVIEW WORKSPACE" : "REPORT CENTER"} title={mode === "audit" ? "选择合同开始审核" : "选择合同查看报告"} description={mode === "audit" ? "查看真实解析结果、定位条款、提交人工反馈。解析失败的合同可在工作台重新解析。" : "查看已有报告，或根据当前审核数据生成报告并下载。"} actions={<Button variant="secondary" disabled={loading} onClick={() => void load()}>刷新合同</Button>} /><div className="panel hub-panel"><label className="search-field"><input aria-label="搜索待选合同" placeholder="搜索合同名称或编号" value={query} onChange={(event) => setQuery(event.target.value)} /></label>{loading ? <div className="data-state">正在加载合同…</div> : error ? <div className="data-state error">{error}</div> : !visible.length ? <div className="data-state"><div><p>{items.length ? "没有匹配合同" : "尚未上传合同"}</p><Link to="/contracts?upload=1">上传合同</Link></div></div> : visible.map((item) => <Link className="hub-contract" key={item.id} to={`/contracts/${item.id}/${mode}`}><div><strong>{item.name}</strong><small>{item.type} · {item.date} · {item.id}</small>{item.errorMessage && <p className="error-text">{item.errorMessage}</p>}</div><Badge tone={item.status === "解析失败" ? "high" : "neutral"}>{item.status}</Badge><span>{mode === "audit" ? "进入审核 →" : "查看报告 →"}</span></Link>)}</div></div>;
}
