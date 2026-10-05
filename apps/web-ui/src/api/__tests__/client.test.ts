import { afterEach, describe, expect, it } from "vitest";
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";

import {
  httpClient,
  refreshAccessToken,
  setAccessToken,
  setRefreshHandler,
} from "@/api/client";

const originalAdapter = httpClient.defaults.adapter;

describe("httpClient 401 refresh", () => {
  afterEach(() => {
    setRefreshHandler(null);
    setAccessToken(null);
    httpClient.defaults.adapter = originalAdapter;
  });

  it("test_401_single_flight_refresh", async () => {
    let refreshCalls = 0;
    setAccessToken("expired-access-token");
    setRefreshHandler(async () => {
      refreshCalls += 1;
      return "new-access-token";
    });

    const adapter: AxiosAdapter = async (config: InternalAxiosRequestConfig) => {
      const authorization = config.headers.get("Authorization");
      if (authorization === "Bearer expired-access-token") {
        const error = Object.assign(new Error("Unauthorized"), {
          config,
          response: {
            status: 401,
            statusText: "Unauthorized",
            headers: {},
            config,
            data: {},
          },
        });
        throw error;
      }
      return {
        data: { ok: true },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
      };
    };
    httpClient.defaults.adapter = adapter;

    const [first, second] = await Promise.all([
      httpClient.get("/protected"),
      httpClient.get("/protected"),
    ]);

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(refreshCalls).toBe(1);
  });

  it("normalizes ApiError message detail and issues", async () => {
    const adapter: AxiosAdapter = async (config: InternalAxiosRequestConfig) => {
      const error = Object.assign(new Error("Request failed"), {
        config,
        response: {
          status: 409,
          statusText: "Conflict",
          headers: {},
          config,
          data: {
            code: "ERR_DUPLICATE_REQUEST",
            message: "请求重复",
            detail: "request id reused with different body",
            traceId: "tr-c52",
            issues: [{ path: "fileKey", code: "duplicate", message: "重复请求" }],
          },
        },
      });
      throw error;
    };
    httpClient.defaults.adapter = adapter;

    try {
      await httpClient.get("/conflict");
      throw new Error("expected request to fail");
    } catch (error) {
      const apiError = error as Error & {
        code?: string;
        detail?: string;
        issues?: Array<{ path: string; code: string; message: string }>;
      };
      expect(apiError.message).toBe("请求重复");
      expect(apiError.code).toBe("ERR_DUPLICATE_REQUEST");
      expect(apiError.detail).toBe("request id reused with different body");
      expect(apiError.issues).toEqual([
        { path: "fileKey", code: "duplicate", message: "重复请求" },
      ]);
    }
  });

  it("refreshAccessToken de-duplicates concurrent refresh calls", async () => {
    let calls = 0;
    let resolveRefresh: ((token: string | null) => void) | undefined;
    setRefreshHandler(
      () =>
        new Promise<string | null>((resolve) => {
          calls += 1;
          resolveRefresh = resolve;
        }),
    );

    const first = refreshAccessToken();
    const second = refreshAccessToken();
    expect(calls).toBe(1);

    resolveRefresh?.("new-access-token");
    await expect(first).resolves.toBe("new-access-token");
    await expect(second).resolves.toBe("new-access-token");
  });

  it.each([
    [422, { message: "请检查上传文件", detail: "validation failed" }, "请检查上传文件"],
    [409, { message: "数据冲突", detail: "该文件此前已上传过" }, "该文件此前已上传过"],
    [400, { detail: "文件格式不受支持" }, "文件格式不受支持"],
    [400, { message: " ", detail: "请填写文件名" }, "请填写文件名"],
    [400, {}, "Request failed"],
  ])("keeps readable errors for status %s and body %j", async (status, data, expected) => {
    httpClient.defaults.adapter = async (config) => {
      throw Object.assign(new Error("Request failed"), {
        config,
        response: { status, statusText: "Error", headers: {}, config, data },
      });
    };
    await expect(httpClient.get("/error")).rejects.toHaveProperty("message", expected);
  });

  it("preserves the original network error", async () => {
    const networkError = new Error("Network Error");
    httpClient.defaults.adapter = async () => { throw networkError; };
    await expect(httpClient.get("/unreachable")).rejects.toBe(networkError);
  });
});
