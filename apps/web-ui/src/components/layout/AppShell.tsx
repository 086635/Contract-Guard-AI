import { FormEvent, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import Icon, { type IconName } from "@/components/ui/Icon";

type NavItem = { label: string; detail: string; to: string; icon: IconName; end?: boolean; count?: string };

export default function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const isAudit = location.pathname.endsWith("/audit");
  const contractMatch = location.pathname.match(/^\/contracts\/([^/]+)\/(?:audit|report)$/);
  const contractId = contractMatch?.[1] ?? null;
  const navItems: NavItem[] = [
    { label: "合同中心", detail: "Contracts & import", to: "/contracts", icon: "grid", end: true },
    { label: "审核工作台", detail: "Review workspace", to: contractId ? `/contracts/${contractId}/audit` : "/contracts", icon: "file-text", end: !contractId },
    { label: "审核报告", detail: "Reports & export", to: contractId ? `/contracts/${contractId}/report` : "/contracts", icon: "book", end: !contractId },
    { label: "规则配置", detail: "Rules & agents", to: "/settings/rules", icon: "sliders" },
    { label: "反馈中心", detail: "Human in the loop", to: "/feedback", icon: "activity" },
  ];
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
        <button className="workspace-switcher"><div className="workspace-avatar">杭</div><div><b>杭州宏图科技</b><small><i /> 企业法务空间</small></div><Icon name="chevron-down" size={14} /></button>
        <button className="new-review-button" onClick={() => { navigate("/contracts?upload=1"); setSidebarOpen(false); }}><span><Icon name="plus" size={16} /></span><div><b>新建审核</b><small>上传合同开始分析</small></div><Icon name="arrow-up-right" size={15} /></button>
        <div className="nav-label"><span>工作空间</span><i /></div>
        <nav className="main-nav">
          {navItems.map((item) => <NavLink key={item.to} to={item.to} end={item.end} onClick={() => setSidebarOpen(false)} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}><span className="nav-icon"><Icon name={item.icon} size={17} /></span><span className="nav-copy"><b>{item.label}</b><small>{item.detail}</small></span>{item.count && <em>{item.count}</em>}<Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink>)}
        </nav>
        <div className="sidebar-spacer" />
        <div className="nav-label"><span>系统</span><i /></div>
        <nav className="main-nav compact-nav"><NavLink to="/feedback" className="nav-item"><span className="nav-icon"><Icon name="activity" size={17} /></span><span className="nav-copy"><b>反馈记录</b><small>人工复核与标注</small></span><Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink><NavLink to="/settings/rules" className="nav-item"><span className="nav-icon"><Icon name="settings" size={17} /></span><span className="nav-copy"><b>工作台设置</b><small>规则与偏好</small></span><Icon className="nav-arrow" name="chevron-right" size={13} /></NavLink></nav>
        <div className="sidebar-footer"><div className="user-avatar"><span>王</span><i /></div><div><b>王钘宇</b><small>高级法务专员</small></div><button className="profile-more"><Icon name="more" size={17} /></button></div>
      </aside>
      <div className="shell-main">
        <header className="topbar">
          <div className="breadcrumbs"><span>{isAudit ? "合同审核" : "法务工作台"}</span>{isAudit && <><Icon name="chevron-right" size={14} /><b>审核工作台</b></>}</div>
          <div className="topbar-actions"><form className="global-search" onSubmit={submitSearch}><Icon name="search" size={17} /><input id="global-contract-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索合同、条款或编号" /><kbd>⌘ K</kbd></form><button className="icon-button" aria-label="任务通知"><Icon name="bell" size={18} /><i /></button><div className="topbar-divider" /><button className="user-chip"><span className="user-avatar small">王</span><span>王钘宇</span><Icon name="chevron-down" size={14} /></button></div>
        </header>
        <main className="page-content"><Outlet /></main>
      </div>
    </div>
  );
}
