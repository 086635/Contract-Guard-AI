import httpClient from "@/api/client";
import type { components } from "@/api/generated/types";

type Schemas = components["schemas"];
type Envelope<T> = { code: "SUCCESS"; traceId: string; serverTime: string; data: T };
type ContractListData = Schemas["ContractListResponse"]["data"];
type Contract = Schemas["Contract"];
type TaskProgress = Schemas["TaskProgress"];
type FileType = Schemas["FileType"];
type UploadBody = Schemas["UploadBody"];
type FeedbackCreateBody = Schemas["FeedbackCreateBody"];
type AgentRewriteRequest = Schemas["AgentRewriteRequest"];
type SystemAuditConfiguration = Schemas["SystemAuditConfiguration"];
type LoginRequest = Schemas["LoginRequest"];
type TokenData = Schemas["TokenData"];
type CurrentUser = Schemas["CurrentUser"];
type LogoutData = Schemas["LogoutData"];

const unwrap = <T>(response: { data: Envelope<T> }): T => {
  if (response.data.code !== "SUCCESS") throw new Error(`API request failed: ${response.data.code}`);
  return response.data.data;
};

/** EventSource 端点返回裸 TaskProgress（而普通 HTTP 返回统一包络）。 */
const apiBase = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");

export const authApi = {
  async login(body: LoginRequest): Promise<TokenData> {
    return unwrap(
      await httpClient.post<Envelope<TokenData>>("/auth/login", body, {
        withCredentials: true,
      }),
    );
  },
  async refresh(): Promise<TokenData> {
    return unwrap(
      await httpClient.post<Envelope<TokenData>>("/auth/refresh", {}, {
        withCredentials: true,
      }),
    );
  },
  async logout(): Promise<LogoutData> {
    return unwrap(
      await httpClient.post<Envelope<LogoutData>>("/auth/logout", {}, {
        withCredentials: true,
      }),
    );
  },
  async me(): Promise<CurrentUser> {
    return unwrap(await httpClient.get<Envelope<CurrentUser>>("/auth/me"));
  },
};

export const contractApi = {
  async list(params: { page?: number; pageSize?: number; status?: Schemas["ContractStatus"]; contractType?: Schemas["ContractType"] } = {}): Promise<ContractListData> {
    const response = await httpClient.get<Envelope<ContractListData>>("/contracts", { params: { page: params.page, page_size: params.pageSize, status: params.status, contract_type: params.contractType } });
    return unwrap(response);
  },
  async detail(contractId: string): Promise<Contract> { return unwrap(await httpClient.get<Envelope<Contract>>(`/contracts/${contractId}`)); },
  async elements(contractId: string): Promise<Schemas["ContractElement"][]> { return unwrap(await httpClient.get<Envelope<Schemas["ContractElement"][]>>(`/contracts/${contractId}/elements`)); },
  async risks(contractId: string, level?: Schemas["RiskLevel"]): Promise<Schemas["EnhancedRiskItem"][]> { return unwrap(await httpClient.get<Envelope<Schemas["EnhancedRiskItem"][]>>(`/contracts/${contractId}/risks`, { params: { level } })); },
  async conflicts(contractId: string): Promise<Schemas["CrossClauseConflict"][]> { return unwrap(await httpClient.get<Envelope<Schemas["CrossClauseConflict"][]>>(`/contracts/${contractId}/conflicts`)); },
  async seals(contractId: string): Promise<Schemas["SealSignatureDetection"][]> { return unwrap(await httpClient.get<Envelope<Schemas["SealSignatureDetection"][]>>(`/contracts/${contractId}/seals`)); },
  async parseStatus(contractId: string): Promise<TaskProgress> { return unwrap(await httpClient.get<Envelope<TaskProgress>>(`/contracts/${contractId}/parse-status`)); },
  async commit(contractId: string, body: Schemas["CommitBody"]): Promise<Schemas["BatchResult"]> { return unwrap(await httpClient.post<Envelope<Schemas["BatchResult"]>>(`/contracts/${contractId}/commit`, body)); },
};

export const uploadApi = {
  async presign(fileName: string, fileType: FileType, sizeBytes?: number, contentType?: string): Promise<Schemas["PresignData"]> {
    // C56：sizeBytes/contentType 仅作服务端准入输入，服务端不复用其真实性判断。
    return unwrap(await httpClient.post<Envelope<Schemas["PresignData"]>>("/contracts/presign", { fileName, fileType, sizeBytes, contentType }));
  },
  async directUpload(presignedUrl: string, file: File): Promise<string | null> {
    // 原样使用后端签发的 public presign URL；失败必须向调用方抛出，不得静默跳过。
    const response = await httpClient.put(presignedUrl, file, {
      baseURL: "",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      timeout: 120_000,
    });
    // C56：MinIO PUT 响应带 ETag；浏览器能否读到取决于对象存储 CORS 是否暴露该头。
    const etag = response.headers?.etag;
    return typeof etag === "string" && etag ? etag : null;
  },
  async submit(body: UploadBody): Promise<TaskProgress> { return unwrap(await httpClient.post<Envelope<TaskProgress>>("/contracts/upload", body)); },
};

