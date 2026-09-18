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
import {
  audioSourceFromTarget, audioSourceLabel, audioSourceToTarget, sameAudioTarget
} from './config-migration';
import type {
  AudioSourceLostMessage, AudioSourcePref, BackendErrorMessage, DeviceStateView, GatewayLogger,
  Intent, KnownBroadcast, ModelProgressMessage, PipelineWarningMessage, SubtitleMessage,
  ToastMessage, UnknownBroadcast, VadStateMessage
} from '../shared/ipc-types';
import { DEFAULT_AUDIO_SOURCE } from '../shared/ipc-types';

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
    llm?: string;
    activeLanguage?: string;
    audioSource?: AudioSourcePref;
    device?: 'auto' | 'cpu' | 'cuda';
    deviceState?: DeviceStateView | null;
  } = {};

  /**
   * 对齐路径待确认音频源（D9）：align 下发 set_audio_source 后置位；若后端以
   * invalid_audio_source 拒绝且无用户乐观锚点，则按"粘性回退默认设备"处理。
   */
  private pendingAlignSource: AudioSourcePref | null = null;

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
      translationModel: t.model,
      audioSource: this.deps.config.get('audio').source,
      device: this.deps.config.get('inference').device
    };
  }

  /** 连接建立/配置保存后把后端运行态对齐到 store 偏好（含实际设备种入 AppState） */
  async align(): Promise<void> {
    const prefs = this.prefs();
    // 记录本次对齐将下发的源（默认设备不发）；无用户锚点的 invalid_audio_source
    // 视为对齐路径拒绝 → 粘性回退默认设备（D6/D9）
    this.pendingAlignSource = prefs.audioSource.kind === 'device' && prefs.audioSource.id === ''
      ? null
      : prefs.audioSource;
    const view = await alignPreferences(
      this.deps.gateway, prefs, this.deps.tracker, this.deps.logger
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
      case 'setLlm': {
        if (typeof intent.modelId !== 'string' || intent.modelId === '') {
          this.toast(`不支持的翻译模型: ${String(intent.modelId)}`, 'error');
          return;
        }
        const p = this.prefs();
        if (p.translationModel === intent.modelId) return;
        this.applyLlm(intent.modelId);
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
        const prev = this.prefs().audioSource;
        const next = audioSourceFromTarget(intent.source);
        // 同值跳过按"线上目标"比较（含 PID：同名不同实例视为换源）
        const prevTarget = audioSourceToTarget(prev);
        if (prevTarget !== null && sameAudioTarget(prevTarget, intent.source)) return;
        this.optimistic.audioSource = prev;
        this.pendingAlignSource = null; // 用户主动切换：覆盖对齐路径标记
        this.deps.gateway.send({
          type: 'control', action: 'set_audio_source', source: intent.source
        });
        this.deps.config.set('audio', { source: next });
        this.deps.state.dispatch({
          type: 'audioSourceChanged', audioSource: audioSourceLabel(next)
        });
        return;
      }
      case 'newSession': {
        const h = this.deps.history;
        if (!h) return;
        const id = h.newSession(
          Date.now(), audioSourceLabel(this.deps.config.get('audio').source)
        );
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
    // 音频源：配置写入路径不设用户乐观锚点（形态不定）；差异经 align 下发，
    // 拒绝时按对齐路径粘性回退默认设备（D9）
    const nextAudioLabel = audioSourceLabel(next.audioSource);
    if (s.audioSource !== nextAudioLabel) {
      patch.audioSource = nextAudioLabel;
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

  private applyLlm(next: string): void {
    const t = this.deps.config.get('translation');
    this.optimistic.llm = t.model;
    this.deps.gateway.send({ type: 'control', action: 'change_llm', model_id: next });
    this.deps.config.set('translation', { ...t, model: next });
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
          droppedTotal: typeof w.dropped === 'number' ? w.dropped : undefined,
          reason: typeof w.reason === 'string' ? w.reason : undefined,
          message: typeof w.message === 'string' ? w.message : undefined,
          pending: typeof w.pending === 'number' ? w.pending : undefined,
          detail: asDetail(w.detail)
        });
        this.toast(warningToastText(w.reason, w.message), 'warn');
        return;
      }
      case 'error': {
        const err = msg as BackendErrorMessage;
        this.deps.logger.warn('后端错误回执:', err.code ?? '-', err.message ?? '');
        const handled = this.rollbackIfRejected(err.code);
        if (handled === 'audio_align_reset') {
          // 对齐路径拒绝：粘性回退已发生，仅一条提示（不叠加通用错误 toast）
          this.toast('音频源不可用，已切换为整个系统', 'warn');
        } else {
          this.toast(err.message ?? '后端错误', 'error');
        }
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
      case 'audio_source_lost': {
        // D6 粘性重置：目标进程退出 → toast + store/state 同步回默认设备，
        // 避免"启动时进程不可用 → 对齐失败 → 回滚 → 下次再试"的重试循环
        const lost = msg as AudioSourceLostMessage;
        this.toast(`音频源「${lost.name}」已退出，已切换为整个系统`, 'warn');
        this.pendingAlignSource = null;
        this.optimistic.audioSource = undefined;
        this.deps.config.set('audio', { source: { ...DEFAULT_AUDIO_SOURCE } });
        this.deps.state.dispatch({ type: 'audioSourceChanged', audioSource: '' });
        return;
      }
      default:
        this.deps.logger.info('未路由的广播类型:', msg.type);
    }
  }

  /** 后端否定回执 → 按锚点回退乐观更新（config + state 同步恢复） */
  private rollbackIfRejected(code: string | undefined): 'audio_align_reset' | null {
    switch (code) {
      case 'invalid_model': {
        const prev = this.optimistic.model;
        this.optimistic.model = undefined;
        if (prev === undefined) return null;
        this.deps.config.set('asr', { model: prev });
        this.deps.state.dispatch({ type: 'modelChanged', model: prev });
        break;
      }
      case 'invalid_language': {
        const prev = this.optimistic.activeLanguage;
        this.optimistic.activeLanguage = undefined;
        if (prev === undefined) return null;
        const t = this.deps.config.get('translation');
        this.deps.config.set('translation', { ...t, activeLanguage: prev });
        this.deps.state.dispatch({ type: 'languageChanged', activeLanguage: prev });
        break;
      }
      case 'invalid_audio_source': {
        const prev = this.optimistic.audioSource;
        this.optimistic.audioSource = undefined;
        if (prev !== undefined) {
          // 用户主动切换失败：回退原源（existing behavior）
          this.deps.config.set('audio', { source: prev });
          this.deps.state.dispatch({
            type: 'audioSourceChanged', audioSource: audioSourceLabel(prev)
          });
          return null;
        }
        // 对齐路径失败（应用未运行等）：粘性回退默认设备，后续连接不再重试
        const pending = this.pendingAlignSource;
        this.pendingAlignSource = null;
        if (pending === null) return null;
        this.deps.config.set('audio', { source: { ...DEFAULT_AUDIO_SOURCE } });
        this.deps.state.dispatch({ type: 'audioSourceChanged', audioSource: '' });
        return 'audio_align_reset';
      }
      case 'invalid_llm':
      case 'model_download_failed':
      case 'llm_load_failed': {
        const prev = this.optimistic.llm;
        this.optimistic.llm = undefined;
        if (prev === undefined) return null;
        const t = this.deps.config.get('translation');
        this.deps.config.set('translation', { ...t, model: prev });
        break;
      }
      case 'invalid_device': {
        const prev = this.optimistic.device;
        if (prev === undefined) return null;
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
    return null;
  }

  /** 字幕落库：静音自动切分 → 写入 → 首句改题时通知侧栏刷新 */
  private writeHistory(m: SubtitleMessage): void {
    const h = this.deps.history;
    if (!h) return;

    const now = Date.now();
    const audioSource = audioSourceLabel(this.deps.config.get('audio').source);
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

/** pipeline_warning 缺 message 时按 reason 兜底（主窗口 toast；细条文案见 OverlayApp） */
function warningToastText(reason: string | undefined, message: string | undefined): string {
  if (typeof message === 'string' && message.length > 0) return message;
  switch (reason) {
    case 'stalled':
      return '识别引擎停滞，正在自动恢复…';
    case 'engine_degraded':
      return '识别引擎已降级运行（建议换更小的模型）';
    default:
      return '处理过载：已丢弃最旧语句（建议换更小的模型）';
  }
}

/** detail 容错：非对象（含数组以外的非法值）一律丢弃 */
function asDetail(raw: unknown): Record<string, unknown> | undefined {
  return typeof raw === 'object' && raw !== null && !Array.isArray(raw)
    ? (raw as Record<string, unknown>)
    : undefined;
}
