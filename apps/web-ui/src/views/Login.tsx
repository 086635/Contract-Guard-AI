import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import Button from "@/components/ui/Button";
import Icon from "@/components/ui/Icon";
import { useAuthStore } from "@/stores/authStore";

type LoginLocationState = { from?: string };

export default function Login() {
  const login = useAuthStore((state) => state.login);
  const status = useAuthStore((state) => state.status);
  const storeError = useAuthStore((state) => state.error);
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  const state = location.state as LoginLocationState | null;
  const from = state?.from?.startsWith("/") ? state.from : "/contracts";

  if (status === "authenticated") return <Navigate to={from} replace />;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError(null);
    try {
      await login(username, password);
      navigate(from, { replace: true });
    } catch {
      setLocalError("登录失败，请检查用户名、密码，或稍后重试。");
    }
  };

  const busy = status === "loading";
  const error = localError ?? storeError;

  return (
    <main className="auth-page">
      <section className="auth-panel" aria-label="账号登录">
        <div className="auth-brand">
          <div className="brand-mark"><Icon name="shield" size={24} /></div>
          <div><strong>Contract<span>Guard</span></strong><small>AI LEGAL WORKSPACE</small></div>
        </div>
        <div className="auth-copy">
          <span className="eyebrow">SECURE ACCESS</span>
          <h1>登录企业法务空间</h1>
          <p>使用本地账号进入合同审核工作台。访问令牌仅保存在内存中，刷新页面会自动恢复会话。</p>
        </div>
        <form className="auth-form" onSubmit={(event) => void submit(event)}>
          <label>用户名<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoFocus required /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
          {error && <div className="auth-error" role="alert">{error}</div>}
          <Button type="submit" disabled={busy}>{busy ? "登录中…" : "登录"}</Button>
        </form>
        <small className="auth-footnote">没有账号请联系管理员创建，不会生成默认管理员账号。</small>
      </section>
    </main>
  );
}