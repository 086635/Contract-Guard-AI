import { useEffect, useRef, useState } from "react";
import Button from "@/components/ui/Button";
import StandardClausePanel from "./StandardClausePanel";
import { knowledgeApi, type KnowledgeResult, type KnowledgeState } from "@/api/knowledge";
import type { components } from "@/api/generated/types";

type Library = "standard" | "clauses" | "law";
export default function KnowledgePanel({ contractType }: { contractType: components["schemas"]["ContractType"] }) {
  const [library, setLibrary] = useState<Library>("standard");
  const [state, setState] = useState<KnowledgeState | null>(null);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<KnowledgeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const requestId = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    knowledgeApi.sources(controller.signal).then(setState).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "知识库状态读取失败");
    });
    return () => { controller.abort(); requestId.current += 1; };
  }, []);
  useEffect(() => { requestId.current += 1; setResult(null); setSearching(false); }, [contractType]);
  const choose = (next: Library) => { requestId.current += 1; setLibrary(next); setResult(null); setSearching(false); setError(null); };
  const search = async () => {
    if (query.trim().length < 2 || searching) return;
    const id = ++requestId.current;
    setSearching(true); setError(null); setResult(null);
    try {
      const found = await knowledgeApi.search(query.trim(), library === "law" ? "law" : library === "clauses" ? "clauses" : "all", contractType);
      if (id === requestId.current) setResult(found);
    } catch (reason) {
      if (id === requestId.current) setError(reason instanceof Error ? reason.message : "检索失败，请检查本地向量服务");
    } finally { if (id === requestId.current) setSearching(false); }
  };
  return <div className="editor-section">
    <h3>审核知识库</h3>
    {state ? <>
      <p role="status">本地向量化：{state.embedding.model} · {state.embedding.dimension} 维 · {state.embedding.status === "ok" ? "已就绪" : "服务异常"}；生成模型：{state.generation.provider} / {state.generation.model}</p>
      <p>{state.notice}</p>
    </> : <p>正在读取知识库状态…</p>}
    <div className="required-clauses"><div>
      {([['standard', '标准库', state?.standardDocuments], ['clauses', '条款库', state?.enabledClauses], ['law', '法典库', state?.lawArticles]] as const).map(([id, title, count]) => <button type="button" key={id} className={`rule-chip ${library === id ? "selected" : ""}`} aria-pressed={library === id} onClick={() => choose(id)}>{title} {count ?? "—"}</button>)}
    </div></div>
    {library === "clauses" ? <StandardClausePanel contractType={contractType} /> : state?.sources.filter((source) => source.library === library).map((source) => <article key={source.doc_id} style={{ margin: "16px 0" }}>
      <h4>{source.title} · {source.code}</h4>
      <p>发布日期：{source.published}{source.effective_date ? `；施行日期：${source.effective_date}` : ""}</p>
      <p>适用范围：{source.scope}</p>
      <a href={source.source_url} target="_blank" rel="noreferrer">查看官方原文</a>
    </article>)}
    <form className="editor-form" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label className="full">检索预览<input aria-label="检索条款或法条" value={query} maxLength={2000} onChange={(event) => setQuery(event.target.value)} placeholder="例如：买方逾期付款，应承担什么责任" /></label>
      <div className="full"><Button type="submit" disabled={searching || query.trim().length < 2}>{searching ? "正在检索…" : "检索依据"}</Button></div>
    </form>
    {error && <p role="alert" className="error-text">{error}</p>}
    {result && <div aria-live="polite">
      <p>{result.items.length ? `检索到 ${result.items.length} 条依据，审核时按预算注入模型上下文。` : "没有召回相关依据，请补充资料或调整检索词。"}</p>
      {result.warnings.map((warning, index) => <p key={index}>{warning}</p>)}
      {result.items.map((item, index) => <article key={`${item.sourceTitle}-${index}`} style={{ margin: "16px 0" }}>
        <h4>{item.sourceTitle}</h4><p>语义相关度：{item.relevanceScore.toFixed(3)}；{item.scope}</p>
        <p style={{ whiteSpace: "pre-wrap" }}>{item.contentChunk}</p>
        {item.sourceUrl && <a href={item.sourceUrl} target="_blank" rel="noreferrer">核对官方来源</a>}
      </article>)}
    </div>}
  </div>;
}
