/** AI 多轮改写 Agent 对话框（携带分层 ScopedClauseMemory，§10）。 */
export default function AgentChat({ clauseId }: { clauseId?: string }) {
  return (
    <div className="agent-chat">
      <p>AI 改写对话框占位{clauseId ? `（条款 ${clauseId}）` : ""}。</p>
      <p>POST /agent/rewrite 提交自然语言指令，返回 RewriteResponse（Diff + CoT + 溯源）。</p>
    </div>
  );
}
