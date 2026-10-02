/**
 * C71 标准条款库面板的纯逻辑（vitest node 环境可直接测）。
 *
 * 设计要点：
 * 1. 组件只做渲染与事件转发；状态机、权限判断、错误文案都在本模块，可离线单测。
 * 2. 写操作成功后强制重读服务端列表（§⑥.3「保存后刷新服务端版本」）。
 * 3. 非 ADMIN 的写操作直接短路返回 false 且不发请求；后端仍以 ADMIN-only 为准（前端只做体验层）。
 */

import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];

export type StandardClause = Schemas["StandardClause"];
export type StandardClauseCreateInput = Schemas["StandardClauseCreateRequest"];
export type StandardClauseUpdateInput = Schemas["StandardClauseUpdateRequest"];
export type StandardClauseListData = Schemas["StandardClauseListData"];
export type TaskProgress = Schemas["TaskProgress"];
export type ContractType = Schemas["ContractType"];
export type Role = Schemas["Role"];

export const STANDARD_CLAUSE_PAGE_SIZE = 20;

export type EnabledFilter = "all" | "enabled" | "disabled";

export interface StandardClauseFilters {
  query: string;
  contractType: ContractType | "";
  enabled: EnabledFilter;
  page: number;
}

export interface StandardClauseListParams {
  page: number;
  pageSize: number;
  query?: string;
  contractType?: ContractType;
  isEnabled?: boolean;
}

/** 面板依赖的 API 端口：由 `standardClauseApi` 实现，测试可注入替身。 */
export interface StandardClauseApiPort {
  list(params: StandardClauseListParams): Promise<StandardClauseListData>;
  create(body: StandardClauseCreateInput): Promise<StandardClause>;
  update(clauseId: string, body: StandardClauseUpdateInput): Promise<StandardClause>;
  rebuildIndex(): Promise<TaskProgress>;
}

export interface StandardClauseState {
  items: StandardClause[];
  total: number;
  hasMore: boolean;
  loading: boolean;
  saving: boolean;
  error: string | null;
  message: string | null;
  empty: boolean;
  filters: StandardClauseFilters;
}

export interface StandardClauseControllerOptions {
  role?: Role | null;
  contractType?: ContractType | null;
  pageSize?: number;
}

export const canManageStandardClause = (role: Role | null | undefined): boolean =>
  role === "ADMIN";

export const httpStatusOf = (error: unknown): number | undefined => {
  if (typeof error !== "object" || error === null || !("response" in error)) {
    return undefined;
  }
  return (error as { response?: { status?: number } }).response?.status;
};

/** 409/403/422/503 给明确文案（§⑥.2），其余回退服务端 message 或动作级兜底。 */
export const standardClauseErrorMessage = (
  error: unknown,
  action: "load" | "save" | "rebuild",
): string => {
  const status = httpStatusOf(error);
  if (status === 403) return "无权限维护标准条款库（仅 ADMIN 可操作）。";
  if (status === 409) return "标准条款版本冲突，请刷新后重试。";
  if (status === 404) return "标准条款不存在，可能已被删除。";
  if (status === 422 || status === 400) {
    return "提交内容不合法：请检查标题、合同类型与正文。";
  }
  if (status === 503) return "检索索引暂时不可用，请稍后重试。";
  if (error instanceof Error && error.message) return error.message;
  if (action === "load") return "标准条款库加载失败，请重试。";
  if (action === "rebuild") return "索引重建失败，请稍后重试。";
  return "保存失败，请稍后重试。";
};

export const toListParams = (
  filters: StandardClauseFilters,
  pageSize: number = STANDARD_CLAUSE_PAGE_SIZE,
): StandardClauseListParams => {
  const keyword = filters.query.trim();
  return {
    page: filters.page,
    pageSize,
    ...(keyword ? { query: keyword } : {}),
    ...(filters.contractType ? { contractType: filters.contractType } : {}),
    ...(filters.enabled === "all" ? {} : { isEnabled: filters.enabled === "enabled" }),
  };
};

/** 标准条款空态文案（无数据时不得展示演示数据，§⑥.4）。 */
export const standardClauseEmptyText = (canManage: boolean): string =>
  canManage
    ? "暂无标准条款，点击「新建条款」添加第一条。"
    : "暂无标准条款。";

