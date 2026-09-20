/**
 * 偏好对齐（client-gateway-state spec：连接建立后的偏好对齐）
 *
 * Gateway 每次 WS 连接建立（含重连）后调用：
 * 1. 发送 config_sync（幂等）
 * 2. 源语言：get_config 回传 asr.language 且与 store 不一致时发 set_source_language
 *    （Whisper 模型档位对齐随档位移除，MUST NOT 再按 store 模型字段发 change_model）
 * 3. 音频源：get_config 回传结构化 audio.source 时比较（一致不发/不一致发）；
 *    旧后端缺失该字段 → 用 AlignmentTracker 做"本后端会话内应用一次"的幂等
 *    （后端进程重启时由 BackendManager 调 markBackendRestarted 复位）
 *
 * 纯 Node 模块：不 import electron，可独立单测。
 */
import type { Gateway } from './gateway';
import type {
  DeviceEngineState, DeviceReason, DeviceResolved, DeviceStateView, GatewayLogger
} from './types';
import { audioSourceKey, audioSourceToTarget, sameAudioSource, type AudioSourcePref } from './config-migration';
import type { SourceLanguage } from '../shared/ipc-types';

export type { DeviceStateView } from './types';

export interface PrefsSnapshot {
  targetLanguages: string[];
  activeLanguage: string;
  /** 源语言偏好（识别提示 + 翻译源语言；键 asr.language） */
  sourceLanguage: SourceLanguage;
  /** store 翻译模型偏好（llama.cpp 注册表 id，键 translation.model） */
  translationModel: string;
  /** 结构化音频源偏好（D9/D10）：设备源或按进程源 */
  audioSource: AudioSourcePref;
  /** 推理设备偏好；auto 时以后端解析结果为准，不做设备对齐 */
  device: 'auto' | 'cpu' | 'cuda';
}

export interface BackendConfigInfo {
  asr?: {
    /** 后端当前源语言（新引擎；旧后端为 model_size 档位字段，缺失时跳过对齐） */
    language?: unknown;
    /** 后端当前设备偏好（兼容旧字段） */
    device?: unknown;
    resolved_device?: unknown;
    device_reason?: unknown;
  };
  translation?: { model?: unknown; resolved_device?: unknown; device_reason?: unknown };
  active_language?: unknown;
  /** 后端当前运行源（纯增量字段；旧后端缺失） */
  audio?: { source?: unknown };
}

function asResolved(raw: unknown): DeviceResolved {
  return raw === 'cuda' || raw === 'cpu' ? raw : null;
}

function asReason(raw: unknown): DeviceReason {
  switch (raw) {
    case 'auto':
    case 'user':
    case 'no_cuda':
    case 'load_failed':
    case 'runtime_failed':
      return raw;
    default:
      return 'auto';
  }
}

function engineFromConfig(raw: unknown): DeviceEngineState {
  const rec = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return { resolved: asResolved(rec.resolved_device), reason: asReason(rec.device_reason) };
}

function engineFromBroadcast(raw: unknown): DeviceEngineState {
  const rec = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return { resolved: asResolved(rec.resolved), reason: asReason(rec.reason) };
}

/** get_config → AppState.device 种子；两端都无 resolved（旧后端/字段缺失）返回 null（保持检测态） */
export function deviceViewFromConfig(info: unknown): DeviceStateView | null {
  const rec = (typeof info === 'object' && info !== null ? info : {}) as Record<string, unknown>;
  const asr = engineFromConfig(rec.asr);
  const translation = engineFromConfig(rec.translation);
  if (asr.resolved === null && translation.resolved === null) return null;
  return { asr, translation };
}

/** device_state 广播 → AppState.device；缺失字段容错（resolved=null 表示加载中，视图为 null） */
export function deviceViewFromBroadcast(msg: unknown): DeviceStateView | null {
  const rec = (typeof msg === 'object' && msg !== null ? msg : {}) as Record<string, unknown>;
  const asr = engineFromBroadcast(rec.asr);
  const translation = engineFromBroadcast(rec.translation);
  if (asr.resolved === null && translation.resolved === null) return null;
  return { asr, translation };
}

function asDevicePreference(raw: unknown): 'auto' | 'cpu' | 'cuda' | null {
  return raw === 'auto' || raw === 'cpu' || raw === 'cuda' ? raw : null;
}

