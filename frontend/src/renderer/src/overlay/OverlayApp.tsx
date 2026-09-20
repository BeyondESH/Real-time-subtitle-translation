/**
 * 字幕悬浮窗（P0：功能等价旧 overlay.html）
 *
 * 数据全部来自主进程桥（appAPI）：字幕流 / 状态 patch / 配置 / toast。
 * MUST NOT 直连后端 WebSocket（overlay-window spec）。
 * 渲染规则遵循 subtitle-display spec：激活语言译文、缺译文回退原文、
 * 显示模式、最近 5 条、暂停丢弃。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Pause } from 'lucide-react';
import { useTypewriterText } from '../state/hooks';

const MAX_HISTORY = 5;
const TOAST_MS = 5000;
const WARN_STRIP_MS = 2000;

type WarningStrip = NonNullable<AppStateView['lastWarning']>;

/**
 * 告警细条文案（subtitle-display spec「过载告警可视化」）：
 * 按 reason 区分；未知 reason 回退泛化文案（含可用丢弃数），MUST NOT 抛错或空条。
 */
export function warningStripText(w: WarningStrip | null): string {
  if (!w) return '';
  const count = typeof w.droppedTotal === 'number' ? w.droppedTotal : 0;
  switch (w.reason) {
    case 'queue_full':
      return `处理过载 · 累计丢弃 ${count} 句（建议切换更小的模型）`;
    case 'stalled':
      return w.message ?? '识别引擎停滞，正在自动恢复…';
    case 'engine_degraded':
      return w.message ?? '识别引擎已降级运行（建议切换更小的模型）';
    default:
      return count > 0
        ? `识别引擎告警 · 累计丢弃 ${count} 句`
        : '识别引擎告警（收到未知类型通知）';
  }
}

interface CaptionEntry {
  seq: number;
  msg: SubtitleMessageView | SubtitlePartialMessageView;
  /** true = 进行中（subtitle_partial）；定稿后清除 */
  streaming?: boolean;
}

/**
 * 字幕行：译文逐字揭示（打字机）；进行中态弱化。
 * 抽为组件以在 map 回调外合法使用 Hook。
 */
function OverlayLine({
  msg, streaming, displayMode
}: {
  msg: CaptionEntry['msg'];
  streaming?: boolean;
  displayMode: string;
}) {
  // 激活语言来自消息本身，前端不硬编码语言代码
  const active = msg.active_language;
  const translation = (active && msg.translations && msg.translations[active]) || msg.original;
  const revealed = useTypewriterText(translation);
  const showOriginal = displayMode === 'original_and_translation'
    && Boolean(msg.original)
    && msg.source_language !== active;
  return (
    <div className="subtitle-line caption-enter">
      {/* 进行中态弱化（降低不透明度）；悬浮窗无 Tailwind 入口，故用无色彩的 inline opacity */}
      <div
        className="subtitle-translation"
        style={streaming === true ? { opacity: 0.6 } : undefined}
      >
        {revealed}
      </div>
      {showOriginal && <div className="subtitle-original">{msg.original}</div>}
    </div>
  );
}

