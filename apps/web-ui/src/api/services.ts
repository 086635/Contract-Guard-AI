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

const unwrap = <T>(response: { data: Envelope<T> }): T => {
  if (response.data.code !== "SUCCESS") throw new Error(`API request failed: ${response.data.code}`);
  return response.data.data;
};

/** EventSource 端点返回裸 TaskProgress（而普通 HTTP 返回统一包络）。 */
const apiBase = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");

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
  async presign(fileName: string, fileType: FileType): Promise<Schemas["PresignData"]> { return unwrap(await httpClient.post<Envelope<Schemas["PresignData"]>>("/contracts/presign", { fileName, fileType })); },
  async directUpload(presignedUrl: string, file: File): Promise<void> {
    const target = new URL(presignedUrl, window.location.origin);
    // Walking Skeleton 返回不可解析的 .local MinIO 地址；开发态保留完整三步流程，但跳过外部直传。
    if (import.meta.env.DEV && target.hostname.endsWith(".local")) return;
    await httpClient.put(presignedUrl, file, { baseURL: "", headers: { "Content-Type": file.type || "application/octet-stream" }, timeout: 120_000 });
  },
  async submit(body: UploadBody): Promise<TaskProgress> { return unwrap(await httpClient.post<Envelope<TaskProgress>>("/contracts/upload", body)); },
};

export const taskApi = {
  async get(taskId: string): Promise<TaskProgress> { return unwrap(await httpClient.get<Envelope<TaskProgress>>(`/tasks/${taskId}`)); },
  subscribe(taskId: string, onProgress: (progress: TaskProgress) => void, onError?: (event: Event) => void): () => void {
    const source = new EventSource(`${apiBase}/tasks/${taskId}/stream`);
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as TaskProgress | Envelope<TaskProgress>;
        onProgress("data" in payload ? payload.data : payload);
      } catch (error) { console.warn("[task-stream] invalid event", error); }
    };
    if (onError) source.onerror = (event) => { onError(event); source.close(); };
    return () => source.close();
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
  async create(body: FeedbackCreateBody): Promise<Schemas["FeedbackItem"]> { return unwrap(await httpClient.post<Envelope<Schemas["FeedbackItem"]>>("/feedback", body)); },
};

export const agentApi = {
  async rewrite(body: AgentRewriteRequest): Promise<Schemas["RewriteData"]> { return unwrap(await httpClient.post<Envelope<Schemas["RewriteData"]>>("/agent/rewrite", body)); },
};
