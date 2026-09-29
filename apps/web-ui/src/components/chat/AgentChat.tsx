import { useEffect, useRef, useState } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import { agentApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type RewriteData = components["schemas"]["RewriteData"];
type AgentChatProps = {
  contractId: string;
  clauseId: string;
  originalText: string;
  onClose: () => void;
  onApplied?: (data: RewriteData) => void;
};

export default function AgentChat({ contractId, clauseId, originalText, onClose, onApplied }: AgentChatProps) {
  const [instruction, setInstruction] = useState("请将违约金调整为与实际损失相匹配，并补充总额上限。");
  const [result, setResult] = useState<RewriteData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRef = useRef(true);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => () => {
    activeRef.current = false;
  }, []);

  const close = () => {
    activeRef.current = false;
    onCloseRef.current();
  };

  const rewrite = async () => {
    if (!instruction.trim() || loading) return;
    activeRef.current = true;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await agentApi.rewrite({
        clauseId,
        contractId,
        instruction: instruction.trim(),
        xRequestId: crypto.randomUUID(),
        scopedMemory: {
          targetClauseId: clauseId,
          draftAfter: originalText,
          recentDialog: [],
        },
      });
      if (!activeRef.current) return;
      setResult(data);
    } catch (reason) {
      if (!activeRef.current) return;
      setError(reason instanceof Error ? reason.message : "AI 改写暂时不可用，请稍后重试。");
    } finally {
      if (activeRef.current) setLoading(false);
    }
  };

  const applyResult = () => {
    if (!result) return;
    onApplied?.(result);
    close();
  };

  return (
    <div className="modal-backdrop" onMouseDown={close}>
      <section className="modal agent-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-heading">
          <div>
            <div className="eyebrow">CLAUSE AGENT · {clauseId.slice(0, 8)}</div>
            <h2>让 AI 改写条款</h2>
            <p>基于当前条款、审核规则和法条溯源生成可追踪的 Diff。</p>
          </div>
          <button className="close-button" onClick={close} aria-label="关闭"><Icon name="x" size={20} /></button>
        </div>
        <div className="agent-context"><span>原条款</span><p>{originalText}</p></div>
        <label className="agent-input-label">改写要求<textarea disabled={loading} value={instruction} onChange={(event) => setInstruction(event.target.value)} rows={3} placeholder="描述你希望 AI 如何调整条款" /></label>
        {error && <div className="upload-error"><Icon name="activity" size={15} /><span>{error}</span></div>}
        {result && (
          <div className="rewrite-result">
            <div className="rewrite-result-head"><Badge tone="success" dot>已生成建议</Badge><span>{new Date(result.updatedAt).toLocaleTimeString("zh-CN")}</span></div>
            <div className="diff-block before"><small>原文</small><p>{result.before}</p></div>
            <div className="diff-arrow"><Icon name="arrow-up-right" size={15} /></div>
            <div className="diff-block after"><small>建议稿</small><p>{result.after}</p></div>
            {result.citations.length ? result.citations.map((citation, index) => (
              <div className="citation" key={`${citation.sourceTitle}-${index}`}><Icon name="book" size={14} /><span>{citation.sourceTitle}</span></div>
            )) : (
              <div className="citation empty"><Icon name="book" size={14} /><span>暂无引用来源</span></div>
            )}
          </div>
        )}
        <div className="modal-actions">
          <Button variant="secondary" onClick={close}>取消</Button>
          {result ? (
            <Button onClick={applyResult} icon={<Icon name="check" size={16} />}>采用这版修改</Button>
          ) : (
            <Button disabled={loading || !instruction.trim()} onClick={() => void rewrite()} icon={<Icon name={loading ? "clock" : "sparkles"} size={16} />}>{loading ? "生成中…" : error ? "重试生成" : "生成改写建议"}</Button>
          )}
        </div>
      </section>
    </div>
  );
}