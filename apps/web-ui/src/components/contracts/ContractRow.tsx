import { useNavigate } from "react-router-dom";
import Badge from "@/components/ui/Badge";
import Icon from "@/components/ui/Icon";

export type ContractItem = { id: string; name: string; type: string; date: string; status: "已完成" | "解析中" | "待审核"; score: number; high: number; medium: number; low: number };

export default function ContractRow({ contract, view = "table" }: { contract: ContractItem; view?: "table" | "cards" }) {
  const navigate = useNavigate();
  const open = () => navigate(`/contracts/${contract.id}/audit`);
  if (view === "cards") return <article className="contract-card" tabIndex={0} onClick={open} onKeyDown={(event) => { if (event.key === "Enter") open(); }}><div className="contract-card-top"><div className="file-icon"><Icon name="file-text" size={20} /></div><Badge tone={contract.status === "已完成" ? "success" : contract.status === "解析中" ? "info" : "medium"} dot>{contract.status}</Badge></div><div className="contract-primary"><b>{contract.name}</b><span>{contract.id}</span></div><div className="contract-card-meta"><span>{contract.type}</span><span>{contract.date}</span></div><div className="risk-counts"><Badge tone="high">高 {contract.high}</Badge><Badge tone="medium">中 {contract.medium}</Badge><Badge tone="low">低 {contract.low}</Badge></div><div className="contract-card-foot"><span>合同健康分</span><strong>{contract.score}<small>/100</small></strong></div></article>;
  return <div className="contract-row" tabIndex={0} onClick={open} onKeyDown={(event) => { if (event.key === "Enter") open(); }}><div className="contract-file-cell"><div className="file-icon"><Icon name="file-text" size={20} /></div><div className="contract-primary"><b>{contract.name}</b><span>{contract.id} · {contract.date}</span></div></div><div className="contract-type">{contract.type}</div><div className="risk-counts"><Badge tone="high">高 {contract.high}</Badge><Badge tone="medium">中 {contract.medium}</Badge><Badge tone="low">低 {contract.low}</Badge></div><div className="score"><strong>{contract.score}</strong><span>/100</span></div><div className="contract-status"><Badge tone={contract.status === "已完成" ? "success" : contract.status === "解析中" ? "info" : "medium"} dot>{contract.status}</Badge></div><button className="row-more" aria-label="更多操作" onClick={(event) => event.stopPropagation()}><Icon name="more" size={18} /></button></div>;
}
