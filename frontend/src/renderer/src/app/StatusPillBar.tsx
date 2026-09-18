import { useCallback, useEffect, useState } from 'react';
import { Cpu, Languages, Mic } from 'lucide-react';
import { Pill, StatusDot, cx } from '../components/ui';
import type { AudioSourceTarget } from '../../../shared/ipc-types';
import { langLabel } from './lang-labels';
import {
  AUDIO_APPS_EMPTY_HINT,
  AUDIO_APPS_TITLE,
  AUDIO_APPS_UNSUPPORTED_HINT,
  AUDIO_DEVICES_EMPTY_HINT,
  AUDIO_DEVICES_TITLE,
  AUDIO_POLL_MS,
  buildAudioSourceList,
  fetchAudioSources,
  isAudioOptionSelected,
  resolveAudioSourceLabel,
  stateAudioPref,
  type AudioSourceList,
  type AudioSourcesData
} from '../state/audio-sources';

const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3'];

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
export function StatusPillBar({
  state, cfg
}: {
  state: AppStateView;
  cfg?: AppConfigView | null;
}) {
  const [panel, setPanel] = useState<PanelKind>(null);
  const [audioData, setAudioData] = useState<AudioSourcesData | null>(null);
  const [audioLoading, setAudioLoading] = useState(false);

  const conn = connectionPresentation(state);

  const loadAudio = useCallback((): void => {
    setAudioLoading(true);
    void fetchAudioSources()
      .then(setAudioData)
      .catch((e: unknown) => {
        const text = String(e);
        setAudioData({ devices: null, deviceError: text, processes: null, processError: text });
      })
      .finally(() => setAudioLoading(false));
  }, []);

  // 面板打开：拉取一次；保持打开期间每 3s 轻量轮询；关闭/切面板时清理
  useEffect(() => {
    if (panel !== 'audio') return;
    setAudioData(null);
    loadAudio();
    const timer = window.setInterval(loadAudio, AUDIO_POLL_MS);
    return () => window.clearInterval(timer);
  }, [panel, loadAudio]);

  const toggle = (kind: Exclude<PanelKind, null>): void => {
    setPanel((prev) => (prev === kind ? null : kind));
  };

  const selectSource = (source: AudioSourceTarget): void => {
    void window.appAPI.dispatch({ type: 'setAudioSource', source });
    setPanel(null);
  };

  const list: AudioSourceList | null = audioData ? buildAudioSourceList(audioData) : null;
  const pref: AudioSourcePrefView | null = cfg?.audio.source
    ?? (list ? stateAudioPref(state.audioSource, list) : null);
  const audioName = resolveAudioSourceLabel(state.audioSource, list);

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
              {audioLoading && !audioData && (
                <HintRow>加载中…</HintRow>
              )}
              {audioData && list && pref && (
                <span className="flex max-h-72 flex-col gap-0.5 overflow-y-auto">
                  <DeviceItem
                    label={list.system.label}
                    selected={isAudioOptionSelected(pref, list.system)}
                    onSelect={() => selectSource(list.system.source)}
                  />

                  {/* 应用进程（supported=false 隐藏，附说明） */}
                  {audioData.processError && (
                    <ErrorRow text={`应用：${audioData.processError}`} onRetry={loadAudio} />
                  )}
                  {!audioData.processError && list.appsSupported && (
                    <>
                      <SectionLabel>{AUDIO_APPS_TITLE}</SectionLabel>
                      {list.apps.length === 0
                        ? <HintRow>{AUDIO_APPS_EMPTY_HINT}</HintRow>
                        : list.apps.map((o) => (
                          <DeviceItem
                            key={o.key}
                            label={o.label}
                            selected={isAudioOptionSelected(pref, o)}
                            onSelect={() => selectSource(o.source)}
                          />
                        ))}
                    </>
                  )}
                  {!audioData.processError && !list.appsSupported && (
                    <HintRow>{AUDIO_APPS_UNSUPPORTED_HINT}</HintRow>
                  )}

                  {/* 回环设备 */}
                  {audioData.deviceError && (
                    <ErrorRow text={`设备：${audioData.deviceError}`} onRetry={loadAudio} />
                  )}
                  {!audioData.deviceError && (
                    <>
                      <SectionLabel>{AUDIO_DEVICES_TITLE}</SectionLabel>
                      {list.devices.length === 0
                        ? <HintRow>{AUDIO_DEVICES_EMPTY_HINT}</HintRow>
                        : list.devices.map((o) => (
                          <DeviceItem
                            key={o.key}
                            label={o.label}
                            selected={isAudioOptionSelected(pref, o)}
                            onSelect={() => selectSource(o.source)}
                          />
                        ))}
                    </>
                  )}
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

function SectionLabel({ children }: { children: string }) {
  return (
    <span className="block px-2 pb-0.5 pt-2 text-[11px] text-secondary opacity-60">{children}</span>
  );
}

function HintRow({ children }: { children: string }) {
  return <span className="block px-2 py-1.5 text-xs text-secondary opacity-60">{children}</span>;
}

function ErrorRow({ text, onRetry }: { text: string; onRetry(): void }) {
  return (
    <span className="block px-2 py-2 text-xs text-danger">
      {text}
      <button type="button" className="ml-2 underline" onClick={onRetry}>
        重试
      </button>
    </span>
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