export function OverlayApp() {
  const [captions, setCaptions] = useState<CaptionEntry[]>([]);
  const [displayMode, setDisplayMode] = useState<string>('original_and_translation');
  const [toast, setToast] = useState<ToastView | null>(null);
  const [modelDownload, setModelDownload] = useState<AppStateView['modelDownload']>(null);
  const [paused, setPaused] = useState(false);
  const [warning, setWarning] = useState<WarningStrip | null>(null);

  const pausedRef = useRef(false);
  const seqRef = useRef(0);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const warnTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 用户样式配置 → CSS 变量（subtitle-display spec：样式 MUST NOT 硬编码） */
  const applyConfig = useCallback((cfg: AppConfigView | null | undefined) => {
    const s = cfg?.subtitle;
    if (!s) return;
    const root = document.documentElement;
    root.style.setProperty('--sub-font-family', s.fontFamily);
    root.style.setProperty('--sub-font-size', `${s.fontSize}px`);
    root.style.setProperty('--sub-font-color', s.fontColor);
    root.style.setProperty('--sub-stroke-color', s.strokeColor);
    root.style.setProperty('--sub-stroke-width', `${s.strokeWidth}px`);
    setDisplayMode(s.displayMode);
  }, []);

  useEffect(() => {
    window.appAPI
      .getConfig()
      .then(applyConfig)
      .catch((e: unknown) => console.error('配置加载失败，使用默认值', e));

    window.appAPI
      .getState()
      .then((st) => {
        pausedRef.current = st.capture === 'paused';
        setPaused(pausedRef.current);
        document.body.classList.toggle('unlocked', !st.locked);
        setModelDownload(st.modelDownload);
      })
      .catch((e: unknown) => console.error('状态快照加载失败', e));

    const offs = [
      window.appAPI.onConfigChanged(applyConfig),
      window.appAPI.onStatePatch((patch) => {
        if (patch.capture !== undefined) {
          pausedRef.current = patch.capture === 'paused';
          setPaused(pausedRef.current);
        }
        if (patch.locked !== undefined) {
          document.body.classList.toggle('unlocked', !patch.locked);
        }
        if (patch.modelDownload !== undefined) setModelDownload(patch.modelDownload);
        if (patch.lastWarning !== undefined && patch.lastWarning !== null) {
          // 连续告警刷新同一条（重置换 2s 计时），不堆叠
          setWarning(patch.lastWarning);
          if (warnTimer.current) clearTimeout(warnTimer.current);
          warnTimer.current = setTimeout(() => {
            warnTimer.current = null;
            setWarning(null);
          }, WARN_STRIP_MS);
        }
      }),
      window.appAPI.onSubtitle((ev) => {
        if (pausedRef.current) return; // 暂停期间收到的字幕（含进行中帧）丢弃不渲染

        if (ev.type === 'subtitle_cancel') {
          // 清算：移除同 id 的进行中行（从未成为正式字幕）
          setCaptions((prev) => {
            const idx = prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
            return idx < 0 ? prev : prev.filter((_, i) => i !== idx);
          });
          return;
        }

        // seq 在 updater 外分配（StrictMode 会双调用 updater，避免重复自增）
        const seq = ++seqRef.current;

        if (ev.type === 'subtitle_partial') {
          setCaptions((prev) => {
            const idx = prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
            if (idx >= 0) {
              // 原地替换：保 seq → React 不重挂载、入场动效不重放
              const next = [...prev];
              next[idx] = { ...next[idx], msg: ev };
              return next;
            }
            // 新增进行中行且 MUST NOT 裁剪：窗口推进只由定稿触发
            return [...prev, { seq, msg: ev, streaming: true }];
          });
          return;
        }

        // 定稿（subtitle）：同 id 原地替换并清标记，否则追加；窗口在定稿时裁剪
        setCaptions((prev) => {
          const idx = ev.id === undefined
            ? -1
            : prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
          const next: CaptionEntry[] = idx >= 0
            ? prev.map((e, i) => (i === idx ? { ...e, msg: ev, streaming: false } : e))
            : [...prev, { seq, msg: ev, streaming: false }];
          return next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
        });
      }),
      window.appAPI.onToast((t) => {
        // 过载告警已由顶部琥珀细条呈现，悬浮窗不重复弹 toast
        if (t.kind === 'warn') return;
        setToast(t);
        if (toastTimer.current) clearTimeout(toastTimer.current);
        toastTimer.current = setTimeout(() => setToast(null), TOAST_MS);
      })
    ];

    return () => {
      for (const off of offs) off();
      if (toastTimer.current) clearTimeout(toastTimer.current);
      if (warnTimer.current) clearTimeout(warnTimer.current);
    };
  }, [applyConfig]);

  const progressPct = modelDownload
    ? Math.min(100, Math.max(0, Math.round(modelDownload.progress)))
    : 0;

  return (
    <div className="overlay-shell">
      {warning && (
        <div className="warn-strip">
          {warningStripText(warning)}
        </div>
      )}

      {paused && (
        <div className="pause-badge" aria-label="已暂停">
          <Pause className="h-4 w-4" />
        </div>
      )}

      {modelDownload && (
        <div className="progress-panel">
          <div className="progress-title">模型下载中...</div>
          <div className="progress-model">{modelDownload.name}</div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="progress-status">{modelDownload.message}</div>
        </div>
      )}

      {toast && <div className={`toast toast-${toast.kind}`}>{toast.text}</div>}

      <div className="subtitle-list">
        {captions.map(({ seq, msg, streaming }) => (
          <OverlayLine key={seq} msg={msg} streaming={streaming} displayMode={displayMode} />
        ))}
      </div>
    </div>
  );
}
