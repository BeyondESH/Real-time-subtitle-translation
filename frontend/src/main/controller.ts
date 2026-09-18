/**
 * Controller — 意图执行 + Gateway 广播路由（tasks 2.3/2.9 的核心）
 *
 * 单一动作源：托盘、全局快捷键、任意窗口 UI 全部经 handle(intent)；
 * Gateway 广播按类型路由到 AppState / IPC stream / toast。
 * 纯 Node 模块（不 import electron），窗口广播经注入的 broadcast 回调。
 */
import type { Gateway } from './gateway';
import type { AppState, StateStore } from './state';
import type { ConfigStore } from './config';
import type { HistoryStore } from './history-db';
import { AlignmentTracker, alignPreferences, deviceViewFromBroadcast, type PrefsSnapshot } from './prefs-align';
import type {
  BackendErrorMessage, DeviceStateView, GatewayLogger, Intent, KnownBroadcast, ModelProgressMessage,
  PipelineWarningMessage, SubtitleMessage, ToastMessage, UnknownBroadcast, VadStateMessage
} from '../shared/ipc-types';

export const WHISPER_MODELS: readonly string[] = [
  'tiny', 'base', 'small', 'medium', 'large-v3'
];

/** 模型下载完成后进度态停留时长（与旧 overlay 行为等价） */
const DONE_HIDE_MS = 3000;

export interface ControllerDeps {
  gateway: Gateway;
  state: StateStore;
  config: ConfigStore;
  tracker: AlignmentTracker;
  logger: GatewayLogger;
  /** 历史库（P2）；null 时字幕只分发不落库 */
  history: HistoryStore | null;
  /** 向所有存活窗口广播 IPC 事件 */
  broadcast(channel: 'app:subtitle' | 'app:toast' | 'history:changed', payload: unknown): void;
  onShowSettings(): void;
  onRestartBackend(): void;
}

export class Controller {
  private doneTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * 乐观更新回退锚点（settings-management spec：失败时 UI 状态回退为原值）。
   * control 消息是 fire-and-forget，后端以 {type:'error', code} 回执否定时按锚点恢复。
   */
  private readonly optimistic: {
    model?: string;
    activeLanguage?: string;
    audioSource?: string;
    device?: 'auto' | 'cpu' | 'cuda';
    deviceState?: DeviceStateView | null;
  } = {};

  constructor(private readonly deps: ControllerDeps) {
    this.wireGateway();
  }

  // ---------- 偏好快照 ----------

  prefs(): PrefsSnapshot {
    const t = this.deps.config.get('translation');
    return {
      targetLanguages: t.targetLanguages,
      activeLanguage: t.activeLanguage,
      model: this.deps.config.get('asr').model,
      audioSourceId: this.deps.config.get('audio').sourceId,
      device: this.deps.config.get('inference').device
    };
  }

  /** 连接建立/配置保存后把后端运行态对齐到 store 偏好（含实际设备种入 AppState） */
  async align(): Promise<void> {
    const view = await alignPreferences(
      this.deps.gateway, this.prefs(), this.deps.tracker, this.deps.logger
    );
    if (view) {
      this.deps.state.dispatch({ type: 'deviceChanged', device: view });
    }
  }

  /** 后端进程（重）启动后调用：音频源需重新应用 */
  notifyBackendRestarted(): void {
    this.deps.tracker.markBackendRestarted();
  }

  // ---------- 意图执行（唯一动作入口） ----------

  handle(intent: Intent): void {
    switch (intent.type) {
      case 'togglePause': {
        const pausing = this.deps.state.getState().capture === 'running';
        this.deps.gateway.send({
          type: 'control', action: pausing ? 'pause' : 'resume'
        });
        this.deps.state.dispatch({ type: 'captureToggled', paused: pausing });
        return;
      }
      case 'cycleLanguage': {
        const p = this.prefs();
        if (p.targetLanguages.length === 0) return;
        const next = p.targetLanguages[
          (p.targetLanguages.indexOf(p.activeLanguage) + 1) % p.targetLanguages.length
        ];
        this.handle({ type: 'setLanguage', language: next });
        this.toast(`目标语言: ${next}`);
        return;
      }
      case 'cycleModel': {
        const cur = this.prefs().model;
        const idx = WHISPER_MODELS.indexOf(cur);
        const next = WHISPER_MODELS[(idx + 1) % WHISPER_MODELS.length];
        this.handle({ type: 'setModel', model: next });
        this.toast(`切换模型: ${next}`);
        return;
      }
      case 'setLanguage': {
        const p = this.prefs();
        if (p.activeLanguage === intent.language) return;
        if (!p.targetLanguages.includes(intent.language)) {
          this.toast(`语言 ${intent.language} 不在目标列表`, 'error');
          return;
        }
        this.applyActiveLanguage(intent.language);
        return;
      }
      case 'setModel': {
        const p = this.prefs();
        if (p.model === intent.model) return;
        if (!WHISPER_MODELS.includes(intent.model)) {
          this.toast(`不支持的模型: ${intent.model}`, 'error');
          return;
        }
        this.applyModel(intent.model);
        return;
      }
      case 'toggleLock': {
        const next = !this.deps.state.getState().locked;
        this.deps.config.set('locked', next);
        this.deps.state.dispatch({ type: 'lockToggled', locked: next });
        return;
      }
      case 'toggleOverlay': {
        const next = !this.deps.state.getState().overlayVisible;
        this.deps.state.dispatch({ type: 'overlayVisibilityChanged', visible: next });
        return;
      }
      case 'setAudioSource': {
        const prev = this.prefs().audioSourceId;
        if (prev === intent.id) return;
        this.optimistic.audioSource = prev;
        this.deps.gateway.send({
          type: 'control', action: 'set_audio_source', source_id: intent.id
        });
        this.deps.config.set('audio', { sourceId: intent.id });
        this.deps.state.dispatch({ type: 'audioSourceChanged', audioSource: intent.id });
        return;
      }
      case 'newSession': {
        const h = this.deps.history;
        if (!h) return;
        const id = h.newSession(Date.now(), this.deps.config.get('audio').sourceId);
        this.deps.state.dispatch({ type: 'activeSessionChanged', id });
        this.deps.broadcast('history:changed', { kind: 'session' });
        return;
      }
      case 'setDevice': {
        const prev = this.prefs().device;
        if (prev === intent.device) return;
        if (intent.device !== 'auto' && intent.device !== 'cpu' && intent.device !== 'cuda') {
          this.toast(`不支持的推理设备: ${String(intent.device)}`, 'error');
          return;
        }
        this.applyDevice(intent.device);
        return;
      }
      case 'showSettings':
        this.deps.onShowSettings();
        return;
      case 'restartBackend':
        this.deps.onRestartBackend();
        return;
    }
  }

