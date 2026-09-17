import { useEffect } from 'react';
import { HashRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { applyTheme } from './theme';
import { useAppConfig, useNavigateChannel } from './state/hooks';
import { AppShell } from './app/AppShell';
import { LivePage } from './app/LivePage';
import { SessionPage } from './app/SessionPage';
import { SearchPage } from './app/SearchPage';
import { SettingsPage } from './app/SettingsPage';
import { OnboardingPage } from './app/OnboardingPage';
import { StyleGuidePage } from './app/StyleGuidePage';
import './styles/app.css';

/** 托盘深链 → 路由跳转（须在 Router 上下文内） */
function DeepLinkBridge() {
  const navigate = useNavigate();
  useNavigateChannel(navigate);
  return null;
}

/** 会话回放页（session-history spec） */
function SessionRoute() {
  return <SessionPage />;
}

function Boot() {
  const cfg = useAppConfig();

  // 主题应用（config → data-theme；system 模式跟随系统实时切换）
  useEffect(() => {
    if (cfg) applyTheme(cfg.theme);
  }, [cfg]);

  if (!cfg) {
    return (
      <div className="flex h-screen items-center justify-center bg-base text-secondary">
        加载中…
      </div>
    );
  }

  const initial = cfg.onboarding.completed ? '/live' : '/onboarding';

  return (
    <HashRouter>
      <DeepLinkBridge />
      <Routes>
        <Route path="/" element={<Navigate to={initial} replace />} />
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route element={<AppShell />}>
          <Route path="/live" element={<LivePage />} />
          <Route path="/session/:id" element={<SessionRoute />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/settings" element={<Navigate to="/settings/general" replace />} />
          <Route path="/settings/:section" element={<SettingsPage />} />
          <Route path="/styleguide" element={<StyleGuidePage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </HashRouter>
  );
}

export default function App() {
  return <Boot />;
}
