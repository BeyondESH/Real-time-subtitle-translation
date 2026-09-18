/**
 * 音频源共享 helper 单测（add-per-process-audio-capture D9 / D12）：
 * 并行拉取与单侧失败容错、分组选项构造（序号/不支持/空态）、选中匹配、值编解码。
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
  AUDIO_APPS_UNSUPPORTED_HINT,
  AUDIO_APPS_EMPTY_HINT,
  type WsRequestFn,
  type AudioSourcesData,
  type AudioProcessesResult,
  type AudioSourceOption
} from '../audio-sources';

type WsResponse = Awaited<ReturnType<WsRequestFn>>;

function procResult(over: Partial<AudioProcessesResult> = {}): AudioProcessesResult {
  return { supported: true, reason: null, processes: [], ...over };
}

function makeData(over: Partial<AudioSourcesData> = {}): AudioSourcesData {
  return { devices: [], deviceError: null, processes: procResult(), processError: null, ...over };
}

describe('fetchAudioSources：并行拉取', () => {
  it('两个接口在等待前均已发出（并行而非串行）', async () => {
    const calls: string[] = [];
    let releaseDevices!: (v: WsResponse) => void;
    const devicePromise = new Promise<WsResponse>((resolve) => {
      releaseDevices = resolve;
    });
    const request: WsRequestFn = (method) => {
      calls.push(method);
      if (method === 'get_audio_sources') return devicePromise;
      return Promise.resolve({ ok: true, result: { supported: true, reason: null, processes: [] } });
    };

    const pending = fetchAudioSources(request);
    expect(calls).toEqual(['get_audio_sources', 'get_audio_processes']);

    releaseDevices({ ok: true, result: [{ id: 'd1', name: '扬声器', is_loopback: true }] });
    const out = await pending;
    expect(out.devices).toHaveLength(1);
    expect(out.processes?.supported).toBe(true);
    expect(out.deviceError).toBeNull();
    expect(out.processError).toBeNull();
  });
});

describe('fetchAudioSources：单侧失败容错', () => {
  it('设备请求失败（not_connected）不影响应用列表', async () => {
    const request: WsRequestFn = async (method) => method === 'get_audio_sources'
      ? { ok: false, error: 'not_connected', message: 'x' }
      : {
          ok: true,
          result: {
            supported: true,
            reason: null,
            processes: [{ pid: 1, name: 'chrome.exe', active: true, ordinal: null }]
          }
        };
    const out = await fetchAudioSources(request);
    expect(out.deviceError).toBe('后端未连接');
    expect(out.devices).toBeNull();
    expect(out.processError).toBeNull();
    expect(out.processes?.processes).toHaveLength(1);
  });

  it('应用请求 reject 不影响设备列表', async () => {
    const request: WsRequestFn = (method) => method === 'get_audio_processes'
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ ok: true, result: [{ id: 'd1', name: '扬声器' }] });
    const out = await fetchAudioSources(request);
    expect(out.processError).toContain('boom');
    expect(out.processes).toBeNull();
    expect(out.devices).toEqual([{ id: 'd1', name: '扬声器' }]);
  });

  it('timeout 映射为既有文案', async () => {
    const request: WsRequestFn = async () => ({ ok: false, error: 'timeout', message: 'slow' });
    const out = await fetchAudioSources(request);
    expect(out.deviceError).toBe('拉取超时，请确认后端已启动');
    expect(out.processError).toBe('拉取超时，请确认后端已启动');
  });

  it('返回格式异常 → 显式错误而非伪造数据', async () => {
    const request: WsRequestFn = async () => ({ ok: true, result: 'nonsense' });
    const out = await fetchAudioSources(request);
    expect(out.devices).toBeNull();
    expect(out.processes).toBeNull();
    expect(out.deviceError).toContain('返回格式异常');
  });
});

describe('buildAudioSourceList：分组选项', () => {
  it('整个系统置顶、设备仅保留回环、label 为设备名', () => {
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

  it('同名多实例按 ordinal 加序号，唯一/无序号不加', () => {
    const list = buildAudioSourceList(makeData({
      processes: procResult({
        processes: [
          { pid: 1, name: 'chrome.exe', active: true, ordinal: 1 },
          { pid: 2, name: 'chrome.exe', active: true, ordinal: 2 },
          { pid: 3, name: 'vlc.exe', active: false, ordinal: null }
        ]
      })
    }));
    expect(list.apps.map((o) => o.label)).toEqual(['chrome.exe (1)', 'chrome.exe (2)', 'vlc.exe']);
    expect(list.apps[0].source).toEqual({ kind: 'process', pid: 1, name: 'chrome.exe' });
  });

  it('supported=false → 应用区隐藏并附不支持说明，不显示假数据', () => {
    const list = buildAudioSourceList(makeData({
      processes: procResult({ supported: false, reason: 'os_too_old' })
    }));
    expect(list.appsSupported).toBe(false);
    expect(list.apps).toEqual([]);
    expect(list.appsError).toBeNull();
    expect(list.appsHint).toBe(AUDIO_APPS_UNSUPPORTED_HINT);
  });

  it('supported=true 但无发声应用 → 非误导性空态提示', () => {
    const list = buildAudioSourceList(makeData());
    expect(list.apps).toEqual([]);
    expect(list.appsHint).toBe(AUDIO_APPS_EMPTY_HINT);
  });

  it('应用拉取失败 → appsError 而非 unsupported/空态', () => {
    const list = buildAudioSourceList(makeData({ processes: null, processError: '后端未连接' }));
    expect(list.appsError).toBe('后端未连接');
    expect(list.appsHint).toBeNull();
    expect(list.appsSupported).toBe(false);
  });

  it('toSelectOptions：分组标题 disabled、真实选项可点', () => {
    const list = buildAudioSourceList(makeData({
      devices: [{ id: 'd1', name: '扬声器', is_loopback: true }],
      processes: procResult({
        processes: [{ pid: 9, name: 'chrome.exe', active: true, ordinal: null }]
      })
    }));
    const options = toSelectOptions(list);
    expect(options[0]).toEqual({ value: 'device:', label: list.system.label });
    const appsHeader = options.find((o) => o.value === '__apps_header');
    const devicesHeader = options.find((o) => o.value === '__devices_header');
    expect(appsHeader?.disabled).toBe(true);
    expect(devicesHeader?.disabled).toBe(true);
    expect(options.some((o) => o.value === 'process:9:chrome.exe' && !o.disabled)).toBe(true);
  });
});

describe('选中匹配（isAudioOptionSelected）', () => {
  const deviceOption: AudioSourceOption = {
    key: 'device:d1', label: '扬声器', source: { kind: 'device', id: 'd1' }
  };
  const processOption: AudioSourceOption = {
    key: 'process:42:chrome.exe',
    label: 'chrome.exe',
    source: { kind: 'process', pid: 42, name: 'chrome.exe' }
  };

  it('设备：id 相等才选中', () => {
    expect(isAudioOptionSelected({ kind: 'device', id: 'd1' }, deviceOption)).toBe(true);
    expect(isAudioOptionSelected({ kind: 'device', id: 'd2' }, deviceOption)).toBe(false);
  });

  it('进程：名称相等且 lastPid 已知时 PID 必须一致', () => {
    expect(isAudioOptionSelected(
      { kind: 'process', name: 'chrome.exe', lastPid: 42 }, processOption
    )).toBe(true);
    expect(isAudioOptionSelected(
      { kind: 'process', name: 'chrome.exe', lastPid: 99 }, processOption
    )).toBe(false);
  });

  it('进程：lastPid=null 时仅凭名称匹配', () => {
    expect(isAudioOptionSelected(
      { kind: 'process', name: 'chrome.exe', lastPid: null }, processOption
    )).toBe(true);
    expect(isAudioOptionSelected(
      { kind: 'process', name: 'vlc.exe', lastPid: null }, processOption
    )).toBe(false);
  });

  it('类型不交叉匹配', () => {
    expect(isAudioOptionSelected({ kind: 'device', id: '42' }, processOption)).toBe(false);
    expect(isAudioOptionSelected(
      { kind: 'process', name: 'd1', lastPid: null }, deviceOption
    )).toBe(false);
  });
});

describe('值编解码', () => {
  it('device/process 往返一致；设备 id 允许包含冒号', () => {
    const cases = [
      { kind: 'device', id: '' },
      { kind: 'device', id: 'd1' },
      { kind: 'device', id: '{0.0.0.00000000}.{abc}' },
      { kind: 'process', pid: 1234, name: 'chrome.exe' }
    ] as const;
    for (const source of cases) {
      expect(decodeAudioSource(encodeAudioSource(source))).toEqual(source);
    }
  });

  it('非法串返回 null', () => {
    expect(decodeAudioSource('bogus')).toBeNull();
    expect(decodeAudioSource('process:nope:x')).toBeNull();
    expect(decodeAudioSource('process:5:')).toBeNull();
  });

  it('encodeAudioPref：lastPid 缺失以 -1 占位', () => {
    expect(encodeAudioPref({ kind: 'device', id: 'd1' })).toBe('device:d1');
    expect(encodeAudioPref({ kind: 'process', name: 'chrome.exe', lastPid: 7 }))
      .toBe('process:7:chrome.exe');
    expect(encodeAudioPref({ kind: 'process', name: 'chrome.exe', lastPid: null }))
      .toBe('process:-1:chrome.exe');
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

  it('偏好不在列表（进程未运行）→ 补当前值，避免误导性回落首项', () => {
    const list = buildAudioSourceList(makeData());
    const model = buildAudioSelectModel(list, {
      kind: 'process', name: 'chrome.exe', lastPid: null
    });
    expect(model.value).toBe('process:-1:chrome.exe');
    expect(model.options[0]).toEqual({ value: 'process:-1:chrome.exe', label: 'chrome.exe' });
  });

  it('stateAudioPref / resolveAudioSourceLabel', () => {
    const list = buildAudioSourceList(makeData({
      devices: [{ id: 'd1', name: '扬声器', is_loopback: true }],
      processes: procResult({
        processes: [{ pid: 1, name: 'chrome.exe', active: true, ordinal: 1 }]
      })
    }));
    expect(stateAudioPref('', list)).toEqual({ kind: 'device', id: '' });
    expect(stateAudioPref('d1', list)).toEqual({ kind: 'device', id: 'd1' });
    expect(stateAudioPref('chrome.exe', list)).toEqual({
      kind: 'process', name: 'chrome.exe', lastPid: null
    });
    expect(resolveAudioSourceLabel('', list)).toBe('整个系统');
    expect(resolveAudioSourceLabel('d1', list)).toBe('扬声器');
    expect(resolveAudioSourceLabel('chrome.exe', list)).toBe('chrome.exe (1)');
    expect(resolveAudioSourceLabel('unknown', null)).toBe('unknown');
  });
});
