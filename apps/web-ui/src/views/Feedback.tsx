import { useState } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import { feedbackApi } from "@/api/services";

const CONTRACT_ID = "00000000-0000-0000-0000-000000000001";
const items = [
  { id: "00000000-0000-0000-0000-000000000010", title: "违约金比例过高", clause: "第 3.2 条", verdict: "确认风险", tone: "success" as const, apiVerdict: "CORRECT" as const, time: "今天 09:48" },
  { id: "00000000-0000-0000-0000-000000000050", title: "争议解决条款缺失", clause: "第 5 条", verdict: "补充条款", tone: "info" as const, apiVerdict: "MISSING" as const, time: "昨天 17:22" },
  { id: "00000000-0000-0000-0000-000000000030", title: "付款责任计算口径矛盾", clause: "第 6.1 条", verdict: "误报", tone: "neutral" as const, apiVerdict: "FALSE_POSITIVE" as const, time: "2026-08-22" },
];

export default function Feedback() {
  const [submitted, setSubmitted] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const submitFeedback = async (item: (typeof items)[number]) => { if (submitting || submitted.includes(item.id)) return; setSubmitting(item.id); try { await feedbackApi.create({ contractId: CONTRACT_ID, targetId: item.id, targetType: "RISK", verdict: item.apiVerdict, reviewer: "王钘宇", userCorrection: null }); setSubmitted((current) => [...current, item.id]); } catch (error) { console.warn("[feedback] failed to submit", error); } finally { setSubmitting(null); } };
  return <div className="feedback-page"><SectionHeader eyebrow="HUMAN-IN-THE-LOOP · FEEDBACK" title="反馈中心" description="每一次人工确认，都会让下一次审核更准确。" actions={<Button variant="secondary" icon={<Icon name="download" size={16} />}>导出标注集</Button>} /><div className="feedback-stats"><div className="panel"><span>本周已标注</span><b>38</b><small>较上周 +12</small></div><div className="panel"><span>模型采纳率</span><b>94.2%</b><small>持续提升中</small></div><div className="panel"><span>待处理反馈</span><b>6</b><small>需要你的判断</small></div></div><section className="panel feedback-list"><div className="panel-heading"><div><h2>最近标注</h2><p>用于审核模型结果并优化规则</p></div><div className="search-field compact"><Icon name="search" size={16} /><input placeholder="搜索反馈内容" /></div></div>{items.map((item) => <div className="feedback-row" key={item.id}><div className="feedback-symbol"><Icon name="file-text" size={18} /></div><div><b>{item.title}</b><span>{item.clause} · 采购合同_宏图盛达_202608.docx</span></div><Badge tone={item.tone} dot>{item.verdict}</Badge><time>{item.time}</time><Button variant={submitted.includes(item.id) ? "secondary" : "ghost"} size="sm" disabled={submitting === item.id || submitted.includes(item.id)} onClick={() => submitFeedback(item)}>{submitted.includes(item.id) ? "已提交" : submitting === item.id ? "提交中…" : "提交反馈"}</Button></div>)}</section></div>;
}
