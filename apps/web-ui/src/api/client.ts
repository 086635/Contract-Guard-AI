/**
 * 全局唯一 HTTP 客户端（Axios 单例）。
 *
 * 对应 data_scheme.md §6.4（全局 HTTP Client 唯一注入点）：
 * - 禁止在业务代码里 `fetch` / `axios.create()` / 第三方请求库；一律经此实例。
 * - 请求拦截：自动注入 X-Request-ID（幂等，§4.2）。
 * - 响应拦截：开发环境用 Zod 运行时断言校验 Payload（§6.2），遇脏数据立即报错，
 *   避免 AI 堆砌 if-else 兜底；生产环境仅记录。
 *
 * ⚠️ 生成后：从 "@/api/generated/zod" 导入各端点 Zod Schema 并在拦截器中绑定。
 */

import axios, { type AxiosInstance, type InternalAxiosRequestConfig, type AxiosResponse } from "axios";

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

type ErrorPayload = {
  code?: string;
  traceId?: string;
  serverTime?: string;
  message?: string;
  detail?: string;
};

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function normalizeErrorPayload(data: unknown): ErrorPayload | null {
  if (typeof data !== "object" || data === null) return null;
  const record = data as Record<string, unknown>;
  return {
    code: nonEmptyString(record.code),
    traceId: nonEmptyString(record.traceId),
    serverTime: nonEmptyString(record.serverTime),
    message: nonEmptyString(record.message),
    detail: nonEmptyString(record.detail),
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

// ---- 响应拦截：Zod 运行时断言（开发环境） ----
httpClient.interceptors.response.use(
  (response: AxiosResponse) => {
    // TODO（工程实现，生成后启用）：
    //   import { schemas } from "@/api/generated/zod";
    //   const schema = schemas[endpointKey];
    //   if (import.meta.env.DEV && schema) {
    //     const parsed = schema.safeParse(response.data);
    //     if (!parsed.success) console.error("[zod] 脏数据", parsed.error);
    //   }
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

    const payload = normalizeErrorPayload(error.response.data);
    const traceId = payload?.traceId;
    if (traceId) attachTraceId(error, traceId);

    const logDetail = payload?.message ?? payload?.detail ?? error.message;
    if (traceId && import.meta.env.DEV) {
      console.warn(`[api] ${traceId}`, logDetail);
    }

    // message 优先；仅当 message 缺失时使用 detail。原始 HTTP status 保持在 error.response.status。
    const userMessage = payload?.message ?? payload?.detail;
    if (userMessage) error.message = userMessage;
    return Promise.reject(error);
  },
);

export default httpClient;
