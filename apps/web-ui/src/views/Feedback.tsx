import { useEffect, useMemo, useRef, useState } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import { feedbackApi } from "@/api/services";
import { useAuthStore } from "@/stores/authStore";
import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];
type FeedbackItem = Schemas["FeedbackItem"];
type FeedbackVerdict = Schemas["FeedbackVerdict"];

const VERDICT_LABEL: Record<FeedbackVerdict, string> = {
  CORRECT: "确认正确",
  FALSE_POSITIVE: "误报",
  MISSING: "漏报",
};

const VERDICT_TONE: Record<FeedbackVerdict, "success" | "neutral" | "info"> = {
  CORRECT: "success",
  FALSE_POSITIVE: "neutral",
  MISSING: "info",
};

const TARGET_LABEL: Record<Schemas["TargetType"], string> = {
  RISK: "风险",
  CONFLICT: "矛盾",
  SEAL: "印章",
  ELEMENT: "要素",
};

const formatTime = (value: string): string => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
};

export default function Feedback() {
  const canExport = useAuthStore((state) => state.hasRole("ADMIN"));
  const [exporting, setExporting] = useState(false);
  const [data, setData] = useState<Schemas["FeedbackListData"] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [verdictFilter, setVerdictFilter] = useState<"ALL" | FeedbackVerdict>("ALL");
  const activeRef = useRef(true);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await feedbackApi.listAll();
      if (!activeRef.current) return;
      setData(result);
    } catch (reason) {
      if (!activeRef.current) return;
      setError(reason instanceof Error ? reason.message : "反馈列表加载失败，请稍后重试。");
      setData(null);
    } finally {
      if (activeRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    activeRef.current = true;
    void load();
    return () => { activeRef.current = false; };
  }, []);

  const items = data?.items ?? [];
  const filteredItems = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return items.filter((item) => {
      if (verdictFilter !== "ALL" && item.verdict !== verdictFilter) return false;
      if (!keyword) return true;
      return [item.targetId, item.contractId, item.reviewer ?? "", item.userCorrection ?? "", item.targetType, item.verdict]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    });
  }, [items, query, verdictFilter]);

  const now = Date.now();
  const weekCount = items.filter((item) => {
    const timestamp = new Date(item.createdAt).getTime();
    return Number.isFinite(timestamp) && now - timestamp <= 7 * 24 * 60 * 60 * 1000;
  }).length;
  const incorporatedCount = items.filter((item) => item.isIncorporated).length;
  const incorporatedRate = items.length ? Math.round((incorporatedCount / items.length) * 100) : 0;
  const pendingCount = items.filter((item) => !item.isIncorporated).length;

  const exportCsv = async () => {
    if (!items.length || exporting || !canExport) return;
    setExporting(true);
    try {
      const data = await feedbackApi.export();
      if (!activeRef.current) return;
      window.open(data.downloadUrl, "_blank", "noopener,noreferrer");
      setMessage(`已生成 ${data.recordCount} 条反馈记录的下载文件。`);
    } catch (reason) {
      if (activeRef.current) setMessage(reason instanceof Error ? reason.message : "导出失败，请重试。");
    } finally {
      if (activeRef.current) setExporting(false);
    }
  };

  return (
    <div className="feedback-page">
      <SectionHeader
        eyebrow="HUMAN-IN-THE-LOOP · FEEDBACK"
        title="反馈中心"
        description="每一次人工确认，都会让下一次审核更准确。"
        actions={<Button variant="secondary" icon={<Icon name="download" size={16} />} onClick={() => void exportCsv()} disabled={!canExport || !items.length || loading || exporting} title={canExport ? "导出完整标注集" : "完整标注集导出需要管理员权限"}>{exporting ? "正在生成…" : "导出标注集"}</Button>}
      />
      {message && <div className="settings-message">{message}<button onClick={() => setMessage(null)} aria-label="关闭提示"><Icon name="x" size={14} /></button></div>}
      <div className="feedback-stats">
        <div className="panel"><span>本周已标注</span><b>{weekCount}</b><small>来自当前真实列表</small></div>
        <div className="panel"><span>模型采纳率</span><b>{incorporatedRate}%</b><small>{incorporatedCount} / {items.length} 条已采纳</small></div>
        <div className="panel"><span>待处理反馈</span><b>{pendingCount}</b><small>未纳入模型反馈</small></div>
      </div>
      <section className="panel feedback-list">
        <div className="panel-heading">
          <div><h2>反馈记录</h2><p>{loading ? "正在同步 API 数据…" : `已加载 ${items.length} / ${data?.total ?? 0} 条`}</p></div>
          <div className="search-field compact"><Icon name="search" size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索目标、律师或修正内容" /></div>
        </div>
        <div className="feedback-filters">
          {(["ALL", "CORRECT", "FALSE_POSITIVE", "MISSING"] as const).map((value) => (
            <button key={value} className={verdictFilter === value ? "active" : ""} onClick={() => setVerdictFilter(value)}>{value === "ALL" ? "全部" : VERDICT_LABEL[value]}</button>
          ))}
        </div>
        {loading ? (
          <div className="data-state"><div className="state-spinner" />正在读取反馈记录…</div>
        ) : error ? (
          <div className="data-state error"><div><p>{error}</p><Button variant="secondary" size="sm" onClick={() => void load()}>重新加载</Button></div></div>
        ) : filteredItems.length === 0 ? (
          <div className="data-state"><div><Icon name="activity" size={22} /><p>{items.length ? "没有匹配的反馈记录" : "还没有反馈记录"}</p>{items.length > 0 && <Button variant="secondary" size="sm" onClick={() => { setQuery(""); setVerdictFilter("ALL"); }}>清除筛选</Button>}</div></div>
        ) : (
          filteredItems.map((item: FeedbackItem) => (
            <div className="feedback-row" key={item.id}>
              <div className="feedback-symbol"><Icon name="activity" size={18} /></div>
              <div><b>{item.targetType === "ELEMENT" && item.targetId.startsWith("clause-") ? "条款" : TARGET_LABEL[item.targetType]} · {item.targetId}</b><span>{item.contractId} · {formatTime(item.createdAt)}</span>{item.userCorrection && <p>{item.userCorrection}</p>}</div>
              <Badge tone={VERDICT_TONE[item.verdict]} dot>{VERDICT_LABEL[item.verdict]}</Badge>
              <span className="feedback-reviewer">{item.reviewer ?? "未署名"}</span>
              <Badge tone={item.isIncorporated ? "success" : "medium"} dot>{item.isIncorporated ? "已采纳" : "待处理"}</Badge>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
