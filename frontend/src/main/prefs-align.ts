/**
 * 偏好对齐（client-gateway-state spec：连接建立后的偏好对齐）
 *
 * Gateway 每次 WS 连接建立（含重连）后调用：
 * 1. 发送 config_sync（幂等）
 * 2. get_config 查询后端 ASR 模型，与 store 不一致时发 change_model
 * 3. 音频源：store 非空且"本后端会话内未应用过"时发一次 set_audio_source
 *    （get_config 不回传音频源，故用 AlignmentTracker 做会话级幂等；
 *      后端进程重启时由 BackendManager 调 markBackendRestarted 复位）
 *
 * 纯 Node 模块：不 import electron，可独立单测。
 */
import type { Gateway } from './gateway';
import type {
  DeviceEngineState, DeviceReason, DeviceResolved, DeviceStateView, GatewayLogger
} from './types';

export type { DeviceStateView } from './types';

export interface PrefsSnapshot {
  targetLanguages: string[];
  activeLanguage: string;
  model: string;
  audioSourceId: string; // '' = 默认设备
  /** 推理设备偏好；auto 时以后端解析结果为准，不做设备对齐 */
  device: 'auto' | 'cpu' | 'cuda';
}

export interface BackendConfigInfo {
  asr?: {
    model_size?: unknown;
    /** 后端当前设备偏好（兼容旧字段） */
    device?: unknown;
    resolved_device?: unknown;
    device_reason?: unknown;
  };
  translation?: { resolved_device?: unknown; device_reason?: unknown };
  active_language?: unknown;
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

export class AlignmentTracker {
  private appliedSourceId: string | null = null;

  /** 后端进程重启后调用：音频源需重新应用 */
  markBackendRestarted(): void {
    this.appliedSourceId = null;
  }

  markSourceApplied(id: string): void {
    this.appliedSourceId = id;
  }

  needsSourceApply(id: string): boolean {
    return id !== '' && id !== this.appliedSourceId;
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
  try {
    const info = await gw.request<BackendConfigInfo>('get_config');
    const backendModel = typeof info?.asr?.model_size === 'string' ? info.asr.model_size : null;
    if (backendModel !== null && backendModel !== prefs.model) {
      gw.send({ type: 'control', action: 'change_model', model_size: prefs.model });
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

  if (tracker.needsSourceApply(prefs.audioSourceId)) {
    const ok = gw.send({
      type: 'control', action: 'set_audio_source', source_id: prefs.audioSourceId
    });
    if (ok) tracker.markSourceApplied(prefs.audioSourceId);
  }

  return deviceView;
}
