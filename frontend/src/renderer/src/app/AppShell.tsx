import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { TitleBar } from './TitleBar';

/** 应用壳：自定义标题栏 + 可折叠侧栏 + 内容路由区 */
export function AppShell() {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-base">
      <TitleBar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