  /** 设置保存（settings 页改动即落盘路径）后的状态/后端同步 */
  onConfigSaved(): void {
    const next = this.prefs();
    const s = this.deps.state.getState();
    const patch: Partial<AppState> = {};
    if (s.model !== next.model) {
      this.optimistic.model = s.model;
      patch.model = next.model;
    }
    if (s.activeLanguage !== next.activeLanguage) {
      this.optimistic.activeLanguage = s.activeLanguage;
      patch.activeLanguage = next.activeLanguage;
    }
    if (!sameArray(s.targetLanguages, next.targetLanguages)) {
      patch.targetLanguages = [...next.targetLanguages];
    }
    if (s.audioSource !== next.audioSourceId) {
      this.optimistic.audioSource = s.audioSource;
      patch.audioSource = next.audioSourceId;
    }
    if (Object.keys(patch).length > 0) {
      this.deps.state.dispatch({ type: 'configApplied', patch });
    }
    void this.align();
  }

  toast(text: string, kind: ToastMessage['kind'] = 'info'): void {
    this.deps.broadcast('app:toast', { text, kind } satisfies ToastMessage);
  }

  dispose(): void {
    if (this.doneTimer) {
      clearTimeout(this.doneTimer);
      this.doneTimer = null;
    }
  }

  // ---------- 内部 ----------

  private applyActiveLanguage(next: string): void {
    this.optimistic.activeLanguage = this.deps.state.getState().activeLanguage;
    this.deps.gateway.send({ type: 'control', action: 'set_language', language: next });
    const t = this.deps.config.get('translation');
    this.deps.config.set('translation', { ...t, activeLanguage: next });
    this.deps.state.dispatch({ type: 'languageChanged', activeLanguage: next });
  }

  private applyModel(next: string): void {
    this.optimistic.model = this.deps.state.getState().model;
    this.deps.gateway.send({ type: 'control', action: 'change_model', model_size: next });
    this.deps.config.set('asr', { model: next });
    this.deps.state.dispatch({ type: 'modelChanged', model: next });
  }

  private applyDevice(next: 'auto' | 'cpu' | 'cuda'): void {
    this.optimistic.device = this.prefs().device;
    this.optimistic.deviceState = this.deps.state.getState().device;
    this.deps.gateway.send({ type: 'control', action: 'change_device', device: next });
    this.deps.config.set('inference', { device: next });
    // 切换中：状态行回到"正在检测"，待 device_state 广播/降级结果刷新；静默降级不算失败
    this.deps.state.dispatch({ type: 'deviceChanged', device: null });
  }

  private wireGateway(): void {
    this.deps.gateway.onEvent((e) => {
      if (e.kind === 'state') {
        this.deps.state.dispatch({ type: 'connectionChanged', state: e.state });
        if (e.state === 'open') {
          void this.align().catch((err) => this.deps.logger.warn('偏好对齐失败', err));
        }
        return;
      }
      this.routeBroadcast(e.message);
    });
  }

