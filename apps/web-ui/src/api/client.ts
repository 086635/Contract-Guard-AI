/**
 * 全局唯一 HTTP 客户端（Axios 单例）。
 *
 * 对应 data_scheme.md §6.4（全局 HTTP Client 唯一注入点）：
 * - 禁止在业务代码里 `fetch` / `axios.create()` / 第三方请求库；一律经此实例。
 * - 请求拦截：自动注入 X-Request-ID（幂等，§4.2）。
 * - 响应拦截：按 operationId 用**生成物** Zod Schema 校验业务 payload（§6.2）：
 *   开发/测试环境校验失败立即抛 `ApiClientValidationError`（fail fast）；
 *   生产环境记录 operationId/traceId 并**同样拒绝**脏数据进入核心状态（不静默接受）。
 * - `endpointSchemas[operationId]` 的注册在 `services.ts`（唯一处），本文件只做校验与上报。
 */

import axios, { type AxiosInstance, type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import type { ZodTypeAny } from "zod";

declare module "axios" {
  export interface AxiosRequestConfig {
    /** C73：服务层登记的 operationId，响应拦截器据此选择生成 Zod Schema。 */
    _operationId?: string;
  }
}

/** C73：operationId → 生成 Zod Schema 的唯一注册表（禁止手写第二套业务 Schema）。 */
const endpointSchemas = new Map<string, ZodTypeAny>();

let apiErrorSchema: ZodTypeAny | null = null;

export function registerOperationSchema(operationId: string, schema: ZodTypeAny): void {
  endpointSchemas.set(operationId, schema);
}

export function registerApiErrorSchema(schema: ZodTypeAny): void {
  apiErrorSchema = schema;
}

export function registeredOperationIds(): string[] {
  return [...endpointSchemas.keys()].sort();
}

/** 响应脏数据：带 operationId/traceId/issue 路径，**不含响应内容**（C73 §⑥.2）。 */
export class ApiClientValidationError extends Error {
  readonly operationId: string;

  readonly traceId: string | null;

  readonly issues: string[];

  constructor(operationId: string, issues: string[], traceId?: string | null) {
    super(`响应数据校验失败（operationId=${operationId}）`);
    this.name = "ApiClientValidationError";
    this.operationId = operationId;
    this.traceId = traceId ?? null;
    this.issues = issues;
  }
}

function extractTraceId(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const value = (payload as { traceId?: unknown }).traceId;
  return typeof value === "string" && value.trim() ? value : null;
}

function summarizeZodIssues(error: { issues: { path: PropertyKey[]; code: string }[] }): string[] {
  return error.issues
    .slice(0, 5)
    .map((issue) => `${issue.path.map(String).join(".") || "<root>"}:${issue.code}`);
}

function reportValidationFailure(failure: ApiClientValidationError): void {
  const detail = `operationId=${failure.operationId} traceId=${failure.traceId ?? "-"} issues=${failure.issues.join(",")}`;
  if (import.meta.env.PROD) {
    // 生产：只上报（告警）并拒绝脏数据，绝不打印响应内容
    console.warn(`[zod] 脏数据已拒绝：${detail}`);
    return;
  }
  console.error(`[zod] 脏数据已拒绝：${detail}`);
}

/** 校验单个端点响应；合法返回 null，非法返回错误对象（不抛）。 */
export function validateOperationResponse(
  operationId: string,
  payload: unknown,
): ApiClientValidationError | null {
  const schema = endpointSchemas.get(operationId);
  if (!schema) return null;
  const parsed = schema.safeParse(payload);
  if (parsed.success) return null;
  return new ApiClientValidationError(
    operationId,
    summarizeZodIssues(parsed.error),
    extractTraceId(payload),
  );
}

/** 校验失败：上报后抛错（开发 fail fast；生产同样拒绝，不静默接受脏数据）。 */
export function parseOperationResponse(operationId: string, payload: unknown): void {
  const failure = validateOperationResponse(operationId, payload);
  if (!failure) return;
  reportValidationFailure(failure);
  throw failure;
}

/** 错误体是否符合 ApiError（生成物唯一处）；未注册 schema 时视为通过。 */
export function validateApiErrorBody(payload: unknown): boolean {
  if (!apiErrorSchema) return true;
  return apiErrorSchema.safeParse(payload).success;
}

/** 生成本次请求的 UUID（X-Request-ID 幂等）。 */
function makeRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export type AuthRefreshHandler = () => Promise<string | null>;

let accessToken: string | null = null;
let refreshHandler: AuthRefreshHandler | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function setRefreshHandler(handler: AuthRefreshHandler | null): void {
  refreshHandler = handler;
}

export async function refreshAccessToken(): Promise<string | null> {
  if (!refreshHandler) return null;
  if (!refreshPromise) {
    refreshPromise = refreshHandler().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

function isExternalRequest(config: InternalAxiosRequestConfig): boolean {
  return config.baseURL === "" || /^https?:\/\//i.test(config.url ?? "");
}

function isAuthEndpoint(url: string | undefined): boolean {
  if (!url) return false;
  const path = /^https?:\/\//i.test(url) ? new URL(url).pathname : url.split("?")[0];
  return path.endsWith("/auth/login") || path.endsWith("/auth/refresh");
}

type RetryConfig = InternalAxiosRequestConfig & { _retry?: boolean };

type ValidationIssue = { path: string; code: string; message: string };

type ErrorPayload = {
  code?: string;
  traceId?: string;
  serverTime?: string;
  message?: string;
  detail?: string;
  issues?: ValidationIssue[];
};

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function normalizeErrorPayload(data: unknown): ErrorPayload | null {
  if (typeof data !== "object" || data === null) return null;
  const record = data as Record<string, unknown>;
  const rawIssues = Array.isArray(record.issues) ? record.issues : [];
  const issues = rawIssues.flatMap((item) => {
    if (typeof item !== "object" || item === null) return [];
    const issue = item as Record<string, unknown>;
    const path = nonEmptyString(issue.path);
    const code = nonEmptyString(issue.code);
    const message = nonEmptyString(issue.message);
    return path && code && message ? [{ path, code, message }] : [];
  });
  return {
    code: nonEmptyString(record.code),
    traceId: nonEmptyString(record.traceId),
    serverTime: nonEmptyString(record.serverTime),
    message: nonEmptyString(record.message),
    detail: nonEmptyString(record.detail),
    issues,
  };
}

/**
 * 将 traceId 挂到 Axios error 上，供开发日志和调用方诊断使用。
 * 不改变 HTTP 状态码，也不把 traceId 拼进用户可见 message。
 */
function attachTraceId(error: unknown, traceId: string): void {
  if (typeof error !== "object" || error === null) return;
  Object.defineProperty(error, "traceId", {
    value: traceId,
    configurable: true,
    enumerable: false,
  });
}

export const httpClient: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// ---- 请求拦截：注入 X-Request-ID ----
httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  config.headers.set("X-Request-ID", makeRequestId());
  if (accessToken && !config.headers.has("Authorization") && !isExternalRequest(config)) {
    config.headers.set("Authorization", `Bearer ${accessToken}`);
  }
  return config;
});

// ---- 响应拦截：Zod 运行时断言（C73） ----
httpClient.interceptors.response.use(
  (response: AxiosResponse) => {
    const operationId = (response.config as InternalAxiosRequestConfig)._operationId;
    if (operationId) parseOperationResponse(operationId, response.data);
    return response;
  },
  async (error) => {
    const original = error?.config as RetryConfig | undefined;
    // 401 只允许一次 refresh；refresh 端点自身不得递归触发。
    if (
      error?.response?.status === 401 &&
      original &&
      !original._retry &&
      !isAuthEndpoint(original.url) &&
      !isExternalRequest(original)
    ) {
      original._retry = true;
      const nextToken = await refreshAccessToken();
      if (nextToken) {
        original.headers.set("Authorization", `Bearer ${nextToken}`);
        return httpClient(original);
      }
      setAccessToken(null);
    }

    // 网络错误/超时没有 response：保留 Axios 原始错误，不伪造业务错误。
    if (!error?.response) return Promise.reject(error);

    const operationId = (error.config as InternalAxiosRequestConfig | undefined)?._operationId;
    if (import.meta.env.DEV && !validateApiErrorBody(error.response.data)) {
      // 兼容期：错误体不硬失败（保留 detail 回退一个发布周期），只上报形状漂移
      console.warn(`[zod] 错误体不符合 ApiError：operationId=${operationId ?? "-"}`);
    }

    const payload = normalizeErrorPayload(error.response.data);
    const traceId = payload?.traceId;
    if (traceId) attachTraceId(error, traceId);

    const logDetail = payload?.message ?? payload?.detail ?? error.message;
    if (traceId && import.meta.env.DEV) {
      console.warn(`[api] ${traceId}`, logDetail);
    }

    // ApiError.message 优先；detail 只作为兼容回退，不在业务组件中分叉判断。
    const userMessage = payload?.message ?? payload?.detail;
    if (userMessage) error.message = userMessage;
    if (payload) {
      Object.defineProperty(error, "code", { value: payload.code, configurable: true, enumerable: false });
      Object.defineProperty(error, "detail", { value: payload.detail, configurable: true, enumerable: false });
      Object.defineProperty(error, "issues", { value: payload.issues ?? [], configurable: true, enumerable: false });
    }
    return Promise.reject(error);
  },
);

export default httpClient;
