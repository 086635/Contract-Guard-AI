import { createBrowserRouter, Navigate } from "react-router-dom";
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
      { path: "audit", element: <Navigate to="/contracts/00000000-0000-0000-0000-000000000001/audit" replace /> },
      { path: "audit/:contractId", element: <LegacyAuditRedirect /> },
      { path: "report", element: <Navigate to="/contracts/00000000-0000-0000-0000-000000000001/report" replace /> },
      { path: "report/:contractId", element: <LegacyReportRedirect /> },
      { path: "settings", element: <Navigate to="/settings/rules" replace /> },
    ],
  },
]);

function LegacyAuditRedirect() {
  const contractId = window.location.pathname.split("/").pop();
  return <Navigate to={`/contracts/${contractId}/audit`} replace />;
}

function LegacyReportRedirect() {
  const contractId = window.location.pathname.split("/").pop();
  return <Navigate to={`/contracts/${contractId}/report`} replace />;
}
