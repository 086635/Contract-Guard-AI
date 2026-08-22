import { createBrowserRouter } from "react-router-dom";
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
      { index: true, element: <Dashboard /> },
      { path: "audit", element: <Audit /> },
      { path: "audit/:contractId", element: <Audit /> },
      { path: "report", element: <Report /> },
      { path: "settings", element: <Settings /> },
      { path: "feedback", element: <Feedback /> },
    ],
  },
]);
