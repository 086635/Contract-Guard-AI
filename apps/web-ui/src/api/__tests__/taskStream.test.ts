/**
 * C65 前端 SSE 令牌与断线轮询单测（node 环境：注入替身，无需 DOM）。
 *
 * 覆盖：订阅自动带 streamToken、断线换新 token、轮询兜底完成、卸载清理 timer/连接，
 * 以及「token 不进日志/错误」「403/404 不忽略也不无脑轮询」。
 */
import { describe, expect, it } from "vitest";

import {
  MAX_STREAM_ATTEMPTS,
  TASK_POLL_INTERVAL_MS,
  createTaskSubscription,
  parseTaskProgress,
  type TaskSubscriptionDeps,
  type TimerHandle,
} from "@/api/services";
import type { components } from "@/api/generated/types";

type TaskProgress = components["schemas"]["TaskProgress"];

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const progress = (status: TaskProgress["status"]): TaskProgress => ({
  taskId: "t-1",
  stage: status === "DONE" ? "done" : "parse_text",
  percent: status === "DONE" ? 1 : 0.5,
  status,
  result: null,
});

type FakeStream = {
  url: string;
  handlers: { onEvent: (data: string) => void; onError: () => void };
  closed: boolean;
};

type FakeTimer = {
  handle: TimerHandle;
  handler: () => void;
  ms: number;
  cleared: boolean;
};

function makeDeps(overrides: Partial<TaskSubscriptionDeps> = {}) {
  const issued: string[] = [];
  const streams: FakeStream[] = [];
  const timers: FakeTimer[] = [];
  let timerSeq = 1;

  const deps: TaskSubscriptionDeps = {
    issueToken: async () => {
      const token = `tok-${issued.length + 1}`;
      issued.push(token);
      return token;
    },
    fetchProgress: async () => progress("DONE"),
    openStream: (url, handlers) => {
      const stream: FakeStream = { url, handlers, closed: false };
      streams.push(stream);
      return { close: () => { stream.closed = true; } };
    },
    setTimer: (handler, ms) => {
      const handle = timerSeq++ as unknown as TimerHandle;
      timers.push({ handle, handler, ms, cleared: false });
      return handle;
    },
    clearTimer: (handle) => {
      const timer = timers.find((item) => item.handle === handle);
      if (timer) timer.cleared = true;
    },
    streamBaseUrl: () => "/api/v1",
    ...overrides,
  };

  return { deps, issued, streams, timers };
}

describe("C65 SSE 订阅", () => {
  it("test_subscribe_uses_stream_token", async () => {
    const { deps, streams } = makeDeps();
    const seen: TaskProgress[] = [];

    const unsubscribe = createTaskSubscription("t-1", (p) => seen.push(p), undefined, deps);
    await flush();

    expect(streams).toHaveLength(1);
    expect(streams[0].url).toBe("/api/v1/tasks/t-1/stream?streamToken=tok-1");
    // 绝不把 access token / Bearer 拼进 URL
    expect(streams[0].url).not.toContain("Bearer");
    expect(streams[0].url).not.toContain("access");

    streams[0].handlers.onEvent(JSON.stringify(progress("RUNNING")));
    expect(seen.map((item) => item.status)).toEqual(["RUNNING"]);
    expect(streams[0].closed).toBe(false);

    unsubscribe();
    expect(streams[0].closed).toBe(true);
  });

  it("test_expired_token_requests_new_one", async () => {
    const { deps, streams, issued } = makeDeps();

    const unsubscribe = createTaskSubscription("t-1", () => undefined, undefined, deps);
    await flush();
    expect(issued).toEqual(["tok-1"]);

    // 令牌过期/连接中断 → 关闭旧连接并**重新取 token** 重连一次
    streams[0].handlers.onError();
    await flush();

    expect(issued).toEqual(["tok-1", "tok-2"]);
    expect(streams).toHaveLength(MAX_STREAM_ATTEMPTS);
    expect(streams[0].closed).toBe(true);
    expect(streams[1].url).toContain("streamToken=tok-2");

    unsubscribe();
  });

  it("test_polling_fallback_completes", async () => {
    let calls = 0;
    const { deps, streams, timers } = makeDeps({
      fetchProgress: async () => {
        calls += 1;
        return progress(calls >= 1 ? "DONE" : "RUNNING");
      },
    });
    const seen: TaskProgress[] = [];

    const unsubscribe = createTaskSubscription("t-1", (p) => seen.push(p), undefined, deps);
    await flush();

    // 两次流尝试都失败 → 回退 2 秒轮询，并立即拉一次
    streams[0].handlers.onError();
    await flush();
    streams[1].handlers.onError();
    await flush();

    expect(timers).toHaveLength(1);
    expect(timers[0].ms).toBe(TASK_POLL_INTERVAL_MS);
    expect(seen.map((item) => item.status)).toEqual(["DONE"]);
    // 终态 → 统一 finish：timer 被清理、连接关闭
    expect(timers[0].cleared).toBe(true);
    expect(streams[1].closed).toBe(true);

    unsubscribe();
  });

  it("test_unsubscribe_cleans_timers", async () => {
    let calls = 0;
    const { deps, streams, timers } = makeDeps({
      fetchProgress: async () => {
        calls += 1;
        return progress("RUNNING");
      },
    });

    const unsubscribe = createTaskSubscription("t-1", () => undefined, undefined, deps);
    await flush();
    streams[0].handlers.onError();
    await flush();
    streams[1].handlers.onError();
    await flush();

    expect(timers).toHaveLength(1);
    const callsBeforeUnsubscribe = calls;

    unsubscribe();

    expect(timers[0].cleared).toBe(true);
    expect(streams[1].closed).toBe(true);

    // 卸载后即使 timer 回调被触发，也不得再请求（无泄漏、无悬挂订阅）
    timers[0].handler();
    await flush();
    expect(calls).toBe(callsBeforeUnsubscribe);
  });

  it("test_terminal_error_stops_without_polling", async () => {
    const denied = Object.assign(new Error("无权限"), { code: "ERR_FORBIDDEN" });
    const { deps, streams, timers } = makeDeps({
      issueToken: async () => {
        throw denied;
      },
    });
    const errors: unknown[] = [];

    const unsubscribe = createTaskSubscription("t-1", () => undefined, (e) => errors.push(e), deps);
    await flush();

    // 403 只尝试一次：不重连、不轮询、不忽略
    expect(streams).toHaveLength(0);
    expect(timers).toHaveLength(0);
    expect(errors).toEqual([denied]);
    expect((errors[0] as Error).message).toBe("无权限");

    unsubscribe();
  });

  it("test_token_never_reaches_error_callback_or_logs", async () => {
    const failure = new Error("stream failed");
    const { deps } = makeDeps({
      issueToken: async () => "tok-secret",
      openStream: () => {
        throw failure;
      },
    });
    const errors: unknown[] = [];

    const unsubscribe = createTaskSubscription("t-1", () => undefined, (e) => errors.push(e), deps);
    await flush();
    unsubscribe();

    const serialized = JSON.stringify(errors.map((item) => String(item)));
    expect(serialized).not.toContain("tok-secret");
    expect(errors.length).toBeGreaterThan(0);
  });

  it("test_progress_parser_rejects_invalid_payload", () => {
    expect(parseTaskProgress("not-json")).toBeNull();
    expect(parseTaskProgress(JSON.stringify({ taskId: "t-1" }))).toBeNull();
    expect(parseTaskProgress(JSON.stringify({ data: progress("RUNNING") }))?.status).toBe("RUNNING");
  });
});
