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
  msg: SubtitleMessageView;
}

/** 字幕流（内存滚动窗口，持久化在主进程历史库；resetKey 变化=会话切换，清流） */
export function useSubtitleStream(resetKey?: string | null, cap = 500): CaptionEntry[] {
  const [entries, setEntries] = useState<CaptionEntry[]>([]);
  const seqRef = useRef(0);

  useEffect(() => {
    // 会话切换（含删除活跃会话后自动新建）：直播流清空重新开始
    setEntries([]);
    seqRef.current = 0;
  }, [resetKey]);

  useEffect(() => {
    const off = window.appAPI.onSubtitle((msg) => {
      seqRef.current += 1;
      const entry: CaptionEntry = { seq: seqRef.current, receivedAt: Date.now(), msg };
      setEntries((prev) => {
        const next = [...prev, entry];
        return next.length > cap ? next.slice(next.length - cap) : next;
      });
    });
    return off;
  }, [cap]);

  return entries;
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
