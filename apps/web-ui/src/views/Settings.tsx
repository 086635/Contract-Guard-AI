import { useEffect, useState } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import SectionHeader from "@/components/ui/SectionHeader";
import KnowledgePanel from "@/components/settings/KnowledgePanel";
import Dialog from "@/components/ui/Dialog";
import { settingsApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type Rule = components["schemas"]["SystemAuditConfiguration"];
type Tab = "basic" | "prompt" | "threshold" | "library";
const checkNames: Record<string, string> = { penalty_too_high: "违约金过高", unlimited_liability: "无限责任", missing_dispute_clause: "缺少争议解决", payment_term_risk: "付款期限", ip_ownership_risk: "知识产权归属" };
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
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState<Rule["contractType"]>("PURCHASE");
  const [newPrompt, setNewPrompt] = useState("");

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
    if (!rule.promptConfig.roleName.trim() || !rule.promptConfig.systemPrompt.trim()) { setMessage("规则名称和规则说明不能为空。"); return; }
    if (rule.reviewThresholds.high < rule.reviewThresholds.medium || rule.reviewThresholds.medium < rule.reviewThresholds.low) { setMessage("阈值应满足：高风险 ≥ 中风险 ≥ 低风险，请调整后保存。"); return; }
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
  const create = async () => {
    if (!newName.trim() || !newPrompt.trim()) { setMessage("请填写规则名称和规则说明。"); return; }
    setSaving(true); setMessage(null);
    try {
      const created = await settingsApi.create({ id: crypto.randomUUID(), contractType: newType, promptConfig: { roleName: newName.trim(), systemPrompt: newPrompt.trim(), temperature: 0, riskToleranceLevel: "BALANCED" }, enabledRules: [], reviewThresholds: { high: .85, medium: .65, low: .4 }, customEntities: [], enabled: true, version: 1, updatedAt: new Date().toISOString() });
      setRules((current) => [...current, created]); selectRule(created); setCreating(false); setNewName(""); setNewPrompt(""); setMessage("规则已创建并保存，可以继续配置检查项和阈值。");
    } catch (reason) { setMessage(statusOf(reason) === 409 ? "规则名称已存在，请使用其他名称。" : reason instanceof Error ? reason.message : "创建失败"); }
    finally { setSaving(false); }
  };
  const reload = async () => {
    if (!rule) return;
    setSaving(true);
    try { const latest = await settingsApi.get(rule.id); selectRule(latest); setRules((current) => current.map((item) => item.id === latest.id ? latest : item)); setMessage("已读取服务端最新版本，请重新编辑后保存。"); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "重新加载失败"); }
    finally { setSaving(false); }
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
      <SectionHeader eyebrow="RULE ENGINE" title="规则引擎与配置中心" description="按合同类型管理审查规则、Agent 角色与人工复核阈值。" actions={<><Button variant="secondary" onClick={() => setCreating(true)} disabled={saving}>新建规则</Button><Button variant="secondary" onClick={() => void reload()} disabled={saving || !currentRule}>读取最新版本</Button><Button icon={<Icon name={saving ? "clock" : "check"} size={16} />} onClick={() => void save()} disabled={saving || !currentRule}>{saving ? "保存中…" : "保存全部修改"}</Button></>} />
      {message && <div className={`settings-message ${message.includes("失败") || message.includes("无法") || message.includes("冲突") ? "error" : ""}`}>{message}<button onClick={() => setMessage(null)} aria-label="关闭提示"><Icon name="x" size={14} /></button></div>}
      <div className="settings-layout">
        <aside className="panel rules-tree">
          <div className="panel-heading"><div><h2>规则库</h2><p>{loading ? "正在读取…" : `${rules.length} 套规则配置`}</p></div><button className="icon-button" aria-label="添加规则" onClick={() => setCreating(true)} disabled={saving}><Icon name="plus" size={17} /></button></div>
          <div className="tree-group"><div className="tree-parent"><Icon name="chevron-down" size={14} /><Icon name="briefcase" size={16} /><b>合同类型</b><span>{rules.length}</span></div>{rules.map((item) => <button key={item.id} className={`tree-rule ${currentRule?.id === item.id ? "active" : ""}`} onClick={() => selectRule(item)}><i />{item.promptConfig.roleName} <Badge tone={item.enabled ? "success" : "neutral"} dot>{item.enabled ? "启用" : "停用"}</Badge></button>)}{!loading && !rules.length && <div className="compact-empty">暂无规则配置，请点击新建规则。</div>}</div>
          <div className="tree-footer"><Icon name="help" size={16} />规则保存采用服务端版本号防止覆盖</div>
        </aside>
        <section className="panel rule-editor">
          {currentRule ? <>
            <div className="editor-heading"><div><div className="eyebrow">{currentRule.contractType} · {currentRule.id.slice(0, 8)}</div><h2>{contractName(currentRule.contractType)}审查标准 <Badge tone={enabled ? "success" : "neutral"} dot>{enabled ? "已启用" : "已停用"}</Badge></h2><p>更新于 {new Date(currentRule.updatedAt).toLocaleString("zh-CN")} · 版本 v{currentRule.version}</p></div><label className="toggle-label"><span>规则启用</span><button className={`toggle ${enabled ? "on" : ""}`} onClick={() => updateRule({ enabled: !enabled })} aria-pressed={enabled}><i /></button></label></div>
            <div className="editor-tabs"><button className={tab === "basic" ? "active" : ""} onClick={() => setTab("basic")}>基本信息</button><button className={tab === "prompt" ? "active" : ""} onClick={() => setTab("prompt")}>Prompt 与角色</button><button className={tab === "threshold" ? "active" : ""} onClick={() => setTab("threshold")}>风险阈值</button><button className={tab === "library" ? "active" : ""} onClick={() => setTab("library")}>审核知识库</button></div>
            {tab === "basic" && <div className="editor-section"><div className="editor-form"><label>规则名称<input value={currentRule.promptConfig.roleName} onChange={(event) => updatePrompt({ roleName: event.target.value })} /></label><label>适用合同类型<select value={currentRule.contractType} onChange={(event) => updateRule({ contractType: event.target.value as Rule["contractType"] })}>{Object.entries(contractNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="full">规则说明<textarea value={currentRule.promptConfig.systemPrompt} onChange={(event) => updatePrompt({ systemPrompt: event.target.value })} /></label></div><div className="required-clauses"><h3>启用的检查规则</h3><div>{["penalty_too_high","unlimited_liability","missing_dispute_clause","payment_term_risk","ip_ownership_risk"].map((name) => <button key={name} className={`rule-chip ${currentRule.enabledRules.includes(name) ? "selected" : ""}`} aria-pressed={currentRule.enabledRules.includes(name)} onClick={() => toggleRule(name)}>{currentRule.enabledRules.includes(name) && <Icon name="check" size={13} />}{checkNames[name] ?? name}</button>)}</div></div></div>}
            {tab === "prompt" && <div className="editor-section prompt-editor"><label>Agent 角色名称<input value={currentRule.promptConfig.roleName} onChange={(event) => updatePrompt({ roleName: event.target.value })} /></label><label>系统 Prompt<textarea rows={10} value={currentRule.promptConfig.systemPrompt} onChange={(event) => updatePrompt({ systemPrompt: event.target.value })} /></label><div className="inline-controls"><label>随机性 <input type="range" min="0" max="1" step="0.1" value={currentRule.promptConfig.temperature} onChange={(event) => updatePrompt({ temperature: Number(event.target.value) })} /><b>{currentRule.promptConfig.temperature.toFixed(1)}</b></label><label>风险偏好<select value={currentRule.promptConfig.riskToleranceLevel} onChange={(event) => updatePrompt({ riskToleranceLevel: event.target.value as Rule["promptConfig"]["riskToleranceLevel"] })}><option value="STRICT">严格</option><option value="BALANCED">均衡</option><option value="LOOSE">宽松</option></select></label></div></div>}
            {tab === "threshold" && <div className="editor-section threshold-block"><div><h3>人工复核阈值</h3><p>拖动滑块调整各等级进入人工复核队列的置信度；界面使用 0~100，保存时转为 0~1。</p></div>{([['high','高风险'],['medium','中风险'],['low','低风险']] as const).map(([key, label]) => <div className="threshold-row" key={key}><span><i className={`risk-dot ${key}`} />{label}</span><input type="range" min="0" max="100" value={thresholdPercent[key]} onChange={(event) => updateRule({ reviewThresholds: { ...currentRule.reviewThresholds, [key]: percentToRatio(Number(event.target.value)) } })} /><b>{thresholdPercent[key]}%</b></div>)}</div>}
            {tab === "library" && <KnowledgePanel contractType={currentRule.contractType} />}
          </> : <div className="data-state">{loading ? "正在读取规则配置…" : "暂无规则配置"}</div>}
        </section>
      </div>
      {creating && <Dialog title="新建规则" onClose={() => { if (!saving) setCreating(false); }}><form onSubmit={(event) => { event.preventDefault(); void create(); }} className="editor-form"><label>规则名称<input required value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="输入唯一的规则名称" /></label><label>适用合同类型<select value={newType} onChange={(event) => setNewType(event.target.value as Rule["contractType"])}>{Object.entries(contractNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="full">规则说明<textarea required rows={5} value={newPrompt} onChange={(event) => setNewPrompt(event.target.value)} placeholder="说明审核重点和判断要求" /></label>{message && <p className="full error-text" role="status">{message}</p>}<div className="modal-actions full"><Button type="button" variant="secondary" disabled={saving} onClick={() => setCreating(false)}>取消</Button><Button type="submit" disabled={saving}>{saving ? "创建中…" : "创建并保存"}</Button></div></form></Dialog>}
    </div>
  );
}
