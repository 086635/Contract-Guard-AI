import { FormEvent, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import Icon, { type IconName } from "@/components/ui/Icon";
import Button from "@/components/ui/Button";
import Dialog from "@/components/ui/Dialog";
import { useAuthStore } from "@/stores/authStore";
import { useContractStore } from "@/stores/contractStore";
import { loadContractItems } from "@/views/Dashboard";
import type { ContractItem } from "@/components/contracts/ContractRow";

export type NavItem = { label: string; detail: string; to: string; icon: IconName; end?: boolean; count?: string; adminOnly?: boolean; requiresContract?: boolean };

export function buildNavItems(contractId: string | null, isAdmin: boolean): NavItem[] {
  const items: NavItem[] = [
    { label: "合同中心", detail: "Contracts & import", to: "/contracts", icon: "grid", end: true },
    { label: "审核工作台", detail: "Review workspace", to: contractId ? `/contracts/${contractId}/audit` : "/audit", icon: "file-text", end: true },
    { label: "审核报告", detail: "Reports & export", to: contractId ? `/contracts/${contractId}/report` : "/report", icon: "book", end: true },
    { label: "规则配置", detail: "Rules & agents", to: "/settings/rules", icon: "sliders", adminOnly: true },
    { label: "反馈中心", detail: "Human in the loop", to: "/feedback", icon: "activity" },
  ];
  return items.filter((item) => !item.adminOnly || isAdmin);
}

function roleLabel(role: string | undefined): string {
  if (role === "ADMIN") return "管理员";
  if (role === "REVIEWER") return "审核员";
  return "查看者";
}

export default function AppShell() {
  const location = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [location.pathname]);
  const navigate = useNavigate();
  const user = useAuthStore((state) => state.user);
  const hasRole = useAuthStore((state) => state.hasRole);
  const logout = useAuthStore((state) => state.logout);
  const isAdmin = hasRole("ADMIN");
  const [query, setQuery] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [panel, setPanel] = useState<"workspace" | "account" | "tasks" | null>(null);
  const [tasks, setTasks] = useState<ContractItem[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const rememberedId = useContractStore((state) => state.contractId);
  const isAudit = location.pathname.endsWith("/audit");
  const contractMatch = location.pathname.match(/^\/contracts\/([^/]+)\/(?:audit|report)$/);
  const contractId = contractMatch?.[1] ?? (["/audit", "/report"].includes(location.pathname) ? null : rememberedId);
  const navItems = buildNavItems(contractId, isAdmin);
  const displayName = user?.displayName || user?.username || "用户";
  const avatarText = displayName.slice(0, 1);
  const signOut = () => { useContractStore.getState().clearContract(); setPanel(null); void logout().finally(() => navigate("/login", { replace: true })); };
  const loadTasks = async () => { setTasksLoading(true); setTasksError(null); try { setTasks(await loadContractItems()); } catch (reason) { setTasksError(reason instanceof Error ? reason.message : "任务读取失败"); } finally { setTasksLoading(false); } };
  const showTasks = () => { setPanel("tasks"); void loadTasks(); };
  const submitSearch = (event: FormEvent) => { event.preventDefault(); navigate(`/contracts${query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ""}`); setSidebarOpen(false); };
  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); document.getElementById("global-contract-search")?.focus(); }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, []);

  return (
    <div className={`app-shell ${sidebarOpen ? "sidebar-is-open" : ""}`}>
      <button className="mobile-sidebar-button" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="打开导航"><Icon name="menu" size={20} /></button>
      <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />
      <aside className="sidebar">
        <div className="sidebar-glow sidebar-glow-one" /><div className="sidebar-glow sidebar-glow-two" />
        <div className="brand-lockup">
          <div className="brand-mark"><Icon name="shield" size={20} /></div>
          <div><strong>Contract<span>Guard</span></strong><small>AI LEGAL WORKSPACE</small></div>
        </div>
        <button className="workspace-switcher" onClick={() => setPanel("workspace")} aria-label="当前工作空间"><div className="workspace-avatar">{avatarText}</div><div><b>{displayName}的工作台</b><small><i /> 合同审核空间</small></div><Icon name="chevron-down" size={14} /></button>
        <button className="new-review-button" onClick={() => { navigate("/contracts?upload=1"); setSidebarOpen(false); }}><span><Icon name="plus" size={16} /></span><div><b>新建审核</b><small>上传合同开始分析</small></div><Icon name="arrow-up-right" size={15} /></button>
        <div className="nav-label"><span>工作空间</span><i /></div>
        <nav className="main-nav">
          {navItems.map((item) => <NavLink key={item.label} to={item.to} end={item.end} onClick={() => setSidebarOpen(false)} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}><span className="nav-icon"><Icon name={item.icon} size={17} /></span><span className="nav-copy"><b>{item.label}</b><small>{item.detail}</small></span>{item.count && <em>{item.count}</em>}<Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink>)}
        </nav>
        <div className="sidebar-spacer" />
        <div className="nav-label"><span>系统</span><i /></div>
        <nav className="main-nav compact-nav"><NavLink to="/feedback" className="nav-item"><span className="nav-icon"><Icon name="activity" size={17} /></span><span className="nav-copy"><b>反馈记录</b><small>人工复核与标注</small></span><Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink>{isAdmin && <NavLink to="/settings/rules" className="nav-item"><span className="nav-icon"><Icon name="settings" size={17} /></span><span className="nav-copy"><b>工作台设置</b><small>规则与偏好</small></span><Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink>}</nav>
        <div className="sidebar-footer"><div className="user-avatar"><span>{avatarText}</span><i /></div><div><b>{displayName}</b><small>{roleLabel(user?.role)}</small></div><button className="profile-more" onClick={signOut} aria-label="退出登录" title="退出登录"><Icon name="x" size={17} /></button></div>
      </aside>
      <div className="shell-main">
        <header className="topbar">
          <div className="breadcrumbs"><span>{isAudit ? "合同审核" : "法务工作台"}</span>{isAudit && <><Icon name="chevron-right" size={14} /><b>审核工作台</b></>}</div>
          <div className="topbar-actions"><form className="global-search" onSubmit={submitSearch}><Icon name="search" size={17} /><input id="global-contract-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索合同名称或编号" /><kbd>Ctrl K</kbd></form><button className="icon-button" aria-label="任务通知" onClick={showTasks}><Icon name="bell" size={18} /></button><div className="topbar-divider" /><button className="user-chip" onClick={() => setPanel("account")} aria-label="账户信息"><span className="user-avatar small">{avatarText}</span><span>{displayName}</span><Icon name="chevron-down" size={14} /></button></div>
        </header>
        <main className="page-content"><Outlet /></main>
      </div>
      {panel && <Dialog title={panel === "tasks" ? "解析任务" : panel === "account" ? "账户信息" : "当前工作空间"} onClose={() => setPanel(null)}>{panel === "tasks" ? <><p>读取可访问合同的当前解析状态。点击合同可查看原文、错误原因并重试。</p><Button variant="secondary" disabled={tasksLoading} onClick={() => void loadTasks()}>刷新任务</Button>{tasksLoading ? <p>正在读取任务…</p> : tasksError ? <p className="error-text">{tasksError}</p> : tasks.length ? <div className="task-list">{tasks.map((item) => <Link key={item.id} to={`/contracts/${item.id}/audit`} onClick={() => setPanel(null)}><strong>{item.name}</strong><span>{item.status}{item.status === "解析中" ? ` · ${Math.round((item.progress ?? 0) * 100)}%` : ""}</span>{item.errorMessage && <small className="error-text">{item.errorMessage}</small>}</Link>)}</div> : <p>尚无合同任务。<Link to="/contracts?upload=1" onClick={() => setPanel(null)}>上传合同</Link></p>}</> : <><dl className="account-details"><dt>显示名称</dt><dd>{displayName}</dd><dt>账号</dt><dd>{user?.username ?? "—"}</dd><dt>权限</dt><dd>{roleLabel(user?.role)}</dd></dl><div className="dialog-action-list"><Link to="/contracts" onClick={() => setPanel(null)}>合同中心 →</Link><Link to="/audit" onClick={() => setPanel(null)}>选择合同进入审核 →</Link><Link to="/report" onClick={() => setPanel(null)}>选择合同查看报告 →</Link>{isAdmin && <Link to="/settings/rules" onClick={() => setPanel(null)}>规则配置 →</Link>}<Button variant="secondary" onClick={signOut}>退出当前账号</Button></div></>}</Dialog>}
    </div>
  );
}
