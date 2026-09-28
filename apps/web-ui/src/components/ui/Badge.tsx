import type { ReactNode } from "react";

export type BadgeTone = "neutral" | "high" | "medium" | "low" | "success" | "purple" | "info";

export default function Badge({ children, tone = "neutral", dot = false }: { children: ReactNode; tone?: BadgeTone; dot?: boolean }) {
  return <span className={`badge badge-${tone}`}>{dot && <i className="badge-dot" />}{children}</span>;
}
