/**
 * C73 Zod 运行时响应校验单测（node 环境：纯校验函数 + 源码接线守卫）。
 *
 * 环境说明：本仓库 vitest 无 jsdom/测试库，因此沿用 C61/C71 的做法：
 * 「纯函数/状态断言 + 源码接线守卫」，不渲染组件、不启动真实 HTTP。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientValidationError,
  parseOperationResponse,
  registeredOperationIds,
  validateApiErrorBody,
  validateOperationResponse,
} from "@/api/client";
import { schemas } from "@/api/generated/zod";
import { OPERATION_SCHEMAS, P0_OPERATION_IDS } from "@/api/services";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const CLIENT_SOURCE = read("../client.ts");
const SERVICES_SOURCE = read("../services.ts");

const envelope = (data: unknown) => ({
  code: "SUCCESS",
  traceId: "trace-c73",
  serverTime: "2026-10-02T05:00:00+00:00",
  data,
});

const currentUser = {
  id: "u-1",
  username: "admin",
  displayName: "管理员",
  role: "ADMIN",
  isActive: true,
  createdAt: "2026-10-02T05:00:00+00:00",
  updatedAt: "2026-10-02T05:00:00+00:00",
};

const reportData = {
  reportId: "r-1",
  contractId: "c-1",
  status: "DONE",
  downloadUrl: "https://minio.test/report.docx",
  wordDownloadUrl: "https://minio.test/report.docx",
  pdfDownloadUrl: "https://minio.test/report.pdf",
  generatedAt: "2026-10-02T05:00:00+00:00",
};

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("C73 zod runtime validation", () => {
  it("test_zod_valid_response", () => {
    expect(validateOperationResponse("getCurrentUser", envelope(currentUser))).toBeNull();
    expect(validateOperationResponse("getReport", envelope(reportData))).toBeNull();
    expect(validateOperationResponse("getRisks", envelope([]))).toBeNull();
    expect(
      validateOperationResponse(
        "getTask",
        envelope({ taskId: "t-1", stage: "done", percent: 1, status: "DONE" }),
      ),
    ).toBeNull();
    expect(
      validateOperationResponse(
        "retryContractPreview",
        envelope({ taskId: "t-1", stage: "converting", percent: 0, status: "RUNNING" }),
      ),
    ).toBeNull();
    // 未登记的 operationId 不校验（例如未接线的管理端点）
    expect(validateOperationResponse("notRegistered", { whatever: true })).toBeNull();
  });

  it("test_zod_invalid_rejected", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const badRole = validateOperationResponse(
      "getCurrentUser",
      envelope({ ...currentUser, role: "SUPERADMIN" }),
    );
    expect(badRole).toBeInstanceOf(ApiClientValidationError);
    expect(badRole?.operationId).toBe("getCurrentUser");
    expect(badRole?.traceId).toBe("trace-c73");
    expect(badRole?.issues.join(",")).toContain("role");

    const missingField = validateOperationResponse(
      "getTask",
      envelope({ taskId: "t-1", stage: "done", percent: 1 }),
    );
    expect(missingField).not.toBeNull();
    expect(missingField?.issues.length).toBeGreaterThan(0);

    expect(() =>
      parseOperationResponse("getCurrentUser", envelope({ ...currentUser, role: "BAD" })),
    ).toThrow(ApiClientValidationError);
    expect(errorSpy).toHaveBeenCalled();
  });

  it("test_zod_api_error_shape", () => {
    const validError = {
      code: "ERR_FORBIDDEN",
      message: "无权限",
      detail: "无权限",
      traceId: "trace-403",
    };
    expect(schemas.ApiError.safeParse(validError).success).toBe(true);
    expect(validateApiErrorBody(validError)).toBe(true);

    expect(validateApiErrorBody({ code: "ERR_FORBIDDEN" })).toBe(false);
    expect(validateApiErrorBody({ ...validError, code: "ERR_NOT_A_CODE" })).toBe(false);

    // 兼容期：错误体不硬失败，保留 detail 回退（一个发布周期）
    expect(CLIENT_SOURCE).toContain("保留 detail 回退一个发布周期");
    expect(CLIENT_SOURCE).toContain("validateApiErrorBody(error.response.data)");
  });

  it("test_p0_operation_registry_complete", () => {
    const registered = new Set(registeredOperationIds());
    for (const operationId of P0_OPERATION_IDS) {
      expect(registered.has(operationId), operationId).toBe(true);
    }
    expect(Object.keys(OPERATION_SCHEMAS).length).toBe(registered.size);

    // 每个 httpClient 调用都必须登记 operationId（对外直传 presign PUT 除外）
    const httpCalls = SERVICES_SOURCE.match(/httpClient\.(get|post|put|patch|delete)</g) ?? [];
    const registeredCalls = SERVICES_SOURCE.match(/_operationId: "/g) ?? [];
    expect(httpCalls.length).toBeGreaterThan(0);
    // 泛型化调用（业务端点）必须全部登记；对外直传 presign PUT 无泛型、不校验
    expect(registeredCalls.length).toBe(httpCalls.length);
    expect(SERVICES_SOURCE).toContain("httpClient.put(presignedUrl, file, {");
    expect(SERVICES_SOURCE).toContain('baseURL: ""');

    // 拦截器必须接在 httpClient 上：成功路径校验 + 错误体形状上报
    expect(CLIENT_SOURCE).toContain("parseOperationResponse(operationId, response.data)");
    expect(CLIENT_SOURCE).toContain("registerOperationSchema");
    expect(CLIENT_SOURCE).toContain("registerApiErrorSchema");
  });

  it("test_production_rejects_dirty_data", () => {
    vi.stubEnv("PROD", true);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const dirty = envelope({ ...currentUser, role: "SUPERADMIN" });
    expect(() => parseOperationResponse("getCurrentUser", dirty)).toThrow(
      ApiClientValidationError,
    );
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy).not.toHaveBeenCalled();

    const message = String(warnSpy.mock.calls[0]?.[0] ?? "");
    expect(message).toContain("getCurrentUser");
    expect(message).toContain("trace-c73");
    expect(message).not.toContain("SUPERADMIN"); // 上报不得携带响应内容
  });
});
