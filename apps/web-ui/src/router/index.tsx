import { createBrowserRouter, Navigate, useParams } from "react-router-dom";
import App from "@/App";
import Dashboard from "@/views/Dashboard";
import Audit from "@/views/Audit";
import Report from "@/views/Report";
import Settings from "@/views/Settings";
import Feedback from "@/views/Feedback";

/** 路由定义（§5.1 五大核心页面 + 审核详情）。 */
export const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/contracts" replace /> },
      { path: "contracts", element: <Dashboard /> },
      { path: "contracts/:contractId/audit", element: <Audit /> },
      { path: "contracts/:contractId/report", element: <Report /> },
      { path: "settings/rules", element: <Settings /> },
      { path: "feedback", element: <Feedback /> },
      { path: "audit", element: <Navigate to="/contracts" replace /> },
      { path: "audit/:contractId", element: <LegacyAuditRedirect /> },
      { path: "report", element: <Navigate to="/contracts" replace /> },
      { path: "report/:contractId", element: <LegacyReportRedirect /> },
      { path: "settings", element: <Navigate to="/settings/rules" replace /> },
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
