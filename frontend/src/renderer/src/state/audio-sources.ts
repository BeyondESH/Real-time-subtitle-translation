/**
 * 音频源列表共享逻辑（add-per-process-audio-capture D9 / 三处 UI 同源）。
 *
 * 与框架无关、可单测：并行拉取 `get_audio_sources`（设备）与 `get_audio_processes`
 * （应用进程），容忍单侧失败并按段暴露错误；构造分组选项；提供"当前选中"匹配。
 *
 * 值编码（Select 组件为字符串值）：`device:<id>` / `process:<pid>:<name>`。
 * Windows 进程名不含 ':'，设备 id 允许含 ':'（前缀后整体截取），故解码无歧义。
 */
import type { AudioSourceTarget, WsResponse } from '../../../shared/ipc-types';

export const AUDIO_SYSTEM_LABEL = '整个系统（推荐）';
export const AUDIO_APPS_TITLE = '应用';
export const AUDIO_DEVICES_TITLE = '设备';
export const AUDIO_APPS_UNSUPPORTED_HINT = '当前系统不支持按应用捕获（需 Windows 10 2004+）';
export const AUDIO_APPS_EMPTY_HINT = '暂无正在发声的应用（播放声音后出现）';
export const AUDIO_DEVICES_EMPTY_HINT = '未发现可用的回环设备';
/** 胶囊面板打开期间的轻量轮询周期（捕捉新发声应用，与 OBS 口径一致） */
export const AUDIO_POLL_MS = 3000;

export interface AudioDeviceInfo {
  id: string;
  name: string;
  is_loopback?: boolean;
  channels?: number;
}

export interface AudioProcessInfo {
  pid: number;
  name: string;
  active: boolean;
  ordinal: number | null;
}

export interface AudioProcessesResult {
  supported: boolean;
  reason: string | null;
  processes: AudioProcessInfo[];
}

/** 一条可选项：key 供字符串 Select 使用，source 为派发的线上形状 */
export interface AudioSourceOption {
  key: string;
  label: string;
  source: AudioSourceTarget;
}

export interface AudioSourceList {
  system: AudioSourceOption;
  apps: AudioSourceOption[];
  devices: AudioSourceOption[];
  appsSupported: boolean;
  /** 不显示应用区时的说明（不支持 / 暂无发声应用） */
  appsHint: string | null;
  /** 应用区拉取失败（与 unsupported 区分，供错误+重试） */
  appsError: string | null;
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
  return source.kind === 'device'
    ? `device:${source.id}`
    : `process:${source.pid}:${source.name}`;
}

export function decodeAudioSource(value: string): AudioSourceTarget | null {
  if (value.startsWith('device:')) {
    return { kind: 'device', id: value.slice('device:'.length) };
  }
  if (value.startsWith('process:')) {
    const rest = value.slice('process:'.length);
    const sep = rest.indexOf(':');
    if (sep <= 0) return null;
    const pid = Number(rest.slice(0, sep));
    const name = rest.slice(sep + 1);
    if (!Number.isInteger(pid) || name === '') return null;
    return { kind: 'process', pid, name };
  }
  return null;
}

/** store 偏好 → Select 值（进程用 lastPid，缺失回退 -1 仅作展示占位） */
export function encodeAudioPref(pref: AudioSourcePrefView): string {
  return pref.kind === 'device'
    ? encodeAudioSource({ kind: 'device', id: pref.id })
    : encodeAudioSource({ kind: 'process', pid: pref.lastPid ?? -1, name: pref.name });
}

// ---------- 拉取 ----------

export type WsRequestFn = (method: string, params?: unknown) => Promise<WsResponse>;

