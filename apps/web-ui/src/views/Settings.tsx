import { useEffect, useState } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import StandardClausePanel from "@/components/settings/StandardClausePanel";
import { settingsApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Rule = components["schemas"]["SystemAuditConfiguration"];
type Tab = "basic" | "prompt" | "threshold" | "library";
const contractNames: Record<string, string> = { PURCHASE: "采购合同", SALES: "销售合同", NDA: "保密协议", OUTSOURCING: "服务合同", LABOR: "劳动合同", OTHER: "其他合同" };

const percentToRatio = (value: number): number => Math.max(0, Math.min(1, value / 100));
const ratioToPercent = (value: number): number => Math.round(value * 100);

const statusOf = (reason: unknown): number | undefined => {
  if (typeof reason !== "object" || reason === null || !("response" in reason)) return undefined;
  return (reason as { response?: { status?: number } }).response?.status;
};

export default function Settings() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [rule, setRule] = useState<Rule | null>(null);
  const [tab, setTab] = useState<Tab>("basic");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  const selectRule = (next: Rule) => {
    setRule({
      ...next,
      promptConfig: { ...next.promptConfig },
      enabledRules: [...next.enabledRules],
      reviewThresholds: { ...next.reviewThresholds },
      customEntities: [...next.customEntities],
    });
    setMessage(null);
  };

  useEffect(() => {
    let mounted = true;
    settingsApi.list()
      .then((items) => {
        if (!mounted) return;
        setRules(items);
        if (items[0]) selectRule(items[0]);
      })
      .catch((reason) => {
        if (mounted) setMessage(reason instanceof Error ? reason.message : "规则配置暂时无法加载，请确认 API 网关已启动。");
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, []);

  const updateRule = (patch: Partial<Rule>) => setRule((current) => current ? { ...current, ...patch } : current);
  const updatePrompt = (patch: Partial<Rule["promptConfig"]>) => setRule((current) => current ? { ...current, promptConfig: { ...current.promptConfig, ...patch } } : current);
  const toggleRule = (name: string) => setRule((current) => current ? { ...current, enabledRules: current.enabledRules.includes(name) ? current.enabledRules.filter((item) => item !== name) : [...current.enabledRules, name] } : current);

  const save = async () => {
    if (!rule || saving) return;
    setSaving(true);
    setMessage(null);
    try {
      const updated = await settingsApi.update(rule.id, rule);
      setRule(updated);
      setRules((current) => current.map((item) => item.id === updated.id ? updated : item));
      setMessage("规则配置已保存，服务端返回值已刷新。");
    } catch (reason) {
      setMessage(statusOf(reason) === 409 ? "配置版本冲突，请重新加载后再保存。" : reason instanceof Error ? reason.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  };

  const currentRule = rule;
  const enabled = currentRule?.enabled ?? false;
  const thresholdPercent = currentRule ? {
    high: ratioToPercent(currentRule.reviewThresholds.high),
    medium: ratioToPercent(currentRule.reviewThresholds.medium),
    low: ratioToPercent(currentRule.reviewThresholds.low),
  } : { high: 0, medium: 0, low: 0 };
  const contractName = (type: string) => contractNames[type] ?? "其他合同";

  return (
    <div className="settings-page">
      <SectionHeader eyebrow="RULE ENGINE" title="规则引擎与配置中心" description="按合同类型管理审查规则、Agent 角色与人工复核阈值。" actions={<Button icon={<Icon name={saving ? "clock" : "check"} size={16} />} onClick={() => void save()} disabled={saving || !currentRule}>{saving ? "保存中…" : "保存全部修改"}</Button>} />
      {message && <div className={`settings-message ${message.includes("失败") || message.includes("无法") || message.includes("冲突") ? "error" : ""}`}>{message}<button onClick={() => setMessage(null)} aria-label="关闭提示"><Icon name="x" size={14} /></button></div>}
      <div className="settings-layout">
        <aside className="panel rules-tree">
          <div className="panel-heading"><div><h2>规则库</h2><p>{loading ? "正在读取…" : `${rules.length} 套规则配置`}</p></div><button className="icon-button" aria-label="新建规则" disabled><Icon name="plus" size={17} /></button></div>
          <div className="tree-group"><div className="tree-parent"><Icon name="chevron-down" size={14} /><Icon name="briefcase" size={16} /><b>合同类型</b><span>{rules.length}</span></div>{rules.map((item) => <button key={item.id} className={`tree-rule ${currentRule?.id === item.id ? "active" : ""}`} onClick={() => selectRule(item)}><i />{contractName(item.contractType)}审查规则 <Badge tone={item.enabled ? "success" : "neutral"} dot>{item.enabled ? "启用" : "停用"}</Badge></button>)}{!loading && !rules.length && <div className="compact-empty">暂无规则配置</div>}</div>
          <div className="tree-footer"><Icon name="help" size={16} />规则保存采用服务端版本号防止覆盖</div>
        </aside>
        <section className="panel rule-editor">
          {currentRule ? <>
            <div className="editor-heading"><div><div className="eyebrow">{currentRule.contractType} · {currentRule.id.slice(0, 8)}</div><h2>{contractName(currentRule.contractType)}审查标准 <Badge tone={enabled ? "success" : "neutral"} dot>{enabled ? "已启用" : "已停用"}</Badge></h2><p>更新于 {new Date(currentRule.updatedAt).toLocaleString("zh-CN")} · 版本 v{currentRule.version}</p></div><label className="toggle-label"><span>规则启用</span><button className={`toggle ${enabled ? "on" : ""}`} onClick={() => updateRule({ enabled: !enabled })} aria-pressed={enabled}><i /></button></label></div>
            <div className="editor-tabs"><button className={tab === "basic" ? "active" : ""} onClick={() => setTab("basic")}>基本信息</button><button className={tab === "prompt" ? "active" : ""} onClick={() => setTab("prompt")}>Prompt 与角色</button><button className={tab === "threshold" ? "active" : ""} onClick={() => setTab("threshold")}>风险阈值</button><button className={tab === "library" ? "active" : ""} onClick={() => setTab("library")}>标准条款库</button></div>
            {tab === "basic" && <div className="editor-section"><div className="editor-form"><label>规则名称<input value={`${contractName(currentRule.contractType)}审查标准`} readOnly /></label><label>适用合同类型<div className="fake-select">{contractName(currentRule.contractType)} <Icon name="chevron-down" size={15} /></div></label><label className="full">规则说明<textarea value={currentRule.promptConfig.systemPrompt} onChange={(event) => updatePrompt({ systemPrompt: event.target.value })} /></label></div><div className="required-clauses"><h3>启用的检查规则</h3><div>{["penalty_too_high","unlimited_liability","missing_dispute_clause","payment_term_risk","ip_ownership_risk"].map((name) => <button key={name} className={`rule-chip ${currentRule.enabledRules.includes(name) ? "selected" : ""}`} onClick={() => toggleRule(name)}>{currentRule.enabledRules.includes(name) && <Icon name="check" size={13} />}{name}</button>)}</div></div></div>}
            {tab === "prompt" && <div className="editor-section prompt-editor"><label>Agent 角色名称<input value={currentRule.promptConfig.roleName} onChange={(event) => updatePrompt({ roleName: event.target.value })} /></label><label>系统 Prompt<textarea rows={10} value={currentRule.promptConfig.systemPrompt} onChange={(event) => updatePrompt({ systemPrompt: event.target.value })} /></label><div className="inline-controls"><label>随机性 <input type="range" min="0" max="1" step="0.1" value={currentRule.promptConfig.temperature} onChange={(event) => updatePrompt({ temperature: Number(event.target.value) })} /><b>{currentRule.promptConfig.temperature.toFixed(1)}</b></label><label>风险偏好<select value={currentRule.promptConfig.riskToleranceLevel} onChange={(event) => updatePrompt({ riskToleranceLevel: event.target.value as Rule["promptConfig"]["riskToleranceLevel"] })}><option value="STRICT">严格</option><option value="BALANCED">均衡</option><option value="LOOSE">宽松</option></select></label></div></div>}
            {tab === "threshold" && <div className="editor-section threshold-block"><div><h3>人工复核阈值</h3><p>拖动滑块调整各等级进入人工复核队列的置信度；界面使用 0~100，保存时转为 0~1。</p></div>{([['high','高风险'],['medium','中风险'],['low','低风险']] as const).map(([key, label]) => <div className="threshold-row" key={key}><span><i className={`risk-dot ${key}`} />{label}</span><input type="range" min="0" max="100" value={thresholdPercent[key]} onChange={(event) => updateRule({ reviewThresholds: { ...currentRule.reviewThresholds, [key]: percentToRatio(Number(event.target.value)) } })} /><b>{thresholdPercent[key]}%</b></div>)}</div>}
            {tab === "library" && <StandardClausePanel contractType={currentRule.contractType} />}
          </> : <div className="data-state">{loading ? "正在读取规则配置…" : "暂无规则配置"}</div>}
        </section>
      </div>
    </div>
  );
}