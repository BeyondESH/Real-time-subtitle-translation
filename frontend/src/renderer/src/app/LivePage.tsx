import { useEffect, useRef, useState } from 'react';
import { ArrowDown, Pause, Play, Lock, Unlock, Captions } from 'lucide-react';
import { Button, EmptyState, Pill, ProgressBar, StatusDot, cx } from '../components/ui';
import { useAppConfig, useAppState, useSubtitleStream, type CaptionEntry } from '../state/hooks';
import { langPairLabel } from './lang-labels';
import { StatusPillBar } from './StatusPillBar';
import { ToastHost } from './ToastHost';

function formatTime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function CaptionCard({
  entry, displayMode
}: {
  entry: CaptionEntry;
  displayMode: string;
}) {
  const m = entry.msg;
  const active = m.active_language;
  const translation = (active && m.translations && m.translations[active]) || m.original;
  const showOriginal = displayMode === 'original_and_translation'
    && Boolean(m.original)
    && m.source_language !== active;

  return (
    <div className="caption-enter mb-5 flex flex-col items-center gap-1 text-center">
      <p className="max-w-3xl text-xl leading-relaxed text-primary">{translation}</p>
      {showOriginal && (
        <p className="max-w-3xl text-sm leading-relaxed text-secondary">{m.original}</p>
      )}
      <p className="text-xs text-secondary opacity-60">
        {formatTime(entry.receivedAt)}
        {' · '}
        {langPairLabel(m.source_language, active)}
      </p>
    </div>
  );
}

/** 直播字幕流（main-window spec：消息流 + 自动滚动 + 占位状态 + 聆听指示） */
export function LivePage() {
  const state = useAppState();
  const cfg = useAppConfig();
  const entries = useSubtitleStream(state?.activeSessionId ?? null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);

  const displayMode = cfg?.subtitle.displayMode ?? 'original_and_translation';

  useEffect(() => {
    if (atBottom && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries.length, atBottom]);

  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  const scrollToLatest = (): void => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setAtBottom(true);
  };

  const connection = state?.connection ?? 'connecting';
  const paused = state?.capture === 'paused';
  const listening = state?.vad === 'speech';

  return (
    <div className="relative flex h-full flex-col">
      {/* 顶部操作行 */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-edge px-6">
        <h1 className="text-base font-semibold text-primary">直播字幕</h1>
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant={paused ? 'primary' : 'ghost'}
            icon={paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            onClick={() => void window.appAPI.dispatch({ type: 'togglePause' })}
          >
            {paused ? '恢复' : '暂停'}
          </Button>
          <Pill
            active={!state?.locked}
            icon={state?.locked ? <Lock className="h-3.5 w-3.5" /> : <Unlock className="h-3.5 w-3.5" />}
            title={state?.locked ? '字幕窗已锁定（点击解锁后可拖拽）' : '字幕窗已解锁（点击锁定）'}
            onClick={() => void window.appAPI.dispatch({ type: 'toggleLock' })}
          >
            {state?.locked ? '已锁定' : '已解锁'}
          </Pill>
        </div>
      </div>

      {/* 暂停提示条 */}
      {paused && (
        <div className="shrink-0 bg-sidebar px-6 py-1.5 text-center text-xs text-secondary">
          已暂停 — 快捷键 Ctrl+Shift+Space 或托盘菜单可恢复
        </div>
      )}

      {/* 字幕流 */}
      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
        {connection !== 'open' && (
          <EmptyState
            icon={<Captions className="h-8 w-8" />}
            title={connection === 'down' ? '后端未连接' : connection === 'reconnecting' ? '正在重连后端…' : '正在连接后端…'}
            description="字幕服务启动或恢复后，这里会自动开始接收字幕。"
          />
        )}

        {connection === 'open' && state?.modelDownload && (
          <div className="mx-auto max-w-md py-10">
            <ProgressBar
              value={state.modelDownload.progress}
              label={`模型加载中：${state.modelDownload.name}`}
            />
            <p className="mt-2 text-center text-xs text-secondary">{state.modelDownload.message}</p>
          </div>
        )}

        {connection === 'open' && !state?.modelDownload && entries.length === 0 && (
          <EmptyState
            icon={<Captions className="h-8 w-8" />}
            title="暂无字幕"
            description="播放任意包含语音的音频（视频、直播、游戏、会议），字幕会自动出现在这里。"
          />
        )}

        {entries.map((entry) => (
          <CaptionCard key={entry.seq} entry={entry} displayMode={displayMode} />
        ))}
      </div>

      {/* 聆听中指示 */}
      {listening && !paused && (
        <div className="pointer-events-none absolute bottom-24 left-1/2 -translate-x-1/2">
          <StatusDot status="ok" pulse label="聆听中…" />
        </div>
      )}

      {/* 回到最新 */}
      {!atBottom && entries.length > 0 && (
        <button
          type="button"
          onClick={scrollToLatest}
          title="回到最新字幕"
          className={cx(
            'absolute bottom-24 right-10 flex h-9 w-9 items-center justify-center',
            'rounded-pill border border-edge bg-elevated text-secondary',
            'transition-colors duration-normal ease-app hover:text-primary',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
          )}
        >
          <ArrowDown className="h-4 w-4" />
        </button>
      )}

      {state && <StatusPillBar state={state} />}
      <ToastHost />
    </div>
  );
}
