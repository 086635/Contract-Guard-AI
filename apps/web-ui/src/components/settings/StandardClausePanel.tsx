/** C71 标准条款库管理面板：Settings →「标准条款库」tab 的真实数据实现。 */

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { standardClauseApi } from "@/api/services";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import { useAuthStore } from "@/stores/authStore";
import {
  StandardClauseController,
  canManageStandardClause,
  standardClauseEmptyText,
  type ContractType,
  type EnabledFilter,
  type StandardClause,
} from "@/components/settings/standardClauseModel";

const CONTRACT_TYPES: ReadonlyArray<{ value: ContractType; label: string }> = [
  { value: "PURCHASE", label: "采购合同" },
  { value: "SALES", label: "销售合同" },
  { value: "NDA", label: "保密协议" },
  { value: "OUTSOURCING", label: "服务合同" },
  { value: "LABOR", label: "劳动合同" },
  { value: "OTHER", label: "其他合同" },
];

const contractLabel = (value: ContractType): string =>
  CONTRACT_TYPES.find((item) => item.value === value)?.label ?? value;

type Draft =
  | { mode: "create"; title: string; contractType: ContractType; text: string }
  | {
      mode: "edit";
      clause: StandardClause;
      title: string;
      contractType: ContractType;
      text: string;
    };

export default function StandardClausePanel({ contractType }: { contractType: ContractType }) {
  const role = useAuthStore((state) => state.user?.role ?? null);
  const controller = useMemo(
    () => new StandardClauseController(standardClauseApi, { role, contractType }),
    [role, contractType],
  );
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const canManage = canManageStandardClause(role);

  useEffect(() => {
    void controller.load();
  }, [controller]);

  const submitDraft = async () => {
    if (!draft) return;
    const title = draft.title.trim();
    const text = draft.text.trim();
    if (!title || !text) {
      controller.setFilters({});
      return;
    }
    const ok =
      draft.mode === "create"
        ? await controller.create({
            title,
            contractType: draft.contractType,
            text,
            source: "manual",
          })
        : await controller.save(draft.clause.id, {
            title,
            contractType: draft.contractType,
            text,
            version: draft.clause.version,
          });
    if (ok) setDraft(null);
  };

  return (
    <div className="editor-section standard-clause-panel">
      <div className="standard-clause-head">
        <div>
          <h3>{contractLabel(contractType)}标准条款库</h3>
          <p>
            共 {state.total} 条 · 停用条款不进入 RAG 检索；保存后自动重读服务端版本。
          </p>
        </div>
        {canManage && (
          <div className="standard-clause-actions">
            <Button
              size="sm"
              variant="secondary"
              icon={<Icon name="refresh" size={14} />}
              disabled={state.saving}
              onClick={() => void controller.rebuildIndex()}
            >
              重建索引
            </Button>
            <Button
              size="sm"
              icon={<Icon name="plus" size={14} />}
              onClick={() => setDraft({ mode: "create", title: "", contractType, text: "" })}
            >
              新建条款
            </Button>
          </div>
        )}
      </div>

      <div className="standard-clause-toolbar">
        <label className="standard-clause-search">
          <Icon name="search" size={14} />
          <input
            value={state.filters.query}
            placeholder="搜索标题或正文"
            onChange={(event) => controller.setFilters({ query: event.target.value })}
          />
        </label>
        <select
          aria-label="合同类型过滤"
          value={state.filters.contractType}
          onChange={(event) =>
            controller.setFilters({ contractType: event.target.value as ContractType | "" })
          }
        >
          <option value="">全部合同类型</option>
          {CONTRACT_TYPES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <select
          aria-label="启停状态过滤"
          value={state.filters.enabled}
          onChange={(event) =>
            controller.setFilters({ enabled: event.target.value as EnabledFilter })
          }
        >
          <option value="all">全部状态</option>
          <option value="enabled">仅启用</option>
          <option value="disabled">仅停用</option>
        </select>
        <Button
          size="sm"
          variant="ghost"
          disabled={state.loading}
          onClick={() => void controller.load()}
        >
          查询
        </Button>
      </div>

      {(state.error || state.message) && (
        <div className={`standard-clause-message ${state.error ? "error" : ""}`}>
          <span>{state.error ?? state.message}</span>
          {state.error && (
            <button type="button" onClick={() => void controller.retry()}>
              重试
            </button>
          )}
          <button type="button" aria-label="关闭提示" onClick={() => controller.clearNotice()}>
            <Icon name="x" size={14} />
          </button>
        </div>
      )}

      {draft && (
        <div className="standard-clause-editor">
          <label>
            标题
            <input
              value={draft.title}
              onChange={(event) => setDraft({ ...draft, title: event.target.value })}
            />
          </label>
          <label>
            合同类型
            <select
              value={draft.contractType}
              onChange={(event) =>
                setDraft({ ...draft, contractType: event.target.value as ContractType })
              }
            >
              {CONTRACT_TYPES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="full">
            条款正文
            <textarea
              rows={5}
              value={draft.text}
              onChange={(event) => setDraft({ ...draft, text: event.target.value })}
            />
          </label>
          <div className="standard-clause-editor-actions">
            <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>
              取消
            </Button>
            <Button size="sm" disabled={state.saving} onClick={() => void submitDraft()}>
              {state.saving ? "保存中…" : "保存"}
            </Button>
          </div>
        </div>
      )}

      <div className="standard-clause-list">
        {state.loading && !state.items.length && (
          <div className="data-state">正在读取标准条款…</div>
        )}
        {!state.loading && state.empty && (
          <div className="data-state">{standardClauseEmptyText(canManage)}</div>
        )}
        {state.items.map((clause) => (
          <article key={clause.id} className="standard-clause-item">
            <div className="standard-clause-item-head">
              <b>{clause.title}</b>
              <Badge tone={clause.isEnabled ? "success" : "neutral"} dot>
                {clause.isEnabled ? "启用" : "停用"}
              </Badge>
              <span className="standard-clause-meta">
                {contractLabel(clause.contractType)} · v{clause.version} · {clause.source}
              </span>
            </div>
            <p>{clause.text}</p>
            {canManage && (
              <div className="standard-clause-item-actions">
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<Icon name="pen" size={14} />}
                  onClick={() =>
                    setDraft({
                      mode: "edit",
                      clause,
                      title: clause.title,
                      contractType: clause.contractType,
                      text: clause.text,
                    })
                  }
                >
                  编辑
                </Button>
                <Button
                  size="sm"
                  variant={clause.isEnabled ? "danger" : "secondary"}
                  disabled={state.saving}
                  onClick={() => void controller.setEnabled(clause, !clause.isEnabled)}
                >
                  {clause.isEnabled ? "停用" : "启用"}
                </Button>
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
