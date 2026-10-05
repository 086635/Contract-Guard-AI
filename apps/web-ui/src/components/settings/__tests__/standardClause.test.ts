/**
 * C71 标准条款库管理面板单测（node 环境：纯状态机 + 源码接线守卫）。
 *
 * 环境说明：本仓库 vitest 无 jsdom/测试库，因此不渲染组件；沿用 C61 的做法：
 * 「纯函数/状态机断言 + 源码接线守卫」。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  StandardClauseController,
  canManageStandardClause,
  standardClauseEmptyText,
  standardClauseErrorMessage,
  toListParams,
  type StandardClause,
  type StandardClauseApiPort,
  type StandardClauseCreateInput,
  type StandardClauseListData,
  type StandardClauseUpdateInput,
} from "@/components/settings/standardClauseModel";
import type { components } from "@/api/generated/types";

type TaskProgress = components["schemas"]["TaskProgress"];

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const PANEL_SOURCE = read("../StandardClausePanel.tsx");
const SETTINGS_SOURCE = read("../../../views/Settings.tsx");
const KNOWLEDGE_SOURCE = read("../KnowledgePanel.tsx");
const SERVICES_SOURCE = read("../../../api/services.ts");

const clause = (overrides: Partial<StandardClause> = {}): StandardClause => ({
  id: "clause-1",
  title: "逾期付款违约金",
  contractType: "PURCHASE",
  text: "买方逾期付款的，应按日支付合同总价千分之五的违约金。",
  isEnabled: true,
  version: 1,
  source: "manual",
  updatedAt: "2026-10-02T00:00:00Z",
  ...overrides,
});

const listData = (items: StandardClause[]): StandardClauseListData => ({
  items,
  total: items.length,
  page: 1,
  pageSize: 20,
  hasMore: false,
});

class FakeApi implements StandardClauseApiPort {
  items: StandardClause[] = [];

  listCalls: Record<string, unknown>[] = [];

  createCalls: StandardClauseCreateInput[] = [];

  updateCalls: { id: string; body: StandardClauseUpdateInput }[] = [];

  rebuildCalls = 0;

  failNextList: unknown = null;

  async list(params: { page: number; pageSize: number }): Promise<StandardClauseListData> {
    this.listCalls.push(params);
    if (this.failNextList) {
      const error = this.failNextList;
      this.failNextList = null;
      throw error;
    }
    return listData(this.items);
  }

  async create(body: StandardClauseCreateInput): Promise<StandardClause> {
    this.createCalls.push(body);
    const created = clause({ ...body, id: `clause-${this.items.length + 1}` });
    this.items = [...this.items, created];
    return created;
  }

  async update(clauseId: string, body: StandardClauseUpdateInput): Promise<StandardClause> {
    this.updateCalls.push({ id: clauseId, body });
    const current = this.items.find((item) => item.id === clauseId) ?? clause({ id: clauseId });
    const updated = {
      ...current,
      ...body,
      isEnabled: body.isEnabled ?? current.isEnabled,
      version: current.version + 1,
    } as StandardClause;
    this.items = this.items.map((item) => (item.id === clauseId ? updated : item));
    return updated;
  }

  async rebuildIndex(): Promise<TaskProgress> {
    this.rebuildCalls += 1;
    // 契约把 TaskProgress.result 声明为空对象；面板按 operationId 语义显式收敛一次。
    return {
      taskId: "standard-clause-index",
      stage: "REBUILD_STANDARD_CLAUSE_INDEX",
      percent: 1,
      status: "DONE",
      result: { indexed: this.items.length, removed: 0, indexHealthy: true },
    } as unknown as TaskProgress;
  }
}

const httpError = (status: number): Error => {
  const error = new Error(`HTTP ${status}`);
  Object.assign(error, { response: { status } });
  return error;
};

describe("C71 standard clause panel", () => {
  it("test_standard_clause_page_crud", async () => {
    const api = new FakeApi();
    api.items = [clause()];
    const controller = new StandardClauseController(api, { role: "ADMIN" });

    await controller.load();
    expect(controller.getState().items).toHaveLength(1);
    expect(api.listCalls[0]).toMatchObject({ page: 1, pageSize: 20 });

    expect(await controller.create({ title: "新条款", contractType: "SALES", text: "正文", source: "manual" })).toBe(true);
    expect(api.createCalls).toHaveLength(1);
    expect(api.listCalls).toHaveLength(2); // 保存后强制重读服务端列表
    expect(controller.getState().items).toHaveLength(2);

    expect(await controller.save("clause-1", { title: "改后标题", version: 1 })).toBe(true);
    expect(api.updateCalls[0]).toMatchObject({ id: "clause-1", body: { title: "改后标题", version: 1 } });

    const target = controller.getState().items[0];
    expect(await controller.setEnabled(target, false)).toBe(true);
    expect(api.updateCalls[1]).toMatchObject({ id: target.id, body: { isEnabled: false, version: target.version } });
    expect(controller.getState().items.find((item) => item.id === target.id)?.isEnabled).toBe(false);

    expect(await controller.rebuildIndex()).toBe(true);
    expect(api.rebuildCalls).toBe(1);
    expect(controller.getState().message).toContain("索引重建完成");
  });

  it("test_standard_clause_readonly_role", async () => {
    const api = new FakeApi();
    api.items = [clause()];
    const controller = new StandardClauseController(api, { role: "VIEWER" });

    expect(controller.editable).toBe(false);
    await controller.load();
    expect(api.listCalls).toHaveLength(1);

    expect(await controller.create({ title: "越权", contractType: "PURCHASE", text: "x", source: "manual" })).toBe(false);
    expect(await controller.save("clause-1", { title: "越权", version: 1 })).toBe(false);
    expect(await controller.setEnabled(clause(), false)).toBe(false);
    expect(await controller.rebuildIndex()).toBe(false);

    expect(api.createCalls).toHaveLength(0);
    expect(api.updateCalls).toHaveLength(0);
    expect(api.rebuildCalls).toBe(0);
    expect(canManageStandardClause("REVIEWER")).toBe(false);
    expect(canManageStandardClause("ADMIN")).toBe(true);

    // 视图侧：编辑/停用/重建按钮必须包在 canManage 条件里
    expect(PANEL_SOURCE).toContain("canManage &&");
    expect(PANEL_SOURCE).toContain("canManageStandardClause(role)");
  });

  it("test_standard_clause_empty_state", async () => {
    const api = new FakeApi();
    const controller = new StandardClauseController(api, { role: "ADMIN" });

    await controller.load();
    expect(controller.getState().empty).toBe(true);
    expect(controller.getState().items).toEqual([]);
    expect(controller.getState().error).toBeNull();
    expect(standardClauseEmptyText(true)).toContain("新建条款");
    expect(standardClauseEmptyText(false)).not.toContain("新建条款");

    // 视图侧：空态走统一 data-state，不得渲染演示数据
    expect(PANEL_SOURCE).toContain("standardClauseEmptyText(canManage)");
    expect(PANEL_SOURCE).not.toContain("MockDataFactory");
    expect(SETTINGS_SOURCE).toContain("KnowledgePanel");
    expect(KNOWLEDGE_SOURCE).toContain("StandardClausePanel");
    expect(SETTINGS_SOURCE).not.toContain("未联调");
  });

  it("test_standard_clause_error_retry", async () => {
    const api = new FakeApi();
    const controller = new StandardClauseController(api, { role: "ADMIN" });

    api.failNextList = httpError(503);
    await controller.load();
    expect(controller.getState().error).toContain("索引暂时不可用");

    api.items = [clause()];
    await controller.retry();
    expect(controller.getState().error).toBeNull();
    expect(controller.getState().items).toHaveLength(1);

    expect(standardClauseErrorMessage(httpError(403), "save")).toContain("仅 ADMIN");
    expect(standardClauseErrorMessage(httpError(409), "save")).toContain("版本");
    expect(standardClauseErrorMessage(httpError(422), "save")).toContain("不合法");

    // 服务层必须暴露 4 个标准条款方法，且面板只经服务层请求
    expect(SERVICES_SOURCE).toContain("standardClauseApi");
    for (const method of ["list", "create", "update", "rebuildIndex"]) {
      expect(SERVICES_SOURCE).toContain(method);
    }
    expect(PANEL_SOURCE).not.toContain("fetch(");
  });

  it("test_standard_clause_query_params", () => {
    expect(
      toListParams({ query: "  违约金 ", contractType: "PURCHASE", enabled: "enabled", page: 2 }, 50),
    ).toEqual({ page: 2, pageSize: 50, query: "违约金", contractType: "PURCHASE", isEnabled: true });
    expect(toListParams({ query: "", contractType: "", enabled: "all", page: 1 })).toEqual({
      page: 1,
      pageSize: 20,
    });
  });
});
