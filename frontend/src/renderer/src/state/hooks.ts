/**
 * appAPI 订阅 hooks —— 渲染层无本地权威状态（client-gateway-state spec），
 * 一切事实来自主进程快照 + patch 流。
 */
import { useEffect, useRef, useState } from 'react';

/** AppState 快照 + patch 增量合并；首帧前为 null */
export function useAppState(): AppStateView | null {
  const [state, setState] = useState<AppStateView | null>(null);

  useEffect(() => {
    let alive = true;
    window.appAPI
      .getState()
      .then((s) => { if (alive) setState(s); })
      .catch((e: unknown) => console.error('状态快照加载失败', e));
    const off = window.appAPI.onStatePatch((patch) => {
      setState((prev) => (prev ? { ...prev, ...patch } : prev));
    });
    return () => {
      alive = false;
      off();
    };
  }, []);

  return state;
}

/** 配置快照 + 变更推送 */
export function useAppConfig(): AppConfigView | null {
  const [cfg, setCfg] = useState<AppConfigView | null>(null);

  useEffect(() => {
    let alive = true;
    window.appAPI
      .getConfig()
      .then((c) => { if (alive) setCfg(c); })
      .catch((e: unknown) => console.error('配置加载失败', e));
    const off = window.appAPI.onConfigChanged((c) => setCfg(c));
    return () => {
      alive = false;
      off();
    };
  }, []);

  return cfg;
}

/** 环境能力（WCO/亚克力/版本），加载前为 null */
export function useEnv(): EnvView | null {
  const [env, setEnv] = useState<EnvView | null>(null);
  useEffect(() => {
    let alive = true;
    window.appAPI
      .getEnv()
      .then((e) => { if (alive) setEnv(e); })
      .catch((err: unknown) => console.error('环境信息加载失败', err));
    return () => { alive = false; };
  }, []);
  return env;
}

export interface CaptionEntry {
  seq: number;
  /** 本地接收时刻（ms epoch） */
  receivedAt: number;
  /** 进行中帧 | 定稿帧（消费方按 msg.type 判定）；进行中帧 MUST NOT 落库 */
  msg: SubtitleMessageView | SubtitlePartialMessageView;
  /** true = 进行中（subtitle_partial）；定稿后清除 */
  streaming?: boolean;
}

/**
 * 字幕流（内存滚动窗口，持久化在主进程历史库；resetKey 变化=会话切换，清流）。
 *
 * 按 id upsert（design D7）：partial 原地替换（保 seq/receivedAt，避免 React 重挂载）否则追加；
 * final 替换同 id 并清除 streaming（缺索引时容错追加，兼容无 id 的旧后端）；cancel 移除进行中条目。
 */
export function useSubtitleStream(resetKey?: string | null, cap = 500): CaptionEntry[] {
  const [entries, setEntries] = useState<CaptionEntry[]>([]);
  const seqRef = useRef(0);

  useEffect(() => {
    // 会话切换（含删除活跃会话后自动新建）：直播流清空重新开始
    setEntries([]);
    seqRef.current = 0;
  }, [resetKey]);

  useEffect(() => {
    const off = window.appAPI.onSubtitle((ev) => {
      if (ev.type === 'subtitle_cancel') {
        // 清算：移除同 id 的进行中条目（它从未成为正式字幕）
        setEntries((prev) => {
          const idx = prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
          return idx < 0 ? prev : prev.filter((_, i) => i !== idx);
        });
        return;
      }

      // seq 在 updater 外分配（StrictMode 会双调用 updater，避免重复自增）
      seqRef.current += 1;
      const seq = seqRef.current;
      const receivedAt = Date.now();

      if (ev.type === 'subtitle_partial') {
        setEntries((prev) => {
          const idx = prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], msg: ev };
            return next;
          }
          const next: CaptionEntry[] = [...prev, {
            seq, receivedAt, msg: ev, streaming: true
          }];
          return next.length > cap ? next.slice(next.length - cap) : next;
        });
        return;
      }

      // 定稿（subtitle）
      setEntries((prev) => {
        const idx = ev.id === undefined
          ? -1
          : prev.findIndex((e) => e.streaming === true && e.msg.id === ev.id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = { ...next[idx], msg: ev, streaming: false };
          return next;
        }
        const next: CaptionEntry[] = [...prev, {
          seq, receivedAt, msg: ev, streaming: false
        }];
        return next.length > cap ? next.slice(next.length - cap) : next;
      });
    });
    return off;
  }, [cap]);

  return entries;
}