export class StandardClauseController {
  private state: StandardClauseState;

  private readonly listeners = new Set<() => void>();

  private readonly pageSize: number;

  private readonly canManage: boolean;

  private retryAction: (() => Promise<void>) | null = null;

  constructor(
    private readonly api: StandardClauseApiPort,
    options: StandardClauseControllerOptions = {},
  ) {
    this.pageSize = options.pageSize ?? STANDARD_CLAUSE_PAGE_SIZE;
    this.canManage = canManageStandardClause(options.role);
    this.state = {
      items: [],
      total: 0,
      hasMore: false,
      loading: false,
      saving: false,
      error: null,
      message: null,
      empty: false,
      filters: {
        query: "",
        contractType: options.contractType ?? "",
        enabled: "all",
        page: 1,
      },
    };
  }

  /** React `useSyncExternalStore` 用：快照引用只在状态变更时替换。 */
  readonly getState = (): StandardClauseState => this.state;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  get editable(): boolean {
    return this.canManage;
  }

  setFilters(patch: Partial<StandardClauseFilters>): void {
    this.patch({ filters: { ...this.state.filters, ...patch, page: patch.page ?? 1 } });
  }

  clearNotice(): void {
    this.patch({ error: null, message: null });
  }

  async load(): Promise<void> {
    this.patch({ loading: true, error: null, message: null });
    this.retryAction = async () => {
      await this.load();
    };
    try {
      const data = await this.api.list(toListParams(this.state.filters, this.pageSize));
      this.patch({
        items: data.items,
        total: data.total,
        hasMore: data.hasMore,
        empty: data.items.length === 0,
        loading: false,
      });
    } catch (error) {
      this.patch({ loading: false, error: standardClauseErrorMessage(error, "load") });
    }
  }

  async create(input: StandardClauseCreateInput): Promise<boolean> {
    if (!this.canManage) return false;
    this.patch({ saving: true, error: null, message: null });
    this.retryAction = async () => {
      await this.create(input);
    };
    try {
      await this.api.create(input);
      await this.refreshAfterWrite("标准条款已创建，列表已刷新。");
      return true;
    } catch (error) {
      this.patch({ saving: false, error: standardClauseErrorMessage(error, "save") });
      return false;
    }
  }

  async save(clauseId: string, patch: StandardClauseUpdateInput): Promise<boolean> {
    if (!this.canManage) return false;
    this.patch({ saving: true, error: null, message: null });
    this.retryAction = async () => {
      await this.save(clauseId, patch);
    };
    try {
      await this.api.update(clauseId, patch);
      await this.refreshAfterWrite("标准条款已保存，已按服务端最新版本刷新。");
      return true;
    } catch (error) {
      this.patch({ saving: false, error: standardClauseErrorMessage(error, "save") });
      return false;
    }
  }

  async setEnabled(clause: StandardClause, enabled: boolean): Promise<boolean> {
    return this.save(clause.id, { isEnabled: enabled, version: clause.version });
  }

  async rebuildIndex(): Promise<boolean> {
    if (!this.canManage) return false;
    this.patch({ saving: true, error: null, message: null });
    this.retryAction = async () => {
      await this.rebuildIndex();
    };
    try {
      const progress = await this.api.rebuildIndex();
      const result = (progress.result ?? {}) as {
        indexed?: number;
        removed?: number;
        indexHealthy?: boolean;
      };
      await this.load();
      this.patch({
        saving: false,
        message: `索引重建完成：写入 ${result.indexed ?? 0} 条，移除 ${result.removed ?? 0} 条${
          result.indexHealthy === false ? "（索引健康检查未通过）" : ""
        }。`,
      });
      return true;
    } catch (error) {
      this.patch({ saving: false, error: standardClauseErrorMessage(error, "rebuild") });
      return false;
    }
  }

  async retry(): Promise<void> {
    const action = this.retryAction;
    if (!action) {
      await this.load();
      return;
    }
    await action();
  }

  private async refreshAfterWrite(message: string): Promise<void> {
    await this.load();
    this.patch({ saving: false, message });
  }

  private patch(partial: Partial<StandardClauseState>): void {
    this.state = { ...this.state, ...partial };
    this.listeners.forEach((listener) => listener());
  }
}