export const previewApi = {
  /** 预览元数据 + 短期签名 URL（C58）。 */
  async get(contractId: string): Promise<Schemas["PreviewData"]> {
    return unwrap(await httpClient.get<Envelope<Schemas["PreviewData"]>>(`/contracts/${contractId}/preview`));
  },
  /** 重试 DOCX→PDF 预览转换（C59；仅 ADMIN/REVIEWER，X-Request-ID 幂等）。 */
  async retry(contractId: string): Promise<TaskProgress> {
    return unwrap(await httpClient.post<Envelope<TaskProgress>>(`/contracts/${contractId}/preview/retry`));
  },
};

/** 进度事件解析（唯一处）：SSE 载荷可能是裸 TaskProgress 或包络内的 data。 */
export function parseTaskProgress(raw: string): TaskProgress | null {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null) return null;
    const candidate = "data" in parsed ? (parsed as { data?: unknown }).data : parsed;
    if (typeof candidate !== "object" || candidate === null) return null;
    const progress = candidate as Partial<TaskProgress>;
    if (
      typeof progress.taskId !== "string" ||
      typeof progress.stage !== "string" ||
      typeof progress.percent !== "number" ||
      !["RUNNING", "DONE", "FAILED"].includes(progress.status as string)
    ) {
      return null;
    }
    return candidate as TaskProgress;
  } catch {
    return null;
  }
}

/** 轮询兜底间隔（毫秒）——与后端「SSE 断线后转 2 秒轮询」的约定一致。 */
export const TASK_POLL_INTERVAL_MS = 2_000;
/** SSE 最多尝试次数：首次 + **一次换新 token 重连**（禁止无限重连）。 */
export const MAX_STREAM_ATTEMPTS = 2;

export type StreamHandle = { close: () => void };
export type TimerHandle = ReturnType<typeof setInterval>;

/** 可注入的订阅依赖：默认走真实 httpClient/EventSource，测试注入替身（无需 DOM）。 */
export type TaskSubscriptionDeps = {
  issueToken: (taskId: string) => Promise<string>;
  fetchProgress: (taskId: string) => Promise<TaskProgress>;
  openStream: (
    url: string,
    handlers: { onEvent: (data: string) => void; onError: () => void },
  ) => StreamHandle;
  setTimer: (handler: () => void, ms: number) => TimerHandle;
  clearTimer: (handle: TimerHandle) => void;
  streamBaseUrl: () => string;
};

const TERMINAL_STATUSES = new Set<TaskProgress["status"]>(["DONE", "FAILED"]);
/** 权限/存在性错误（C51 错误码）：终结订阅并提示，**不忽略、不无脑轮询**（§② 不做）。 */
const TERMINAL_ERROR_CODES = new Set([
  "ERR_UNAUTHORIZED",
  "ERR_FORBIDDEN",
  "ERR_CONTRACT_NOT_FOUND",
]);

function isTerminalError(error: unknown): boolean {
  const code = (error as { code?: unknown } | null | undefined)?.code;
  return typeof code === "string" && TERMINAL_ERROR_CODES.has(code);
}

/**
 * 订阅任务进度（C65）。
 *
 * 状态机：
 *   ① `POST /tasks/{id}/stream-token` 取**短期令牌** → 用 `?streamToken=` 开 EventSource（**不放 access token**）；
 *   ② 流断开/令牌过期 → 关闭旧连接、**重新取 token** 再连一次（`MAX_STREAM_ATTEMPTS` 封顶，不无限重连）；
 *   ③ 再失败 → 回退 2 秒轮询 `GET /tasks/{id}`，直到终态；
 *   ④ 终态或 `unsubscribe()` → 统一清理 timer 与连接（组件卸载无泄漏）。
 *
 * 令牌只出现在 EventSource URL 里：不写日志、不进错误对象（⑥-4 / §⑨-1）。
 */
