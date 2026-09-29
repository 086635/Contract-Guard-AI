import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Icon from "@/components/ui/Icon";
import Button from "@/components/ui/Button";
import { taskApi, uploadApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type TaskProgress = components["schemas"]["TaskProgress"];
type FileType = components["schemas"]["FileType"];
type UploadPanelProps = { onClose: () => void; onCreated?: (task: TaskProgress) => void };
type UploadState = "idle" | "uploading" | "parsing" | "done" | "failed";

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const fileTypeOf = (file: File): FileType => file.name.toLowerCase().endsWith(".docx") ? "DOCX" : "PDF_NATIVE";

const contractIdOf = (progress: TaskProgress | null): string | null => {
  const result = progress?.result as Record<string, unknown> | null | undefined;
  const contractId = result?.contractId;
  return typeof contractId === "string" && UUID_RE.test(contractId) ? contractId : null;
};

const failureMessageOf = (progress: TaskProgress | null): string => {
  const result = progress?.result as Record<string, unknown> | null | undefined;
  const message = result?.errorMessage;
  return typeof message === "string" && message.trim() ? message : "合同解析失败，请检查文件内容后重试。";
};

export default function UploadPanel({ onClose, onCreated }: UploadPanelProps) {
  const navigate = useNavigate();
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [state, setState] = useState<UploadState>("idle");
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const activeRef = useRef(true);
  const busyRef = useRef(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const clearSubscription = () => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  };

  const close = () => {
    activeRef.current = false;
    busyRef.current = false;
    clearSubscription();
    onCloseRef.current();
  };

  const closeRef = useRef(close);
  closeRef.current = close;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      activeRef.current = false;
      window.removeEventListener("keydown", onKeyDown);
      clearSubscription();
    };
  }, []);

  const chooseFile = (candidate?: File) => {
    if (!candidate) return;
    clearSubscription();
    busyRef.current = false;
    setError(null);
    setProgress(null);
    setState("idle");
    const valid = /\.(pdf|docx)$/i.test(candidate.name) && candidate.size <= MAX_FILE_SIZE;
    if (!valid) {
      setFile(null);
      setError(candidate.size > MAX_FILE_SIZE ? "文件超过 50 MB，请压缩后重试。" : "仅支持 PDF 或 DOCX 文件。");
      return;
    }
    setFile(candidate);
  };

  const submit = async () => {
    if (!file || busyRef.current) return;
    busyRef.current = true;
    clearSubscription();
    setError(null);
    setProgress(null);
    setState("uploading");
    try {
      const fileType = fileTypeOf(file);
      const presign = await uploadApi.presign(file.name, fileType);
      if (!activeRef.current) return;
      await uploadApi.directUpload(presign.presignedUrl, file);
      if (!activeRef.current) return;
      const task = await uploadApi.submit({ fileKey: presign.fileKey, fileName: file.name, fileType });
      if (!activeRef.current) return;
      setProgress(task);
      onCreated?.(task);

      if (task.status === "DONE") {
        setState("done");
        busyRef.current = false;
        return;
      }
      if (task.status === "FAILED") {
        setError(failureMessageOf(task));
        setState("failed");
        busyRef.current = false;
        return;
      }

      setState("parsing");
      unsubscribeRef.current = taskApi.subscribe(task.taskId, (next) => {
        if (!activeRef.current) return;
        setProgress(next);
        if (next.status === "DONE") {
          setState("done");
          busyRef.current = false;
          clearSubscription();
        } else if (next.status === "FAILED") {
          setError(failureMessageOf(next));
          setState("failed");
          busyRef.current = false;
          clearSubscription();
        }
      });
    } catch (reason) {
      if (!activeRef.current) return;
      setError(reason instanceof Error ? reason.message : "上传失败，请稍后重试。");
      setState("failed");
      busyRef.current = false;
      clearSubscription();
    }
  };

  const openAudit = () => {
    const contractId = contractIdOf(progress);
    if (!contractId) {
      setError("服务未返回可用的合同 ID，无法进入审核。");
      setState("failed");
      return;
    }
    close();
    navigate(`/contracts/${contractId}/audit`);
  };

  const submitting = state === "uploading" || state === "parsing";
  const done = state === "done";
  const canEnterAudit = done && contractIdOf(progress) !== null;

  return (
    <div className="modal-backdrop" onMouseDown={close}>
      <div className="modal upload-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-heading">
          <div>
            <div className="eyebrow">NEW REVIEW</div>
            <h2>{done ? "解析任务已完成" : submitting ? "正在上传并解析" : "上传新合同"}</h2>
            <p>{done ? "合同已完成解析，可以进入审核工作台查看结果。" : "支持 PDF、DOCX，单文件不超过 50 MB"}</p>
          </div>
          <button className="close-button" onClick={close} aria-label="关闭"><Icon name="x" size={20} /></button>
        </div>
        <div
          className={`drop-zone ${dragging ? "dragging" : ""} ${done ? "upload-done" : ""}`}
          onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files[0]); }}
          onClick={() => inputRef.current?.click()}
        >
          <input ref={inputRef} type="file" accept=".pdf,.docx" hidden onChange={(event) => chooseFile(event.target.files?.[0])} />
          <div className="upload-circle"><Icon name={done ? "check" : submitting ? "clock" : "upload"} size={23} /></div>
          {done ? <><b>已完成解析</b><span>{file?.name}</span></> : file ? <><b>{file.name}</b><span>{submitting ? `解析进度 ${Math.round((progress?.percent ?? 0) * 100)}%` : "文件已就绪，点击可重新选择"}</span></> : <><b>拖拽文件至此处</b><span>或点击选择本地文件</span></>}
          <small>PDF / DOCX</small>
        </div>
        {submitting && <div className="upload-progress"><span><i style={{ width: `${Math.max(4, (progress?.percent ?? 0) * 100)}%` }} /></span><b>{Math.round((progress?.percent ?? 0) * 100)}%</b><small>{progress?.stage ?? (state === "uploading" ? "准备上传" : "准备解析")}</small></div>}
        {error && <div className="upload-error"><Icon name="activity" size={15} /><span>{error}</span></div>}
        <div className="upload-note"><Icon name="shield" size={16} /><span>文件将通过加密通道传输，仅用于本次合同审核</span></div>
        <div className="modal-actions">
          {done ? <Button disabled={!canEnterAudit} onClick={openAudit} icon={<Icon name="arrow-up-right" size={16} />}>进入审核工作台</Button> : <><Button variant="secondary" onClick={close}>取消</Button><Button disabled={!file || submitting} onClick={() => void submit()} icon={<Icon name={submitting ? "clock" : "arrow-up-right"} size={16} />}>{submitting ? "处理中…" : state === "failed" ? "重试上传" : "开始解析"}</Button></>}
        </div>
      </div>
    </div>
  );
}