  /** 后端广播 → AppState / 字幕流 IPC / toast（tasks 2.3） */
  private routeBroadcast(msg: KnownBroadcast | UnknownBroadcast): void {
    switch (msg.type) {
      case 'subtitle': {
        const m = msg as SubtitleMessage;
        // 先落库再分发：主窗口未开时历史照常记录（session-history spec）；
        // 写入失败已在 HistoryStore 内记日志，不中断直播分发
        this.writeHistory(m);
        this.deps.broadcast('app:subtitle', m);
        return;
      }
      case 'model_progress': {
        const m = msg as ModelProgressMessage;
        this.deps.state.dispatch({
          type: 'modelDownloadProgress',
          name: m.model_name,
          progress: m.progress,
          message: m.message
        });
        if (m.progress >= 100) this.scheduleDoneHide();
        return;
      }
      case 'pipeline_warning': {
        const w = msg as PipelineWarningMessage;
        this.deps.state.dispatch({
          type: 'pipelineWarning',
          droppedTotal: typeof w.dropped === 'number' ? w.dropped : undefined
        });
        this.toast(w.message ?? '处理过载：已丢弃最旧语句（建议换更小的模型）', 'warn');
        return;
      }
      case 'error': {
        const err = msg as BackendErrorMessage;
        this.deps.logger.warn('后端错误回执:', err.code ?? '-', err.message ?? '');
        this.rollbackIfRejected(err.code);
        this.toast(err.message ?? '后端错误', 'error');
        return;
      }
      case 'vad_state': {
        const v = msg as VadStateMessage;
        if (v.state === 'speech' || v.state === 'silence') {
          this.deps.state.dispatch({ type: 'vadChanged', state: v.state });
        }
        return;
      }
      case 'device_state': {
        // resolved=null（加载中）也更新：切换期间状态行即时反映检测态
        this.deps.state.dispatch({
          type: 'deviceChanged',
          device: deviceViewFromBroadcast(msg)
        });
        return;
      }
      default:
        this.deps.logger.info('未路由的广播类型:', msg.type);
    }
  }

  /** 后端否定回执 → 按锚点回退乐观更新（config + state 同步恢复） */
  private rollbackIfRejected(code: string | undefined): void {
    switch (code) {
      case 'invalid_model': {
        const prev = this.optimistic.model;
        this.optimistic.model = undefined;
        if (prev === undefined) return;
        this.deps.config.set('asr', { model: prev });
        this.deps.state.dispatch({ type: 'modelChanged', model: prev });
        break;
      }
      case 'invalid_language': {
        const prev = this.optimistic.activeLanguage;
        this.optimistic.activeLanguage = undefined;
        if (prev === undefined) return;
        const t = this.deps.config.get('translation');
        this.deps.config.set('translation', { ...t, activeLanguage: prev });
        this.deps.state.dispatch({ type: 'languageChanged', activeLanguage: prev });
        break;
      }
      case 'invalid_audio_source': {
        const prev = this.optimistic.audioSource;
        this.optimistic.audioSource = undefined;
        if (prev === undefined) return;
        this.deps.config.set('audio', { sourceId: prev });
        this.deps.state.dispatch({ type: 'audioSourceChanged', audioSource: prev });
        break;
      }
      case 'invalid_device': {
        const prev = this.optimistic.device;
        if (prev === undefined) return;
        const prevView = this.optimistic.deviceState ?? null;
        this.optimistic.device = undefined;
        this.optimistic.deviceState = undefined;
        this.deps.config.set('inference', { device: prev });
        this.deps.state.dispatch({ type: 'deviceChanged', device: prevView });
        break;
      }
      default:
        break;
    }
  }

  /** 字幕落库：静音自动切分 → 写入 → 首句改题时通知侧栏刷新 */
  private writeHistory(m: SubtitleMessage): void {
    const h = this.deps.history;
    if (!h) return;

    const now = Date.now();
    const audioSource = this.deps.config.get('audio').sourceId;
    const split = h.maybeAutoSplit(
      now, this.deps.config.get('sessions').autoSplitSilenceMin, audioSource
    );

    let activeId = h.getActiveId();
    if (activeId === null) {
      activeId = h.ensureActiveSession(now, audioSource);
    }
    if (activeId !== this.deps.state.getState().activeSessionId) {
      this.deps.state.dispatch({ type: 'activeSessionChanged', id: activeId });
      this.deps.broadcast('history:changed', { kind: 'session' });
    }

    const wasFirst = (h.getSession(activeId)?.utterance_count ?? 0) === 0;
    const translation = (m.active_language && m.translations && m.translations[m.active_language])
      || m.original
      || '';
    const id = h.insertUtterance({
      receivedAt: now,
      tsStart: typeof m.ts_start === 'number' ? m.ts_start : null,
      tsEnd: typeof m.ts_end === 'number' ? m.ts_end : null,
      original: m.original ?? '',
      sourceLang: m.source_language ?? '',
      translation,
      targetLang: m.active_language ?? '',
      model: this.deps.state.getState().model
    });

    // 会话级变化（切分/首句自动改题）才通知侧栏，避免每条字幕触发列表刷新
    if (id > 0 && (split || wasFirst)) {
      this.deps.broadcast('history:changed', { kind: 'session' });
    }
  }

  private scheduleDoneHide(): void {
    if (this.doneTimer) clearTimeout(this.doneTimer);
    this.doneTimer = setTimeout(() => {
      this.doneTimer = null;
      this.deps.state.dispatch({ type: 'modelDownloadDone' });
    }, DONE_HIDE_MS);
  }
}

function sameArray(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}
