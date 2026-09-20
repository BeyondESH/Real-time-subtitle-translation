import { useEffect, useRef, useState } from 'react';
import { ArrowDown, Pause, Play, Lock, Unlock, Captions } from 'lucide-react';
import { Button, EmptyState, Pill, ProgressBar, StatusDot, cx } from '../components/ui';
import { useAppConfig, useAppState, useSubtitleStream, useTypewriterText, type CaptionEntry } from '../state/hooks';
import { langPairLabel } from './lang-labels';
import { StatusPillBar } from './StatusPillBar';
import { ToastHost } from './ToastHost';

function formatTime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** latency 全字段有效性守卫（全有或全无契约；缺字段/非有限数一律不显示） */
function isLatencyValid(
  v: SubtitleMessageView['latency']
): v is NonNullable<SubtitleMessageView['latency']> {
  return v !== undefined
    && Number.isFinite(v.endpoint_ms)
    && Number.isFinite(v.queue_ms)
    && Number.isFinite(v.asr_ms)
    && Number.isFinite(v.llm_ms)
    && Number.isFinite(v.total_ms);
}

export function CaptionCard({
  entry, displayMode
}: {
  entry: CaptionEntry;
  displayMode: string;
}) {
  const m = entry.msg;
  const streaming = entry.streaming === true;
  const final = m.type === 'subtitle' ? m : null;
  const active = m.active_language;
  const translation = (active && m.translations && m.translations[active]) || m.original;
  const revealed = useTypewriterText(translation);
  const showOriginal = displayMode === 'original_and_translation'
    && Boolean(m.original)
    && m.source_language !== active;
  const latency = final !== null && isLatencyValid(final.latency) ? final.latency : null;
  const firstTokenMs = final !== null
    && typeof final.first_token_ms === 'number'
    && Number.isFinite(final.first_token_ms)
    ? final.first_token_ms
    : null;

  // 脚注分解悬浮提示：定稿才有分解项；首字时延缺失/非法则不占位
  const titleParts: string[] = [];
  if (latency !== null) {
    titleParts.push(
      `静音等待 ${(latency.endpoint_ms / 1000).toFixed(2)}s`,
      `队列 ${(latency.queue_ms / 1000).toFixed(2)}s`,
      `识别 ${(latency.asr_ms / 1000).toFixed(2)}s`,
      `翻译 ${(latency.llm_ms / 1000).toFixed(2)}s`
    );
  }
  if (firstTokenMs !== null) {
    titleParts.push(`首字 ${(firstTokenMs / 1000).toFixed(2)}s`);
  }
  const footnoteTitle = titleParts.length > 0 ? titleParts.join(' · ') : undefined;

  return (
    <div className="caption-enter mb-5 flex flex-col items-center gap-1 text-center">
      {/* 进行中态弱化呈现（次级语气 + 降低不透明度）；定稿保持既有样式 */}
      <p
        className={cx(
          'max-w-3xl text-xl leading-relaxed transition duration-normal ease-app',
          streaming ? 'text-secondary opacity-70' : 'text-primary'
        )}
      >
        {revealed}
      </p>
      {showOriginal && (
        <p className={cx('max-w-3xl text-sm leading-relaxed text-secondary', streaming && 'opacity-60')}>
          {m.original}
        </p>
      )}
      {/* 脚注仅定稿显示（进行中帧无时间戳/语言对/tps/耗时） */}
      {!streaming && (
        <p className="text-xs text-secondary opacity-60" title={footnoteTitle}>
          {formatTime(entry.receivedAt)}
          {' · '}
          {langPairLabel(m.source_language, active)}
          {typeof final?.tps === 'number' ? ` · ${final.tps.toFixed(1)} tok/s` : null}
          {latency !== null ? ` · ${((latency.endpoint_ms + latency.total_ms) / 1000).toFixed(1)}s` : null}
        </p>
      )}
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

      {state && <StatusPillBar state={state} cfg={cfg} />}
      <ToastHost />
    </div>
  );
}
