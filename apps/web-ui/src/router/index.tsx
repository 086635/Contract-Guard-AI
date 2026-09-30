import { useEffect } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation, useParams } from "react-router-dom";
import App from "@/App";
import Dashboard from "@/views/Dashboard";
import Audit from "@/views/Audit";
import Report from "@/views/Report";
import Settings from "@/views/Settings";
import Feedback from "@/views/Feedback";
import Login from "@/views/Login";
import { useAuthStore } from "@/stores/authStore";

/** 路由定义（§5.1 五大核心页面 + 审核详情）。 */
function AuthGuard() {
  const status = useAuthStore((state) => state.status);
  const bootstrap = useAuthStore((state) => state.bootstrap);
  const location = useLocation();

  useEffect(() => {
    if (status === "idle") void bootstrap();
  }, [bootstrap, status]);

  if (status === "idle" || status === "loading") {
    return <div className="auth-loading">正在恢复登录状态…</div>;
  }
  if (status !== "authenticated") {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
  }
  return <Outlet />;
}

function AdminGuard() {
  const hasRole = useAuthStore((state) => state.hasRole);
  if (!hasRole("ADMIN")) return <Navigate to="/contracts" replace />;
  return <Outlet />;
}

export const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    element: <AuthGuard />,
    children: [
      {
        path: "/",
        element: <App />,
        children: [
      { index: true, element: <Navigate to="/contracts" replace /> },
      { path: "contracts", element: <Dashboard /> },
      { path: "contracts/:contractId/audit", element: <Audit /> },
      { path: "contracts/:contractId/report", element: <Report /> },
      { element: <AdminGuard />, children: [{ path: "settings/rules", element: <Settings /> }] },
      { path: "feedback", element: <Feedback /> },
      { path: "audit", element: <Navigate to="/contracts" replace /> },
      { path: "audit/:contractId", element: <LegacyAuditRedirect /> },
      { path: "report", element: <Navigate to="/contracts" replace /> },
      { path: "report/:contractId", element: <LegacyReportRedirect /> },
      { path: "settings", element: <Navigate to="/settings/rules" replace /> },
        ],
      },
    ],
  },
]);

function LegacyAuditRedirect() {
  const { contractId } = useParams<{ contractId: string }>();
  return <Navigate to={contractId ? `/contracts/${contractId}/audit` : "/contracts"} replace />;
}

function LegacyReportRedirect() {
  const { contractId } = useParams<{ contractId: string }>();
  return <Navigate to={contractId ? `/contracts/${contractId}/report` : "/contracts"} replace />;
}
