import { NavLink, Outlet } from "react-router-dom";

/** 应用骨架：左侧导航 + 右侧路由出口（§5.1 五大核心页面）。 */
export default function App() {
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    isActive ? "nav-link nav-link--active" : "nav-link";

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: "system-ui" }}>
      <aside style={{ width: 200, borderRight: "1px solid #eee", padding: 16 }}>
        <h3>合同审核</h3>
        <nav style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <NavLink to="/" end className={linkClass}>工作台</NavLink>
          <NavLink to="/audit" className={linkClass}>审核</NavLink>
          <NavLink to="/report" className={linkClass}>报告</NavLink>
          <NavLink to="/settings" className={linkClass}>配置</NavLink>
          <NavLink to="/feedback" className={linkClass}>反馈</NavLink>
        </nav>
      </aside>
      <main style={{ flex: 1, padding: 24 }}>
        <Outlet />
      </main>
    </div>
  );
}
