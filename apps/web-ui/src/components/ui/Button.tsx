import type { ButtonHTMLAttributes, ReactNode } from "react";

export default function Button({ children, variant = "primary", size = "md", icon, ...props }: { children: ReactNode; variant?: "primary" | "secondary" | "ghost" | "danger"; size?: "sm" | "md"; icon?: ReactNode } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`button button-${variant} button-${size}`} {...props}>{icon}{children}</button>;
}