export function createTaskSubscription(
  taskId: string,
  onProgress: (progress: TaskProgress) => void,
  onError: ((error: unknown) => void) | undefined,
  deps: TaskSubscriptionDeps,
): () => void {
  let stopped = false;
  let attempts = 0;
  let polling = false;
  let pollTimer: TimerHandle | null = null;
  let source: StreamHandle | null = null;

  const clearPollTimer = () => {
    if (pollTimer !== null) {
      deps.clearTimer(pollTimer);
      pollTimer = null;
    }
  };
  const finish = () => {
    if (stopped) return;
    stopped = true;
    clearPollTimer();
    source?.close();
    source = null;
  };
  const emit = (progress: TaskProgress) => {
    if (stopped) return;
    onProgress(progress);
    if (TERMINAL_STATUSES.has(progress.status)) finish();
  };
  const poll = async () => {
    if (stopped || polling) return;
    polling = true;
    try {
      const progress = await deps.fetchProgress(taskId);
      if (!stopped) emit(progress);
    } catch (error) {
      // 权限/任务不存在 → 终结订阅并提示；网络抖动 → 保留轮询兜底，下一拍继续。
      if (!stopped && isTerminalError(error)) {
        onError?.(error);
        finish();
      }
    } finally {
      polling = false;
    }
  };
  const startPolling = () => {
    if (stopped || pollTimer !== null) return;
    pollTimer = deps.setTimer(() => {
      void poll();
    }, TASK_POLL_INTERVAL_MS);
    void poll();
  };
  const handleStreamError = (error?: unknown) => {
    if (stopped) return;
    source?.close();
    source = null;
    if (isTerminalError(error)) {
      // 403/404 等终结性错误：不做轮询噪音、不无限重连，直接提示并收尾
      onError?.(error);
      finish();
      return;
    }
    if (attempts < MAX_STREAM_ATTEMPTS) {
      // 每次重连都必须换新 token（旧 token 可能已过期/已被消费）
      void connect();
      return;
    }
    onError?.(error);
    startPolling();
  };
  const connect = async () => {
    if (stopped) return;
    // 先计一次尝试：即使取 token 抛错（403/404/网络），也必须受 MAX_STREAM_ATTEMPTS 约束
    attempts += 1;
    try {
      const token = await deps.issueToken(taskId);
      if (stopped) return;
      const url = `${deps.streamBaseUrl()}/tasks/${taskId}/stream?streamToken=${encodeURIComponent(token)}`;
      source = deps.openStream(url, {
        onEvent: (data) => {
          const progress = parseTaskProgress(data);
          if (progress) emit(progress);
        },
        onError: () => handleStreamError(),
      });
    } catch (error) {
      handleStreamError(error);
    }
  };

  void connect();
  return finish;
}

export const taskApi = {
  async get(taskId: string): Promise<TaskProgress> { return unwrap(await httpClient.get<Envelope<TaskProgress>>(`/tasks/${taskId}`)); },
  /** 取短期 SSE 订阅令牌（C64；Token 只用于 EventSource URL，不写日志）。 */
  async issueStreamToken(taskId: string): Promise<Schemas["StreamTokenData"]> {
    return unwrap(await httpClient.post<Envelope<Schemas["StreamTokenData"]>>(`/tasks/${taskId}/stream-token`, {}));
  },
  subscribe(
    taskId: string,
    onProgress: (progress: TaskProgress) => void,
    onError?: (error: unknown) => void,
  ): () => void {
    return createTaskSubscription(taskId, onProgress, onError, {
      issueToken: async (id) => (await taskApi.issueStreamToken(id)).streamToken,
      fetchProgress: (id) => taskApi.get(id),
      openStream: (url, handlers) => {
        const source = new EventSource(url);
        source.addEventListener("progress", (event) =>
          handlers.onEvent(String((event as MessageEvent).data)),
        );
        source.onerror = () => handlers.onError();
        return { close: () => source.close() };
      },
      setTimer: (handler, ms) => setInterval(handler, ms),
      clearTimer: (handle) => clearInterval(handle),
      streamBaseUrl: () => apiBase,
    });
  },
};

export const reportApi = {
  async get(contractId: string): Promise<Schemas["ReportData"]> { return unwrap(await httpClient.get<Envelope<Schemas["ReportData"]>>(`/report/${contractId}`)); },
  async generate(contractId: string): Promise<TaskProgress> { return unwrap(await httpClient.post<Envelope<TaskProgress>>(`/report/${contractId}/generate`, {})); },
};

export const settingsApi = {
  async list(contractType?: Schemas["ContractType"]): Promise<SystemAuditConfiguration[]> { return unwrap(await httpClient.get<Envelope<SystemAuditConfiguration[]>>("/settings/rules", { params: { contract_type: contractType } })); },
  async get(configId: string): Promise<SystemAuditConfiguration> { return unwrap(await httpClient.get<Envelope<SystemAuditConfiguration>>(`/settings/rules/${configId}`)); },
  async update(configId: string, body: SystemAuditConfiguration): Promise<SystemAuditConfiguration> { return unwrap(await httpClient.put<Envelope<SystemAuditConfiguration>>(`/settings/rules/${configId}`, body)); },
};

export const feedbackApi = {
  async list(
    params: {
      contractId?: string;
      targetType?: Schemas["TargetType"];
      verdict?: Schemas["FeedbackVerdict"];
      page?: number;
      pageSize?: number;
    } = {},
  ): Promise<Schemas["FeedbackListData"]> {
    return unwrap(await httpClient.get<Envelope<Schemas["FeedbackListData"]>>("/feedback", {
      params: {
        contractId: params.contractId,
        targetType: params.targetType,
        verdict: params.verdict,
        page: params.page ?? 1,
        pageSize: params.pageSize ?? 100,
      },
    }));
  },
  async create(body: FeedbackCreateBody): Promise<Schemas["FeedbackItem"]> { return unwrap(await httpClient.post<Envelope<Schemas["FeedbackItem"]>>("/feedback", body)); },
};

export const agentApi = {
  async rewrite(body: AgentRewriteRequest): Promise<Schemas["RewriteData"]> { return unwrap(await httpClient.post<Envelope<Schemas["RewriteData"]>>("/agent/rewrite", body)); },
};
