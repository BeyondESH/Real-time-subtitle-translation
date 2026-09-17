import {
  useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent
} from 'react';
import {
  PanelLeftClose, PanelLeftOpen, Plus, Search, Settings, Radio,
  Pencil, Trash2, Download
} from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  Button, ListItem, Modal, StatusDot, cx
} from '../components/ui';
import {
  useAppConfig, useAppState, useHistoryChanged
} from '../state/hooks';
import { NO_DRAG_REGION } from './TitleBar';

const CONNECTION_LABEL: Record<
  AppStateView['connection'],
  { text: string; status: 'ok' | 'warn' | 'danger' | 'idle' }
> = {
  open: { text: '已连接', status: 'ok' },
  connecting: { text: '连接中…', status: 'idle' },
  reconnecting: { text: '重连中…', status: 'warn' },
  down: { text: '未连接', status: 'danger' }
};

const EXPORT_FORMATS: Array<{ value: 'srt' | 'txt' | 'md' | 'json'; label: string }> = [
  { value: 'srt', label: 'SRT 字幕' },
  { value: 'txt', label: 'TXT 双语' },
  { value: 'md', label: 'Markdown' },
  { value: 'json', label: 'JSON' }
];

function dayStart(ms: number): number {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function timeLabel(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 今天 / 昨天 / 更早 分组 */
function groupSessions(sessions: SessionRowView[]): Array<{ label: string; items: SessionRowView[] }> {
  const today = dayStart(Date.now());
  const groups: Array<{ label: string; items: SessionRowView[] }> = [
    { label: '今天', items: [] },
    { label: '昨天', items: [] },
    { label: '更早', items: [] }
  ];
  for (const s of sessions) {
    const day = dayStart(s.started_at);
    if (day >= today) groups[0].items.push(s);
    else if (day >= today - 86_400_000) groups[1].items.push(s);
    else groups[2].items.push(s);
  }
  return groups.filter((g) => g.items.length > 0);
}

interface MenuState {
  x: number;
  y: number;
  session: SessionRowView;
}

/** 会话侧栏（session-history spec：分组列表/右键管理/搜索入口） */
export function Sidebar() {
  const cfg = useAppConfig();
  const state = useAppState();
  const navigate = useNavigate();
  const location = useLocation();

  const [sessions, setSessions] = useState<SessionRowView[]>([]);
  const [query, setQuery] = useState('');
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionRowView | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<SessionRowView | null>(null);
  const [exportTarget, setExportTarget] = useState<SessionRowView | null>(null);
  const [feedback, setFeedback] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  const collapsed = cfg?.ui.sidebarCollapsed ?? false;
  const conn = CONNECTION_LABEL[state?.connection ?? 'connecting'];
  const inSettings = location.pathname.startsWith('/settings');
  const activeId = state?.activeSessionId ?? null;

  const reload = useCallback(() => {
    window.appAPI
      .listSessions()
      .then(setSessions)
      .catch((e: unknown) => console.error('会话列表加载失败', e));
  }, []);

  useEffect(reload, [reload]);
  useHistoryChanged(reload);

  // 右键菜单：点击别处关闭
  useEffect(() => {
    if (!menu) return;
    const close = (): void => setMenu(null);
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [menu]);

  const onItemContextMenu = (s: SessionRowView) => (e: ReactMouseEvent<HTMLElement>): void => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, session: s });
  };

  const doExport = async (format: 'srt' | 'txt' | 'md' | 'json'): Promise<void> => {
    if (!exportTarget) return;
    const r = await window.appAPI.exportSession(exportTarget.id, format);
    if (r.ok) {
      setFeedback(r.approximate
        ? `已导出（时间轴为近似值）：${r.filePath}`
        : `已导出：${r.filePath}`);
    } else if (r.error !== 'canceled') {
      setFeedback(`导出失败：${r.error}`);
    }
    setExportTarget(null);
  };

  const toggleCollapse = (): void => {
    void window.appAPI.setConfig('ui.sidebarCollapsed', !collapsed);
  };

  if (collapsed) {
    return (
      <aside
        className="flex w-12 shrink-0 flex-col items-center gap-3 border-r border-edge bg-sidebar py-3"
        style={NO_DRAG_REGION}
      >
        <button
          type="button"
          onClick={toggleCollapse}
          title="展开侧栏"
          className="rounded-control p-2 text-secondary transition-colors duration-normal ease-app hover:bg-surface-hover hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => navigate('/live')}
          title="直播字幕"
          className={cx(
            'rounded-control p-2 transition-colors duration-normal ease-app hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            !inSettings && location.pathname === '/live' ? 'text-accent' : 'text-secondary'
          )}
        >
          <Radio className="h-4 w-4" />
        </button>
        <div className="mt-auto">
          <StatusDot status={conn.status} />
        </div>
      </aside>
    );
  }

  return (
    <aside
      className="flex w-60 shrink-0 flex-col border-r border-edge bg-sidebar"
      style={NO_DRAG_REGION}
    >
      <div className="flex items-center justify-between px-3 py-3">
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus className="h-4 w-4" />}
          onClick={() => void window.appAPI.dispatch({ type: 'newSession' })}
        >
          新会话
        </Button>
        <button
          type="button"
          onClick={toggleCollapse}
          title="折叠侧栏"
          className="rounded-control p-2 text-secondary transition-colors duration-normal ease-app hover:bg-surface-hover hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      <div className="flex items-center gap-2 rounded-control border border-edge bg-elevated mx-3 mb-2 px-2">
        <Search className="h-3.5 w-3.5 shrink-0 text-secondary" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && query.trim()) {
              navigate(`/search?q=${encodeURIComponent(query.trim())}`);
            }
          }}
          placeholder="搜索历史…"
          className="h-8 w-full bg-transparent text-xs text-primary placeholder:text-secondary focus:outline-none"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {groupSessions(sessions).map((group) => (
          <div key={group.label} className="mb-2">
            <div className="px-1 pb-1 text-xs text-secondary opacity-60">{group.label}</div>
            {group.items.map((s) => (
              <ListItem
                key={s.id}
                title={s.title}
                subtitle={`${timeLabel(s.started_at)} · ${s.utterance_count} 句`}
                selected={s.id === activeId}
                onClick={() => (s.id === activeId ? navigate('/live') : navigate(`/session/${s.id}`))}
                onContextMenu={onItemContextMenu(s)}
                trailing={
                  s.id === activeId
                    ? <StatusDot status={state?.capture === 'paused' ? 'paused' : conn.status} />
                    : undefined
                }
              />
            ))}
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="px-3 py-4 text-xs text-secondary opacity-60">
            暂无会话 — 播放音频后自动开始记录
          </div>
        )}
      </div>

      <div className="border-t border-edge px-2 py-2">
        <div
          role="button"
          tabIndex={0}
          onClick={() => navigate('/settings/general')}
          onKeyDown={(e) => { if (e.key === 'Enter') navigate('/settings/general'); }}
          className={cx(
            'flex w-full cursor-pointer items-center gap-3 rounded-control px-3 py-2',
            'transition-colors duration-normal ease-app hover:bg-surface-hover',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
          )}
        >
          <Settings className={cx('h-4 w-4', inSettings ? 'text-accent' : 'text-secondary')} />
          <span className={cx('text-base', inSettings ? 'text-primary' : 'text-secondary')}>设置</span>
          <span className="ml-auto">
            <StatusDot status={conn.status} />
          </span>
        </div>
      </div>

      {/* 右键菜单 */}
      {menu && (
        <div
          ref={menuRef}
          className="panel-enter fixed z-50 w-36 rounded-control border border-edge bg-sidebar p-1"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-xs text-primary transition-colors duration-fast ease-app hover:bg-surface-hover"
            onClick={() => {
              setRenameTarget(menu.session);
              setRenameValue(menu.session.title);
              setMenu(null);
            }}
          >
            <Pencil className="h-3.5 w-3.5 text-secondary" /> 重命名
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-xs text-primary transition-colors duration-fast ease-app hover:bg-surface-hover"
            onClick={() => {
              setExportTarget(menu.session);
              setMenu(null);
            }}
          >
            <Download className="h-3.5 w-3.5 text-secondary" /> 导出…
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-xs text-danger transition-colors duration-fast ease-app hover:bg-surface-hover"
            onClick={() => {
              setDeleteTarget(menu.session);
              setMenu(null);
            }}
          >
            <Trash2 className="h-3.5 w-3.5" /> 删除
          </button>
        </div>
      )}

      {/* 重命名 */}
      <Modal
        open={renameTarget !== null}
        onClose={() => setRenameTarget(null)}
        title="重命名会话"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setRenameTarget(null)}>取消</Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                if (renameTarget && renameValue.trim()) {
                  void window.appAPI
                    .renameSession(renameTarget.id, renameValue.trim())
                    .then(reload);
                }
                setRenameTarget(null);
              }}
            >
              确定
            </Button>
          </>
        )}
      >
        <input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          maxLength={60}
          className="h-9 w-full rounded-control border border-edge bg-elevated px-3 text-base text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </Modal>

      {/* 删除确认 */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="删除会话"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                if (deleteTarget) {
                  void window.appAPI.deleteSession(deleteTarget.id).then(reload);
                }
                setDeleteTarget(null);
              }}
            >
              删除
            </Button>
          </>
        )}
      >
        将同时删除「{deleteTarget?.title}」的全部 {deleteTarget?.utterance_count ?? 0} 条语句，且不可恢复。
      </Modal>

      {/* 导出格式选择 */}
      <Modal
        open={exportTarget !== null}
        onClose={() => setExportTarget(null)}
        title={`导出「${exportTarget?.title ?? ''}」`}
      >
        <div className="flex flex-col gap-2">
          {EXPORT_FORMATS.map((f) => (
            <Button key={f.value} size="sm" onClick={() => void doExport(f.value)}>
              {f.label}
            </Button>
          ))}
        </div>
      </Modal>

      {/* 操作反馈 */}
      {feedback && (
        <div className="panel-enter border-t border-edge px-3 py-2 text-xs text-secondary">
          {feedback}
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => setFeedback('')}
          >
            关闭
          </button>
        </div>
      )}
    </aside>
  );
}
