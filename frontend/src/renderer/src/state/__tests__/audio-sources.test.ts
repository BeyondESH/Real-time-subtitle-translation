/**
 * 音频源共享 helper 单测（设备-only 回退后）：
 * 设备拉取与失败容错、选项构造（默认回环设备置顶/仅保留回环/空态提示）、
 * 选中匹配、值编解码、标签回退。
 *
 * 纯 node 环境；fetchAudioSources 的 request 以结构替身注入，不触碰 window。
 */
import { describe, it, expect } from 'vitest';
import {
  fetchAudioSources,
  buildAudioSourceList,
  buildAudioSelectModel,
  toSelectOptions,
  isAudioOptionSelected,
  encodeAudioSource,
  decodeAudioSource,
  encodeAudioPref,
  stateAudioPref,
  resolveAudioSourceLabel,
  AUDIO_DEVICES_EMPTY_HINT,
  type WsRequestFn,
  type AudioSourcesData,
  type AudioSourceOption
} from '../audio-sources';

function makeData(over: Partial<AudioSourcesData> = {}): AudioSourcesData {
  return { devices: [], deviceError: null, ...over };
}

describe('fetchAudioSources：设备拉取', () => {
  it('请求 get_audio_sources 并返回设备列表', async () => {
    const request: WsRequestFn = async () => ({
      ok: true, result: [{ id: 'd1', name: '扬声器', is_loopback: true }]
    });
    const out = await fetchAudioSources(request);
    expect(out.devices).toHaveLength(1);
    expect(out.deviceError).toBeNull();
  });

  it('not_connected / timeout → 既有文案', async () => {
    const notConnected: WsRequestFn = async () => ({ ok: false, error: 'not_connected', message: 'x' });
    expect((await fetchAudioSources(notConnected)).deviceError).toBe('后端未连接');

    const timeout: WsRequestFn = async () => ({ ok: false, error: 'timeout', message: 'slow' });
    expect((await fetchAudioSources(timeout)).deviceError).toBe('拉取超时，请确认后端已启动');
  });

  it('reject → 显式错误而非伪造数据', async () => {
    const request: WsRequestFn = () => Promise.reject(new Error('boom'));
    const out = await fetchAudioSources(request);
    expect(out.deviceError).toContain('boom');
    expect(out.devices).toBeNull();
  });

  it('返回格式异常 → 显式错误', async () => {
    const request: WsRequestFn = async () => ({ ok: true, result: 'nonsense' });
    const out = await fetchAudioSources(request);
    expect(out.devices).toBeNull();
    expect(out.deviceError).toContain('返回格式异常');
  });
});

describe('buildAudioSourceList：设备选项', () => {
  it('默认回环设备置顶、仅保留回环设备、label 为设备名', () => {
    const list = buildAudioSourceList(makeData({
      devices: [
        { id: '', name: '默认设备' },
        { id: 'd1', name: '扬声器一', is_loopback: true },
        { id: 'd2', name: '麦克风', is_loopback: false }
      ]
    }));
    expect(list.system.source).toEqual({ kind: 'device', id: '' });
    expect(list.devices.map((o) => o.source)).toEqual([
      { kind: 'device', id: '' },
      { kind: 'device', id: 'd1' }
    ]);
    expect(list.devices.map((o) => o.label)).toEqual(['默认设备', '扬声器一']);
  });

  it('无设备 → 空态提示；拉取失败 → devicesError 而非空态', () => {
    const empty = buildAudioSourceList(makeData());
    expect(empty.devices).toEqual([]);
    expect(empty.devicesHint).toBe(AUDIO_DEVICES_EMPTY_HINT);
    expect(empty.devicesError).toBeNull();

    const failed = buildAudioSourceList(makeData({ devices: null, deviceError: '后端未连接' }));
    expect(failed.devicesError).toBe('后端未连接');
    expect(failed.devicesHint).toBeNull();
  });

  it('toSelectOptions：默认回环设备 + 设备列表（无分组标题/禁用项）', () => {
    const list = buildAudioSourceList(makeData({
      devices: [{ id: 'd1', name: '扬声器', is_loopback: true }]
    }));
    const options = toSelectOptions(list);
    expect(options[0]).toEqual({ value: 'device:', label: list.system.label });
    expect(options.some((o) => o.value === 'device:d1')).toBe(true);
    expect(options.some((o) => o.disabled === true)).toBe(false);
  });
});

describe('选中匹配（isAudioOptionSelected）', () => {
  const deviceOption: AudioSourceOption = {
    key: 'device:d1', label: '扬声器', source: { kind: 'device', id: 'd1' }
  };

  it('设备：id 相等才选中', () => {
    expect(isAudioOptionSelected({ kind: 'device', id: 'd1' }, deviceOption)).toBe(true);
    expect(isAudioOptionSelected({ kind: 'device', id: 'd2' }, deviceOption)).toBe(false);
    expect(isAudioOptionSelected({ kind: 'device', id: '' }, deviceOption)).toBe(false);
  });
});

describe('值编解码', () => {
  it('device 往返一致；设备 id 允许包含冒号', () => {
    const cases = [
      { kind: 'device', id: '' },
      { kind: 'device', id: 'd1' },
      { kind: 'device', id: '{0.0.0.00000000}.{abc}' }
    ] as const;
    for (const source of cases) {
      expect(decodeAudioSource(encodeAudioSource(source))).toEqual(source);
    }
  });

  it('非法串（含回退前的 process 编码）返回 null', () => {
    expect(decodeAudioSource('bogus')).toBeNull();
    expect(decodeAudioSource('process:5:chrome.exe')).toBeNull();
    expect(decodeAudioSource('process:nope:x')).toBeNull();
  });

  it('encodeAudioPref：设备 id 直通', () => {
    expect(encodeAudioPref({ kind: 'device', id: 'd1' })).toBe('device:d1');
    expect(encodeAudioPref({ kind: 'device', id: '' })).toBe('device:');
  });
});

describe('buildAudioSelectModel / 标签', () => {
  it('命中列表时 value 指向对应可选项', () => {
    const list = buildAudioSourceList(makeData({
      devices: [{ id: 'd1', name: '扬声器', is_loopback: true }]
    }));
    const model = buildAudioSelectModel(list, { kind: 'device', id: 'd1' });
    expect(model.value).toBe('device:d1');
    expect(model.options.some((o) => o.value === 'device:d1')).toBe(true);
  });

  it('偏好不在列表（设备断开）→ 补当前值，避免误导性回落首项', () => {
    const list = buildAudioSourceList(makeData());
    const model = buildAudioSelectModel(list, { kind: 'device', id: 'gone-dev' });
    expect(model.value).toBe('device:gone-dev');
    expect(model.options[0]).toEqual({ value: 'device:gone-dev', label: 'gone-dev' });
  });

  it('stateAudioPref / resolveAudioSourceLabel', () => {
    const list = buildAudioSourceList(makeData({
      devices: [{ id: 'd1', name: '扬声器', is_loopback: true }]
    }));
    expect(stateAudioPref('', list)).toEqual({ kind: 'device', id: '' });
    expect(stateAudioPref('d1', list)).toEqual({ kind: 'device', id: 'd1' });
    // 列表未命中的显示串 → 回落默认设备（不误标别的项为选中）
    expect(stateAudioPref('unknown-dev', list)).toEqual({ kind: 'device', id: '' });
    expect(resolveAudioSourceLabel('', list)).toBe('默认回环设备');
    expect(resolveAudioSourceLabel('d1', list)).toBe('扬声器');
    expect(resolveAudioSourceLabel('unknown', null)).toBe('unknown');
  });
});
