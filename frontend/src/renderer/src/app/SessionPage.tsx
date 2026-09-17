import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, Download } from 'lucide-react';
import { Button, EmptyState, Modal } from '../components/ui';
import { langPairLabel } from './lang-labels';
import { ToastHost } from './ToastHost';

const EXPORT_FORMATS: Array<{ value: 'srt' | 'txt' | 'md' | 'json'; label: string }> = [
  { value: 'srt', label: 'SRT 字幕' },
  { value: 'txt', label: 'TXT 双语' },
  { value: 'md', label: 'Markdown' },
  { value: 'json', label: 'JSON' }
];

function clockLabel(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function dateTimeLabel(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 会话回放页（session-history spec：完整回放 + 定位高亮 + 导出） */
export function SessionPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const focusId = searchParams.get('focus');

  const [detail, setDetail] = useState<SessionDetailView | null>(null);
  const [loadError, setLoadError] = useState('');
  const [exportOpen, setExportOpen] = useState(false);
  const [feedback, setFeedback] = useState('');

  const load = useCallback(() => {
    if (!id) return;
    window.appAPI
      .getSession(id)
      .then((d) => {
        if (d) {
          setDetail(d);
          setLoadError('');
        } else {
          setLoadError('会话不存在（可能已被删除）');
        }
      })
      .catch((e: unknown) => setLoadError(String(e)));
  }, [id]);

  useEffect(load, [load]);

  // focus 定位：渲染完成后滚动到目标语句
  useEffect(() => {
    if (!focusId || !detail) return;
    const el = document.getElementById(`utt-${focusId}`);
    if (el) {
      el.scrollIntoView({ block: 'center' });
    }
  }, [focusId, detail]);

  const doExport = async (format: 'srt' | 'txt' | 'md' | 'json'): Promise<void> => {
    if (!id) return;
    const r = await window.appAPI.exportSession(id, format);
    setExportOpen(false);
    if (r.ok) {
      setFeedback(r.approximate
        ? `已导出（时间轴为近似值）：${r.filePath}`
        : `已导出：${r.filePath}`);
    } else if (r.error !== 'canceled') {
      setFeedback(`导出失败：${r.error}`);
    }
  };

  if (loadError) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState
          title="无法打开会话"
          description={loadError}
          action={<Button size="sm" onClick={() => navigate('/live')}>回到直播</Button>}
        />
      </div>
    );
  }

  if (!detail) {
    return <div className="flex h-full items-center justify-center text-secondary">加载中…</div>;
  }

  const { session, utterances } = detail;

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center gap-3 border-b border-edge px-6">
        <button
          type="button"
          onClick={() => navigate(-1)}
          title="返回"
          className="rounded-control p-1.5 text-secondary transition-colors duration-normal ease-app hover:bg-surface-hover hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold text-primary">{session.title}</h1>
          <p className="text-xs text-secondary opacity-70">
            {dateTimeLabel(session.started_at)}
            {session.ended_at ? ` → ${clockLabel(session.ended_at)}` : ' → 进行中'}
            {' · '}
            {utterances.length} 句
            {session.audio_source ? ` · ${session.audio_source}` : ''}
          </p>
        </div>
        <div className="ml-auto">
          <Button
            size="sm"
            icon={<Download className="h-3.5 w-3.5" />}
            onClick={() => setExportOpen(true)}
            disabled={utterances.length === 0}
          >
            导出
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
        {utterances.length === 0 && (
          <EmptyState title="该会话没有语句记录" description="可能是刚创建就结束的会话。" />
        )}
        {utterances.map((u) => {
          const focused = focusId !== null && String(u.id) === focusId;
          const showOriginal = Boolean(u.original) && u.original !== u.translation;
          return (
            <div
              key={u.id}
              id={`utt-${u.id}`}
              className={
                focused
                  ? 'mb-5 rounded-card border border-accent bg-sidebar p-3 text-center'
                  : 'mb-5 rounded-card border border-transparent p-3 text-center'
              }
            >
              <p className="text-lg leading-relaxed text-primary">{u.translation}</p>
              {showOriginal && (
                <p className="mt-1 text-sm leading-relaxed text-secondary">{u.original}</p>
              )}
              <p className="mt-1 text-xs text-secondary opacity-60">
                {clockLabel(u.received_at)}
                {u.source_lang && u.target_lang ? ` · ${langPairLabel(u.source_lang, u.target_lang)}` : ''}
                {u.ts_start !== null && u.ts_end !== null
                  ? ` · ${(u.ts_end - u.ts_start).toFixed(1)}s`
                  : ''}
              </p>
            </div>
          );
        })}
      </div>

      {feedback && (
        <div className="shrink-0 border-t border-edge px-6 py-2 text-xs text-secondary">
          {feedback}
          <button type="button" className="ml-2 underline" onClick={() => setFeedback('')}>
            关闭
          </button>
        </div>
      )}

      <Modal open={exportOpen} onClose={() => setExportOpen(false)} title={`导出「${session.title}」`}>
        <div className="flex flex-col gap-2">
          {EXPORT_FORMATS.map((f) => (
            <Button key={f.value} size="sm" onClick={() => void doExport(f.value)}>
              {f.label}
            </Button>
          ))}
        </div>
      </Modal>

      <ToastHost />
    </div>
  );
}
