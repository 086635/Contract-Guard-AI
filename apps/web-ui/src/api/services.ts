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

export const taskApi = {
  async get(taskId: string): Promise<TaskProgress> { return unwrap(await httpClient.get<Envelope<TaskProgress>>(`/tasks/${taskId}`)); },
  subscribe(taskId: string, onProgress: (progress: TaskProgress) => void, onError?: (event: Event) => void): () => void {
    const terminalStatuses = new Set<TaskProgress["status"]>(["DONE", "FAILED"]);
    const parseProgress = (raw: string): TaskProgress | null => {
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
    };

    let stopped = false;
    let polling = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const source = new EventSource(`${apiBase}/tasks/${taskId}/stream`);

    const clearPollTimer = () => {
      if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };
    const finish = () => {
      if (stopped) return;
      stopped = true;
      clearPollTimer();
      source.close();
    };
    const emit = (progress: TaskProgress) => {
      if (stopped) return;
      onProgress(progress);
      if (terminalStatuses.has(progress.status)) finish();
    };
    const poll = async () => {
      if (stopped || polling) return;
      polling = true;
      try {
        const progress = await taskApi.get(taskId);
        if (!stopped) emit(progress);
      } catch (error) {
        if (!stopped) console.warn("[task-poll] request failed", error);
      } finally {
        polling = false;
      }
    };
    const startPolling = () => {
      if (stopped || pollTimer !== null) return;
      pollTimer = setInterval(() => { void poll(); }, 2_000);
    };
    const handleProgress = (event: MessageEvent<string>) => {
      const progress = parseProgress(event.data);
      if (!progress) {
        console.warn("[task-stream] invalid progress event", event.data);
        return;
      }
      emit(progress);
    };

    source.addEventListener("progress", handleProgress as EventListener);
    source.onerror = (event) => {
      if (stopped) return;
      try {
        onError?.(event);
      } finally {
        source.close();
        startPolling();
      }
    };

    return finish;
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
