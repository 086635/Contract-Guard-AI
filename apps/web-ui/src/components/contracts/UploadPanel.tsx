import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Icon from "@/components/ui/Icon";
import Button from "@/components/ui/Button";
import { taskApi, uploadApi } from "@/api/services";
import type { components } from "@/api/generated/types";

type TaskProgress = components["schemas"]["TaskProgress"];
type UploadPanelProps = { onClose: () => void; onCreated?: (task: TaskProgress) => void };

const fileTypeOf = (file: File): components["schemas"]["FileType"] => file.name.toLowerCase().endsWith(".docx") ? "DOCX" : "PDF_NATIVE";

export default function UploadPanel({ onClose, onCreated }: UploadPanelProps) {
  const navigate = useNavigate();
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape" && !submitting) onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => { window.removeEventListener("keydown", onKeyDown); unsubscribeRef.current?.(); };
  }, [onClose, submitting]);

  const chooseFile = (candidate?: File) => {
    if (!candidate) return;
    setError(null);
    const valid = /\.(pdf|docx)$/i.test(candidate.name) && candidate.size <= 50 * 1024 * 1024;
    if (!valid) {
      setFile(null);
      setError(candidate.size > 50 * 1024 * 1024 ? "文件超过 50 MB，请压缩后重试。" : "仅支持 PDF 或 DOCX 文件。");
      return;
    }
    setFile(candidate); setDone(false); setProgress(null);
  };

  const submit = async () => {
    if (!file || submitting) return;
    setSubmitting(true); setError(null);
    try {
      const fileType = fileTypeOf(file);
      const presign = await uploadApi.presign(file.name, fileType);
      await uploadApi.directUpload(presign.presignedUrl, file);
      const task = await uploadApi.submit({ fileKey: presign.fileKey, fileName: file.name, fileType });
      setProgress(task); onCreated?.(task); setDone(task.status === "DONE");
      unsubscribeRef.current = taskApi.subscribe(task.taskId, (next) => {
        setProgress(next);
        if (next.status === "DONE") { setDone(true); setSubmitting(false); unsubscribeRef.current?.(); unsubscribeRef.current = null; }
        if (next.status === "FAILED") { setError("合同解析失败，请检查文件内容后重试。"); setSubmitting(false); unsubscribeRef.current?.(); unsubscribeRef.current = null; }
      }, () => { void taskApi.get(task.taskId).then(setProgress).catch(() => undefined); });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传失败，请稍后重试。"); setSubmitting(false);
    }
  };

  const openAudit = () => { onClose(); navigate(`/contracts/${progress?.result && "contractId" in progress.result ? progress.result.contractId : "00000000-0000-0000-0000-000000000001"}/audit`); };
  return <div className="modal-backdrop" onMouseDown={onClose}><div className="modal upload-modal" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><div className="eyebrow">NEW REVIEW</div><h2>{done ? "解析任务已完成" : submitting ? "正在上传并解析" : "上传新合同"}</h2><p>{done ? "合同已完成解析，可以进入审核工作台查看结果。" : "支持 PDF、DOCX，单文件不超过 50 MB"}</p></div><button className="close-button" onClick={onClose} aria-label="关闭"><Icon name="x" size={20} /></button></div><div className={`drop-zone ${dragging ? "dragging" : ""} ${done ? "upload-done" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files[0]); }} onClick={() => inputRef.current?.click()}><input ref={inputRef} type="file" accept=".pdf,.docx" hidden onChange={(event) => chooseFile(event.target.files?.[0])} /><div className="upload-circle"><Icon name={done ? "check" : submitting ? "clock" : "upload"} size={23} /></div>{done ? <><b>已完成解析</b><span>{file?.name}</span></> : file ? <><b>{file.name}</b><span>{submitting ? `解析进度 ${Math.round((progress?.percent ?? 0) * 100)}%` : "文件已就绪，点击可重新选择"}</span></> : <><b>拖拽文件至此处</b><span>或点击选择本地文件</span></>}<small>PDF / DOCX</small></div>{submitting && <div className="upload-progress"><span><i style={{ width: `${Math.max(4, (progress?.percent ?? 0) * 100)}%` }} /></span><b>{Math.round((progress?.percent ?? 0) * 100)}%</b><small>{progress?.stage ?? "准备上传"}</small></div>}{error && <div className="upload-error"><Icon name="activity" size={15} /><span>{error}</span></div>}<div className="upload-note"><Icon name="shield" size={16} /><span>文件将通过加密通道传输，仅用于本次合同审核</span></div><div className="modal-actions">{done ? <Button onClick={openAudit} icon={<Icon name="arrow-up-right" size={16} />}>进入审核工作台</Button> : <><Button variant="secondary" onClick={onClose}>取消</Button><Button disabled={!file || submitting} onClick={() => void submit()} icon={<Icon name={submitting ? "clock" : "arrow-up-right"} size={16} />}>{submitting ? "处理中…" : "开始解析"}</Button></>}</div></div></div>;
}