export interface AudioSourcesData {
  devices: AudioDeviceInfo[] | null;
  deviceError: string | null;
  processes: AudioProcessesResult | null;
  processError: string | null;
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

/**
 * 并行拉取设备与应用进程；单侧失败不影响另一侧（Promise.allSettled）。
 * 任何失败都以显式 error 呈现，绝不返回伪造数据。
 */
export async function fetchAudioSources(request: WsRequestFn = defaultRequest): Promise<AudioSourcesData> {
  const [deviceSettled, processSettled] = await Promise.allSettled([
    request('get_audio_sources'),
    request('get_audio_processes')
  ]);
  const devices = settleDevices(deviceSettled);
  const processes = settleProcesses(processSettled);
  return { ...devices, ...processes };
}

function settleDevices(
  settled: PromiseSettledResult<WsResponse>
): { devices: AudioDeviceInfo[] | null; deviceError: string | null } {
  if (settled.status === 'rejected') {
    return { devices: null, deviceError: String(settled.reason) };
  }
  const resp = settled.value;
  if (!resp.ok) return { devices: null, deviceError: audioErrorText(resp.error, resp.message) };
  if (!Array.isArray(resp.result)) {
    return { devices: null, deviceError: '拉取失败: 返回格式异常' };
  }
  return { devices: resp.result as AudioDeviceInfo[], deviceError: null };
}

function settleProcesses(
  settled: PromiseSettledResult<WsResponse>
): { processes: AudioProcessesResult | null; processError: string | null } {
  if (settled.status === 'rejected') {
    return { processes: null, processError: String(settled.reason) };
  }
  const resp = settled.value;
  if (!resp.ok) return { processes: null, processError: audioErrorText(resp.error, resp.message) };
  const raw = resp.result as Partial<AudioProcessesResult> | null | undefined;
  if (!raw || typeof raw !== 'object') {
    return { processes: null, processError: '拉取失败: 返回格式异常' };
  }
  const list = Array.isArray(raw.processes) ? raw.processes : [];
  return {
    processes: {
      supported: raw.supported === true,
      reason: typeof raw.reason === 'string' ? raw.reason : null,
      processes: list.filter(isProcessInfo)
    },
    processError: null
  };
}

function isProcessInfo(value: unknown): value is AudioProcessInfo {
  if (!value || typeof value !== 'object') return false;
  const p = value as Partial<AudioProcessInfo>;
  return typeof p.pid === 'number' && typeof p.name === 'string';
}

// ---------- 构造选项 ----------

export function buildAudioSourceList(data: AudioSourcesData): AudioSourceList {
  const devices = data.devices
    ? data.devices.filter((d) => d.is_loopback !== false).map((d) => deviceOption(d.id, d.name))
    : [];

  let apps: AudioSourceOption[] = [];
  let appsSupported = false;
  let appsHint: string | null = null;
  if (data.processes) {
    appsSupported = data.processes.supported;
    if (!appsSupported) {
      appsHint = AUDIO_APPS_UNSUPPORTED_HINT;
    } else {
      apps = data.processes.processes.map(processOption);
      if (apps.length === 0) appsHint = AUDIO_APPS_EMPTY_HINT;
    }
  }

  return {
    system: SYSTEM_AUDIO_OPTION,
    apps,
    devices,
    appsSupported,
    appsHint,
    appsError: data.processError,
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

function processOption(p: AudioProcessInfo): AudioSourceOption {
  return {
    key: encodeAudioSource({ kind: 'process', pid: p.pid, name: p.name }),
    label: p.ordinal !== null ? `${p.name} (${p.ordinal})` : p.name,
    source: { kind: 'process', pid: p.pid, name: p.name }
  };
}

// ---------- 选中匹配 ----------

/**
 * 偏好是否命中某候选：
 * - 设备：id 相等；
 * - 进程：名称相等，且 lastPid 已知时 PID 也必须相等（同名多实例精确区分）。
 */
export function isAudioOptionSelected(pref: AudioSourcePrefView, option: AudioSourceOption): boolean {
  const source = option.source;
  if (source.kind === 'device') {
    return pref.kind === 'device' && pref.id === source.id;
  }
  if (pref.kind !== 'process' || pref.name !== source.name) return false;
  return pref.lastPid == null || pref.lastPid === source.pid;
}

/** 仅含真实可选项 + 分组标题（disabled）；不支持/空/错误的说明由组件另行展示 */
export function toSelectOptions(list: AudioSourceList): AudioSelectOption[] {
  const options: AudioSelectOption[] = [
    { value: list.system.key, label: list.system.label }
  ];
  if (list.apps.length > 0) {
    options.push({ value: '__apps_header', label: AUDIO_APPS_TITLE, disabled: true });
    options.push(...list.apps.map((o) => ({ value: o.key, label: o.label })));
  }
  if (list.devices.length > 0) {
    options.push({ value: '__devices_header', label: AUDIO_DEVICES_TITLE, disabled: true });
    options.push(...list.devices.map((o) => ({ value: o.key, label: o.label })));
  }
  return options;
}

/**
 * Select 的 options + value。偏好不在列表（进程未运行 / PID 变化 / 设备断开）时，
 * 补一条当前值置顶，避免下拉误导性回落到首项。
 */
export function buildAudioSelectModel(list: AudioSourceList, pref: AudioSourcePrefView): AudioSelectModel {
  const all = [list.system, ...list.apps, ...list.devices];
  const hit = all.find((o) => isAudioOptionSelected(pref, o));
  const options = toSelectOptions(list);
  if (hit) return { options, value: hit.key };

  const value = encodeAudioPref(pref);
  return { options: [{ value, label: fallbackPrefLabel(pref, all) }, ...options], value };
}

function fallbackPrefLabel(pref: AudioSourcePrefView, all: AudioSourceOption[]): string {
  if (pref.kind === 'device') {
    if (pref.id === '') return AUDIO_SYSTEM_LABEL;
    const device = all.find((o) => o.source.kind === 'device' && o.source.id === pref.id);
    return device?.label ?? pref.id;
  }
  const app = all.find((o) => o.source.kind === 'process' && o.source.name === pref.name);
  return app?.label ?? pref.name;
}

// ---------- 胶囊标签 ----------

/** AppState.audioSource（显示串）→ 粗粒度偏好（胶囊面板无 cfg 时的选中回退） */
export function stateAudioPref(audioSource: string, list: AudioSourceList): AudioSourcePrefView {
  if (audioSource === '') return { kind: 'device', id: '' };
  const device = list.devices.find(
    (o) => o.source.kind === 'device' && o.source.id === audioSource
  );
  if (device) return { kind: 'device', id: audioSource };
  return { kind: 'process', name: audioSource, lastPid: null };
}

/** AppState.audioSource（显示串）→ 胶囊文案；列表未加载/未命中时回退原始串 */
export function resolveAudioSourceLabel(audioSource: string, list: AudioSourceList | null): string {
  if (audioSource === '') return '整个系统';
  if (list) {
    const device = list.devices.find(
      (o) => o.source.kind === 'device' && o.source.id === audioSource
    );
    if (device) return device.label;
    const app = list.apps.find(
      (o) => o.source.kind === 'process' && o.source.name === audioSource
    );
    if (app) return app.label;
  }
  return audioSource;
}
