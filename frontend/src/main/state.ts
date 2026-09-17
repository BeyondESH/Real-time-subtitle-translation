/**
 * AppState — 应用唯一状态机（client-gateway-state spec）
 *
 * - reduce: 纯函数，action → 新状态（无变化时返回同一引用）
 * - StateStore: dispatch 唯一变更入口；patch 合帧广播（16ms 批量）
 *
 * 纯 Node 模块：不 import electron，可独立单测。
 */
import type { ConnectionState } from './types';

export interface ModelDownload {
  name: string;
  progress: number;
  message: string;
}

export interface WarningInfo {
  /** 后端累计丢弃数（pipeline_warning.dropped） */
  droppedTotal: number;
  /** 最近一次告警的本地时间戳（ms） */
  at: number;
}

export interface AppState {
  connection: ConnectionState;
  capture: 'running' | 'paused';
  vad: 'speech' | 'silence' | 'unknown';
  model: string;
  modelDownload: ModelDownload | null;
  activeLanguage: string;
  targetLanguages: string[];
  audioSource: string; // '' = 默认设备
  locked: boolean;
  overlayVisible: boolean;
  lastWarning: WarningInfo | null;
  droppedCount: number;
  /** 当前活跃历史会话（P2 起由 HistoryStore 驱动） */
  activeSessionId: string | null;
}

export type AppAction =
  | { type: 'connectionChanged'; state: ConnectionState }
  | { type: 'captureToggled'; paused?: boolean }
  | { type: 'vadChanged'; state: 'speech' | 'silence' }
  | { type: 'modelChanged'; model: string }
  | { type: 'modelDownloadProgress'; name: string; progress: number; message: string }
  | { type: 'modelDownloadDone' }
  | { type: 'languageChanged'; activeLanguage: string }
  | { type: 'targetLanguagesChanged'; targetLanguages: string[] }
  | { type: 'audioSourceChanged'; audioSource: string }
  | { type: 'lockToggled'; locked?: boolean }
  | { type: 'overlayVisibilityChanged'; visible: boolean }
  | { type: 'pipelineWarning'; droppedTotal?: number; at?: number }
  | { type: 'activeSessionChanged'; id: string | null }
  | { type: 'configApplied'; patch: Partial<AppState> };

export function createInitialState(prefs: {
  model: string;
  activeLanguage: string;
  targetLanguages: string[];
  audioSource: string;
  locked: boolean;
  overlayVisible: boolean;
  activeSessionId?: string | null;
}): AppState {
  return {
    connection: 'down',
    capture: 'running',
    vad: 'unknown',
    model: prefs.model,
    modelDownload: null,
    activeLanguage: prefs.activeLanguage,
    targetLanguages: [...prefs.targetLanguages],
    audioSource: prefs.audioSource,
    locked: prefs.locked,
    overlayVisible: prefs.overlayVisible,
    lastWarning: null,
    droppedCount: 0,
    activeSessionId: prefs.activeSessionId ?? null
  };
}

function sameArray(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/**
 * 纯 reducer：无变化时返回原引用（StateStore 依赖该不变式跳过广播）
 */
export function reduce(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'connectionChanged':
      return state.connection === action.state
        ? state
        : { ...state, connection: action.state };

    case 'captureToggled': {
      const paused = action.paused ?? state.capture === 'running';
      const next = paused ? 'paused' : 'running';
      return state.capture === next ? state : { ...state, capture: next };
    }

    case 'vadChanged':
      return state.vad === action.state ? state : { ...state, vad: action.state };

    case 'modelChanged':
      return state.model === action.model ? state : { ...state, model: action.model };

    case 'modelDownloadProgress': {
      const prev = state.modelDownload;
      if (
        prev
        && prev.name === action.name
        && prev.progress === action.progress
        && prev.message === action.message
      ) {
        return state;
      }
      return {
        ...state,
        modelDownload: { name: action.name, progress: action.progress, message: action.message }
      };
    }

    case 'modelDownloadDone':
      return state.modelDownload === null ? state : { ...state, modelDownload: null };

    case 'languageChanged':
      return state.activeLanguage === action.activeLanguage
        ? state
        : { ...state, activeLanguage: action.activeLanguage };

    case 'targetLanguagesChanged': {
      if (sameArray(state.targetLanguages, action.targetLanguages)) return state;
      const next: AppState = {
        ...state,
        targetLanguages: [...action.targetLanguages]
      };
      // 不变式：激活语言必须属于目标列表，否则回退首项
      if (!next.targetLanguages.includes(next.activeLanguage)) {
        next.activeLanguage = next.targetLanguages[0] ?? next.activeLanguage;
      }
      return next;
    }

    case 'audioSourceChanged':
      return state.audioSource === action.audioSource
        ? state
        : { ...state, audioSource: action.audioSource };

    case 'lockToggled': {
      const locked = action.locked ?? !state.locked;
      return state.locked === locked ? state : { ...state, locked };
    }

    case 'overlayVisibilityChanged':
      return state.overlayVisible === action.visible
        ? state
        : { ...state, overlayVisible: action.visible };

    case 'pipelineWarning': {
      const droppedCount = typeof action.droppedTotal === 'number'
        ? action.droppedTotal
        : state.droppedCount + 1;
      const at = action.at ?? Date.now();
      return { ...state, droppedCount, lastWarning: { droppedTotal: droppedCount, at } };
    }

    case 'activeSessionChanged':
      return state.activeSessionId === action.id
        ? state
        : { ...state, activeSessionId: action.id };

    case 'configApplied':
      return { ...state, ...action.patch };

    default:
      return state;
  }
}

export type StateListener = (patch: Partial<AppState>, full: AppState) => void;

const BATCH_MS = 16;

function changedKeys(prev: AppState, next: AppState): Array<keyof AppState> {
  const keys = Object.keys(next) as Array<keyof AppState>;
  return keys.filter((k) => {
    const a = prev[k] as unknown;
    const b = next[k] as unknown;
    if (Array.isArray(a) && Array.isArray(b)) return !sameArray(a, b as string[]);
    return a !== b;
  });
}

/**
 * 状态容器：dispatch → reduce → 合帧 patch 广播（约 16ms 批量）。
 * 订阅者（窗口广播器、对齐器等）只读 getState/dispatch，无本地权威状态。
 */
export class StateStore {
  private state: AppState;
  private pendingPatch: Partial<AppState> | null = null;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly listeners = new Set<StateListener>();

  constructor(initial: AppState) {
    this.state = initial;
  }

  getState(): AppState {
    return this.state;
  }

  dispatch(action: AppAction): void {
    const next = reduce(this.state, action);
    if (next === this.state) return;
    const keys = changedKeys(this.state, next);
    this.state = next;
    const patch = { ...(this.pendingPatch ?? {}) } as Record<string, unknown>;
    for (const k of keys) patch[k as string] = next[k] as unknown;
    this.pendingPatch = patch as Partial<AppState>;
    if (this.flushTimer === null) {
      this.flushTimer = setTimeout(() => this.flush(), BATCH_MS);
    }
  }

  /** 立即冲刷未广播的 patch（窗口关闭/退出前调用） */
  flushNow(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    this.flush();
  }

  subscribe(listener: StateListener): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  private flush(): void {
    this.flushTimer = null;
    const patch = this.pendingPatch;
    this.pendingPatch = null;
    if (!patch) return;
    for (const cb of [...this.listeners]) cb(patch, this.state);
  }
}
