import { useEffect, useState } from 'react';
import { Cpu, Languages, Mic } from 'lucide-react';
import { Pill, StatusDot, cx } from '../components/ui';
import { langLabel } from './lang-labels';

const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3'];

interface AudioSource {
  id: string;
  name: string;
  is_loopback?: boolean;
}

type PanelKind = 'audio' | 'model' | 'lang' | null;

function connectionPresentation(state: AppStateView): {
  status: 'ok' | 'warn' | 'danger' | 'idle' | 'paused';
  label: string;
} {
  if (state.connection === 'down') return { status: 'danger', label: '后端未连接' };
  if (state.connection === 'reconnecting') return { status: 'warn', label: '重连中' };
  if (state.connection === 'connecting') return { status: 'idle', label: '连接中' };
  if (state.capture === 'paused') return { status: 'paused', label: '已暂停' };
  return { status: 'ok', label: '运行中' };
}

/** 底部状态胶囊条（composer 形态：只读状态 + 就地切换面板） */
export function StatusPillBar({ state }: { state: AppStateView }) {
  const [panel, setPanel] = useState<PanelKind>(null);
  const [devices, setDevices] = useState<AudioSource[] | null>(null);
  const [deviceError, setDeviceError] = useState<string | null>(null);

  const conn = connectionPresentation(state);

  const loadDevices = (): void => {
    setDeviceError(null);
    window.appAPI
      .wsRequest('get_audio_sources')
      .then((resp) => {
        if (!resp.ok) {
          setDevices(null);
          setDeviceError(resp.error === 'not_connected' ? '后端未连接' : resp.message);
          return;
        }
        const list = resp.result as AudioSource[];
        setDevices(list.filter((s) => s.is_loopback !== false));
      })
      .catch((e: unknown) => {
        setDevices(null);
        setDeviceError(String(e));
      });
  };

  useEffect(() => {
    if (panel === 'audio') loadDevices();
  }, [panel]);

  const toggle = (kind: Exclude<PanelKind, null>): void => {
    setPanel((prev) => (prev === kind ? null : kind));
  };

  const audioName = state.audioSource === ''
    ? '默认回环设备'
    : devices?.find((d) => d.id === state.audioSource)?.name ?? state.audioSource;

  const panelClass = cx(
    'panel-enter absolute bottom-full left-0 z-40 mb-2 w-72 rounded-card border border-edge',
    'bg-sidebar p-2'
  );

  return (
    <div className="shrink-0 px-8 pb-4">
      <div className="relative flex items-center gap-2 rounded-card border border-edge bg-sidebar px-3 py-2">
        {/* 音频源 */}
        <span className="relative">
          <Pill icon={<Mic className="h-3.5 w-3.5" />} title="音频源" onClick={() => toggle('audio')}>
            <span className="max-w-40 truncate">{audioName}</span>
          </Pill>
          {panel === 'audio' && (
            <span className={panelClass}>
              {deviceError && (
                <span className="block px-2 py-2 text-xs text-danger">
                  {deviceError}
                  <button
                    type="button"
                    className="ml-2 underline"
                    onClick={loadDevices}
                  >
                    重试
                  </button>
                </span>
              )}
              {!deviceError && !devices && <span className="block px-2 py-2 text-xs text-secondary">加载中…</span>}
              {devices && (
                <span className="flex max-h-56 flex-col gap-0.5 overflow-y-auto">
                  <DeviceItem
                    label="默认回环设备"
                    selected={state.audioSource === ''}
                    onSelect={() => {
                      void window.appAPI.dispatch({ type: 'setAudioSource', id: '' });
                      setPanel(null);
                    }}
                  />
                  {devices.map((d) => (
                    <DeviceItem
                      key={d.id}
                      label={d.name}
                      selected={state.audioSource === d.id}
                      onSelect={() => {
                        void window.appAPI.dispatch({ type: 'setAudioSource', id: d.id });
                        setPanel(null);
                      }}
                    />
                  ))}
                </span>
              )}
            </span>
          )}
        </span>

        {/* 模型 */}
        <span className="relative">
          <Pill icon={<Cpu className="h-3.5 w-3.5" />} title="Whisper 模型" onClick={() => toggle('model')}>
            {state.model}
          </Pill>
          {panel === 'model' && (
            <span className={panelClass}>
              <span className="flex flex-col gap-0.5">
                {WHISPER_MODELS.map((m) => (
                  <DeviceItem
                    key={m}
                    label={m}
                    selected={state.model === m}
                    onSelect={() => {
                      void window.appAPI.dispatch({ type: 'setModel', model: m });
                      setPanel(null);
                    }}
                  />
                ))}
              </span>
            </span>
          )}
        </span>

        {/* 语言 */}
        <span className="relative">
          <Pill
            icon={<Languages className="h-3.5 w-3.5" />}
            title="目标语言"
            onClick={() => toggle('lang')}
          >
            {langLabel(state.activeLanguage)}
          </Pill>
          {panel === 'lang' && (
            <span className={panelClass}>
              <span className="flex flex-col gap-0.5">
                {state.targetLanguages.map((lang) => (
                  <DeviceItem
                    key={lang}
                    label={`${langLabel(lang)}（${lang}）`}
                    selected={state.activeLanguage === lang}
                    onSelect={() => {
                      void window.appAPI.dispatch({ type: 'setLanguage', language: lang });
                      setPanel(null);
                    }}
                  />
                ))}
              </span>
            </span>
          )}
        </span>

        {/* 连接状态 */}
        <span className="ml-auto pr-1">
          <StatusDot status={conn.status} label={conn.label} pulse={conn.status === 'idle'} />
        </span>

        {/* 面板外点击关闭 */}
        {panel && (
          <button
            type="button"
            aria-label="关闭面板"
            className="fixed inset-0 z-30 cursor-default"
            onClick={() => setPanel(null)}
          />
        )}
      </div>
    </div>
  );
}

function DeviceItem({
  label, selected, onSelect
}: {
  label: string;
  selected: boolean;
  onSelect(): void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cx(
        'block w-full truncate rounded-control px-2 py-1.5 text-left text-xs',
        'transition-colors duration-fast ease-app',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        selected ? 'bg-elevated text-accent' : 'text-secondary hover:bg-surface-hover hover:text-primary'
      )}
    >
      {selected ? '✓ ' : ''}
      {label}
    </button>
  );
}