/** 打字机揭示速度（字符/秒）：译文逐字上屏的可见节奏 */
const TYPEWRITER_CHARS_PER_SEC = 50;

/**
 * 是否直出全文（不播打字机动画）：prefers-reduced-motion 降低动效，或
 * 环境无法检测偏好（无 matchMedia，如 jsdom/SSR）时保守直出。
 */
function isInstantReveal(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return true;
  }
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return true;
  }
}

/**
 * 译文逐字揭示（打字机）。
 *
 * 生成端（GPU 约 200 tok/s）会在数十毫秒内产出整句，仅靠后端推送无法形成
 * 可见的逐字节奏；故由渲染层按固定节奏从 0 揭示 target：
 * - target 增长（进行中帧累积/定稿补齐）时续播，不重头；
 * - target 非扩展式变化（退化重写替换）时从头播放；
 * - prefers-reduced-motion 或无法检测偏好时直接呈现全文。
 */
export function useTypewriterText(target: string): string {
  const [count, setCount] = useState(() => (isInstantReveal() ? target.length : 0));
  const targetRef = useRef(target);
  const prevTargetRef = useRef<string | null>(null);

  useEffect(() => {
    const prev = prevTargetRef.current;
    prevTargetRef.current = target;
    targetRef.current = target;
    setCount((c) => {
      if (isInstantReveal()) return target.length;
      if (prev !== null && !target.startsWith(prev)) return 0;
      return Math.min(c, target.length);
    });
  }, [target]);

  const instant = isInstantReveal();
  const pending = !instant && count < target.length;
  useEffect(() => {
    if (!pending) return;
    const id = window.setInterval(() => {
      setCount((c) => Math.min(c + 1, targetRef.current.length));
    }, Math.max(10, Math.round(1000 / TYPEWRITER_CHARS_PER_SEC)));
    return () => window.clearInterval(id);
  }, [pending]);

  return instant ? target : target.slice(0, count);
}

/** 历史变更订阅（侧栏/回放页刷新触发器） */
export function useHistoryChanged(onChanged: () => void): void {
  useEffect(() => {
    const off = window.appAPI.onHistoryChanged(() => onChanged());
    return off;
  }, [onChanged]);
}

export interface ToastEntry {
  id: number;
  text: string;
  kind: 'info' | 'warn' | 'error';
}

const TOAST_MS = 5000;

/** 瞬态提示队列（自动 5s 消失，最新在下） */
export function useToasts(): ToastEntry[] {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const idRef = useRef(0);

  useEffect(() => {
    const timers = new Map<number, ReturnType<typeof setTimeout>>();
    const off = window.appAPI.onToast((t) => {
      idRef.current += 1;
      const id = idRef.current;
      setToasts((prev) => [...prev.slice(-3), { id, text: t.text, kind: t.kind }]);
      timers.set(id, setTimeout(() => {
        timers.delete(id);
        setToasts((prev) => prev.filter((x) => x.id !== id));
      }, TOAST_MS));
    });
    return () => {
      off();
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  return toasts;
}

/** 深链导航订阅（托盘"设置"→ /settings/general 等） */
export function useNavigateChannel(navigate: (route: string) => void): void {
  useEffect(() => {
    const off = window.appAPI.onNavigate((route) => navigate(route));
    return off;
  }, [navigate]);
}
