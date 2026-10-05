import { useEffect, useState } from "react";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import { useAuthStore } from "@/stores/authStore";
import { feedbackApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Verdict = components["schemas"]["FeedbackVerdict"];

export default function FeedbackDialog({ contractId, targetId, targetLabel, targetType = "RISK", onClose, onSubmitted }: { contractId: string; targetId: string; targetLabel: string; targetType?: components["schemas"]["TargetType"]; onClose: () => void; onSubmitted: (message: string) => void }) {
  const user = useAuthStore((state) => state.user);
  const [verdict, setVerdict] = useState<Verdict>("CORRECT");
  const [correction, setCorrection] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { const close = (event: KeyboardEvent) => { if (event.key === "Escape" && !loading) onClose(); }; window.addEventListener("keydown", close); return () => window.removeEventListener("keydown", close); }, [loading, onClose]);
  const submit = async () => {
    setLoading(true); setError(null);
    try {
      await feedbackApi.create({ contractId, targetId, targetType, verdict, reviewer: user?.displayName || user?.username || null, userCorrection: correction.trim() || null });
      onSubmitted("反馈已提交，将进入后续规则优化样本。"); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "反馈提交失败，请稍后重试。"); }
    finally { setLoading(false); }
  };
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="modal feedback-modal" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><div className="eyebrow">HUMAN IN THE LOOP</div><h2>校正审核结果</h2><p>{targetLabel}</p></div><button className="close-button" onClick={onClose} aria-label="关闭"><Icon name="x" size={20} /></button></div><div className="verdict-options">{([['CORRECT','判断正确'],['FALSE_POSITIVE','这是误报'],['MISSING','存在漏报']] as const).map(([value, label]) => <button key={value} className={verdict === value ? "selected" : ""} onClick={() => setVerdict(value)}><span>{verdict === value && <Icon name="check" size={14} />}</span>{label}</button>)}</div><label className="agent-input-label">补充说明（选填）<textarea rows={4} value={correction} onChange={(event) => setCorrection(event.target.value)} placeholder="说明判断依据，或填写正确的风险描述" /></label>{error && <div className="upload-error"><Icon name="activity" size={15} />{error}</div>}<div className="modal-actions"><Button variant="secondary" onClick={onClose}>取消</Button><Button disabled={loading} onClick={() => void submit()} icon={<Icon name={loading ? "clock" : "send"} size={16} />}>{loading ? "提交中…" : "提交反馈"}</Button></div></section></div>;
}
