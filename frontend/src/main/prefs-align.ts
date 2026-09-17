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
import type { GatewayLogger } from './types';

export interface PrefsSnapshot {
  targetLanguages: string[];
  activeLanguage: string;
  model: string;
  audioSourceId: string; // '' = 默认设备
}

export interface BackendConfigInfo {
  asr?: { model_size?: unknown };
  translation?: unknown;
  active_language?: unknown;
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

export async function alignPreferences(
  gw: Gateway,
  prefs: PrefsSnapshot,
  tracker: AlignmentTracker,
  logger: GatewayLogger
): Promise<void> {
  gw.send({
    type: 'config_sync',
    target_languages: prefs.targetLanguages,
    active_language: prefs.activeLanguage
  });

  try {
    const info = await gw.request<BackendConfigInfo>('get_config');
    const backendModel = typeof info?.asr?.model_size === 'string' ? info.asr.model_size : null;
    if (backendModel !== null && backendModel !== prefs.model) {
      gw.send({ type: 'control', action: 'change_model', model_size: prefs.model });
    }
  } catch (err) {
    logger.warn('get_config 失败，跳过模型对齐', err);
  }

  if (tracker.needsSourceApply(prefs.audioSourceId)) {
    const ok = gw.send({
      type: 'control', action: 'set_audio_source', source_id: prefs.audioSourceId
    });
    if (ok) tracker.markSourceApplied(prefs.audioSourceId);
  }
}