/**
 * get_config 回传的 `audio.source` → 结构化设备源；缺失/形状非法（含回退前的
 * 进程源形态）→ null（旧后端，走 AlignmentTracker 幂等路径）。
 */
export function audioSourceFromConfig(raw: unknown): AudioSourcePref | null {
  if (raw === null || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  if (rec.kind === 'device' && typeof rec.id === 'string') {
    return { kind: 'device', id: rec.id };
  }
  return null;
}

export class AlignmentTracker {
  /** 已应用源的稳定键（device:<id>） */
  private appliedKey: string | null = null;

  /** 后端进程重启后调用：音频源需重新应用 */
  markBackendRestarted(): void {
    this.appliedKey = null;
  }

  markSourceApplied(source: AudioSourcePref): void {
    this.appliedKey = audioSourceKey(source);
  }

  /** 默认设备（device:''）永不主动发送；其余源按稳定键做会话级幂等 */
  needsSourceApply(source: AudioSourcePref): boolean {
    if (source.kind === 'device' && source.id === '') return false;
    return audioSourceKey(source) !== this.appliedKey;
  }
}

/**
 * 连接建立后对齐 store 偏好到后端；返回 get_config 中的实际设备视图
 * （旧后端字段缺失时为 null，由调用方决定保持检测态）。
 */
export async function alignPreferences(
  gw: Gateway,
  prefs: PrefsSnapshot,
  tracker: AlignmentTracker,
  logger: GatewayLogger
): Promise<DeviceStateView | null> {
  gw.send({
    type: 'config_sync',
    target_languages: prefs.targetLanguages,
    active_language: prefs.activeLanguage
  });

  let deviceView: DeviceStateView | null = null;
  let info: BackendConfigInfo | null = null;
  try {
    info = await gw.request<BackendConfigInfo>('get_config');

    // 源语言对齐（set_source_language）：后端回传 asr.language 且与 store 不一致时下发；
    // 旧后端/字段缺失 → 容错跳过（档位对齐 change_model 已随 Whisper 档位移除）
    const backendLanguage = typeof info?.asr?.language === 'string'
      ? info.asr.language
      : null;
    if (backendLanguage !== null && backendLanguage !== prefs.sourceLanguage) {
      gw.send({ type: 'control', action: 'set_source_language', language: prefs.sourceLanguage });
    }

    // 翻译模型对齐（change_llm）：后端回传 translation.model 且与 store 不一致时下发；
    // 旧后端缺该字段 → 容错跳过（避免误下发造成无谓重启）
    const backendLlm = typeof info?.translation?.model === 'string'
      ? info.translation.model
      : null;
    if (
      backendLlm !== null
      && prefs.translationModel !== ''
      && backendLlm !== prefs.translationModel
    ) {
      gw.send({ type: 'control', action: 'change_llm', model_id: prefs.translationModel });
    }

    // 设备对齐：仅 store 显式值参与；后端偏好缺失（旧后端）时不发
    const backendDevice = asDevicePreference(info?.asr?.device);
    if (prefs.device !== 'auto' && backendDevice !== null && backendDevice !== prefs.device) {
      gw.send({ type: 'control', action: 'change_device', device: prefs.device });
    }

    deviceView = deviceViewFromConfig(info);
  } catch (err) {
    logger.warn('get_config 失败，跳过模型与设备对齐', err);
  }

    // 音频源对齐：后端回传 audio.source 时比较结构化设备源（一致不发/不一致下发）；
    // 旧后端缺失该字段 → 沿用 AlignmentTracker 的"本后端会话内应用一次"策略。
    const target = audioSourceToTarget(prefs.audioSource);
    const backendSource = audioSourceFromConfig(info?.audio?.source);
    if (backendSource !== null) {
      if (!sameAudioSource(prefs.audioSource, backendSource)) {
        gw.send({ type: 'control', action: 'set_audio_source', source: target });
      }
    } else if (tracker.needsSourceApply(prefs.audioSource)) {
      const ok = gw.send({ type: 'control', action: 'set_audio_source', source: target });
      if (ok) tracker.markSourceApplied(prefs.audioSource);
    }

  return deviceView;
}
