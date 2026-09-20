/**
 * 音频源列表共享逻辑（设备-only；胶囊条 / 设置页 / 引导页三处同源）。
 *
 * 与框架无关、可单测：经 `get_audio_sources` 拉取回环设备，失败以显式错误呈现；
 * 构造选项（默认回环设备 + 设备列表）；提供"当前选中"匹配。
 *
 * 值编码（Select 组件为字符串值）：`device:<id>`（`device:` = 默认回环设备）。
 */
import type { AudioSourceTarget, WsResponse } from '../../../shared/ipc-types';

export const AUDIO_SYSTEM_LABEL = '默认回环设备（推荐）';
export const AUDIO_DEVICES_TITLE = '设备';
export const AUDIO_DEVICES_EMPTY_HINT = '未发现可用的回环设备';

export interface AudioDeviceInfo {
  id: string;
  name: string;
  is_loopback?: boolean;
  channels?: number;
}

/** 一条可选项：key 供字符串 Select 使用，source 为派发的线上形状 */
export interface AudioSourceOption {
  key: string;
  label: string;
  source: AudioSourceTarget;
}

export interface AudioSourceList {
  system: AudioSourceOption;
  devices: AudioSourceOption[];
  devicesHint: string | null;
  devicesError: string | null;
}

export interface AudioSelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface AudioSelectModel {
  options: AudioSelectOption[];
  value: string;
}

export const SYSTEM_AUDIO_OPTION: AudioSourceOption = {
  key: 'device:',
  label: AUDIO_SYSTEM_LABEL,
  source: { kind: 'device', id: '' }
};

// ---------- 编码 / 解码 ----------

export function encodeAudioSource(source: AudioSourceTarget): string {
  return `device:${source.id}`;
}

export function decodeAudioSource(value: string): AudioSourceTarget | null {
  if (value.startsWith('device:')) {
    return { kind: 'device', id: value.slice('device:'.length) };
  }
  return null;
}

/** store 偏好 → Select 值 */
export function encodeAudioPref(pref: AudioSourcePrefView): string {
  return encodeAudioSource({ kind: 'device', id: pref.id });
}

// ---------- 拉取 ----------

export type WsRequestFn = (method: string, params?: unknown) => Promise<WsResponse>;

export interface AudioSourcesData {
  devices: AudioDeviceInfo[] | null;
  deviceError: string | null;
}

/** WS 错误 → 既有设置页/胶囊文案 */
export function audioErrorText(error: string, message: string): string {
  if (error === 'not_connected') return '后端未连接';
  if (error === 'timeout') return '拉取超时，请确认后端已启动';
  return `拉取失败: ${message}`;
}

function defaultRequest(method: string, params?: unknown): Promise<WsResponse> {
  return window.appAPI.wsRequest(method, params);
}

/** 拉取回环设备列表；失败以显式 error 呈现，绝不返回伪造数据。 */
export async function fetchAudioSources(request: WsRequestFn = defaultRequest): Promise<AudioSourcesData> {
  try {
    const resp = await request('get_audio_sources');
    return settleDevices(resp);
  } catch (err) {
    return { devices: null, deviceError: String(err) };
  }
}

function settleDevices(resp: WsResponse): AudioSourcesData {
  if (!resp.ok) return { devices: null, deviceError: audioErrorText(resp.error, resp.message) };
  if (!Array.isArray(resp.result)) {
    return { devices: null, deviceError: '拉取失败: 返回格式异常' };
  }
  return { devices: resp.result as AudioDeviceInfo[], deviceError: null };
}

// ---------- 构造选项 ----------

export function buildAudioSourceList(data: AudioSourcesData): AudioSourceList {
  const devices = data.devices
    ? data.devices.filter((d) => d.is_loopback !== false).map((d) => deviceOption(d.id, d.name))
    : [];

  return {
    system: SYSTEM_AUDIO_OPTION,
    devices,
    devicesError: data.deviceError,
    devicesHint: !data.deviceError && devices.length === 0 ? AUDIO_DEVICES_EMPTY_HINT : null
  };
}

function deviceOption(id: string, name: string): AudioSourceOption {
  return {
    key: encodeAudioSource({ kind: 'device', id }),
    label: name,
    source: { kind: 'device', id }
  };
}

// ---------- 选中匹配 ----------

/** 偏好是否命中某候选：设备源按 id 相等 */
export function isAudioOptionSelected(pref: AudioSourcePrefView, option: AudioSourceOption): boolean {
  return pref.id === option.source.id;
}

/** 仅含真实可选项（默认回环设备 + 设备列表）；空/错误说明由组件另行展示 */
export function toSelectOptions(list: AudioSourceList): AudioSelectOption[] {
  return [
    { value: list.system.key, label: list.system.label },
    ...list.devices.map((o) => ({ value: o.key, label: o.label }))
  ];
}

/**
 * Select 的 options + value。偏好不在列表（设备断开）时，补一条当前值置顶，
 * 避免下拉误导性回落到首项。
 */
export function buildAudioSelectModel(list: AudioSourceList, pref: AudioSourcePrefView): AudioSelectModel {
  const all = [list.system, ...list.devices];
  const hit = all.find((o) => isAudioOptionSelected(pref, o));
  const options = toSelectOptions(list);
  if (hit) return { options, value: hit.key };

  const value = encodeAudioPref(pref);
  return { options: [{ value, label: fallbackPrefLabel(pref, all) }, ...options], value };
}

function fallbackPrefLabel(pref: AudioSourcePrefView, all: AudioSourceOption[]): string {
  if (pref.id === '') return AUDIO_SYSTEM_LABEL;
  const device = all.find((o) => o.source.id === pref.id);
  return device?.label ?? pref.id;
}

// ---------- 胶囊标签 ----------

/** AppState.audioSource（显示串）→ 粗粒度偏好（胶囊面板无 cfg 时的选中回退） */
export function stateAudioPref(audioSource: string, list: AudioSourceList): AudioSourcePrefView {
  if (audioSource === '') return { kind: 'device', id: '' };
  const device = list.devices.find((o) => o.source.id === audioSource);
  return { kind: 'device', id: device ? audioSource : '' };
}

/** AppState.audioSource（显示串）→ 胶囊文案；列表未加载/未命中时回退原始串 */
export function resolveAudioSourceLabel(audioSource: string, list: AudioSourceList | null): string {
  if (audioSource === '') return '默认回环设备';
  if (list) {
    const device = list.devices.find((o) => o.source.id === audioSource);
    if (device) return device.label;
  }
  return audioSource;
}
