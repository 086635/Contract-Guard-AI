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

export const httpClient: AxiosInstance = axios.create({
  baseURL: "/api/v1",
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// ---- 请求拦截：注入 X-Request-ID ----
httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  config.headers.set("X-Request-ID", makeRequestId());
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
  (error) => Promise.reject(error),
);

export default httpClient;
