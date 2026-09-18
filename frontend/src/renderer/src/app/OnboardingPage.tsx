import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Captions, Check, SkipForward } from 'lucide-react';
import { Button, ProgressBar, Select } from '../components/ui';
import { useAppState } from '../state/hooks';
import {
  AUDIO_APPS_EMPTY_HINT,
  AUDIO_APPS_UNSUPPORTED_HINT,
  buildAudioSourceList,
  decodeAudioSource,
  fetchAudioSources,
  toSelectOptions,
  type AudioSourcesData
} from '../state/audio-sources';
import { DRAG_REGION } from './TitleBar';

type Step = 'welcome' | 'audio' | 'download' | 'done';

/**
 * 首次运行引导（main-window spec）：欢迎 → 音频源 → 模型下载 → 完成。
 * 可跳过：下载转后台，进度经直播流占位态呈现。
 */
export function OnboardingPage() {
  const navigate = useNavigate();
  const state = useAppState();

  const [step, setStep] = useState<Step>('welcome');
  const [audioData, setAudioData] = useState<AudioSourcesData | null>(null);
  const [audioHint, setAudioHint] = useState('加载中…');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [seenDownload, setSeenDownload] = useState(false);

  useEffect(() => {
    if (state?.modelDownload) setSeenDownload(true);
  }, [state?.modelDownload]);

  useEffect(() => {
    if (step !== 'audio') return;
    let alive = true;
    setAudioHint('加载中…');
    fetchAudioSources()
      .then((data) => {
        if (!alive) return;
        setAudioData(data);
        const list = buildAudioSourceList(data);
        if (data.deviceError && data.processError) {
          setAudioHint('列表暂不可用（后端未就绪/未连接），可稍后在设置中调整');
        } else if (data.processError) {
          setAudioHint(`应用列表暂不可用：${data.processError}，可稍后在设置中调整`);
        } else if (data.deviceError) {
          setAudioHint(`设备列表暂不可用：${data.deviceError}，可稍后在设置中调整`);
        } else if (!list.appsSupported) {
          setAudioHint('当前系统仅支持"整个系统"回环捕获');
        } else {
          setAudioHint(
            `发现 ${list.apps.length} 个正在发声的应用、${list.devices.length} 个回环设备`
          );
        }
      })
      .catch((e: unknown) => {
        if (alive) setAudioHint(`列表暂不可用：${String(e)}，可稍后在设置中调整`);
      });
    return () => {
      alive = false;
    };
  }, [step]);

  // 下载页自动前进：见过进度且当前无下载、后端已连
  useEffect(() => {
    if (
      step === 'download'
      && seenDownload
      && state?.connection === 'open'
      && !state.modelDownload
    ) {
      setStep('done');
    }
  }, [step, seenDownload, state?.connection, state?.modelDownload]);

  const finish = (): void => {
    void window.appAPI.setConfig('onboarding.completed', true);
    navigate('/live', { replace: true });
  };

  const skip = (
    <Button
      size="sm"
      variant="ghost"
      icon={<SkipForward className="h-3.5 w-3.5" />}
      onClick={finish}
    >
      跳过引导
    </Button>
  );

  const audioList = audioData ? buildAudioSourceList(audioData) : null;

  return (
    <div className="flex h-screen flex-col bg-base pt-9">
      {/* WCO 标题栏留白区（可拖动） */}
      <div className="h-0 shrink-0" style={DRAG_REGION} />

      <div className="flex min-h-0 flex-1 items-center justify-center p-8">
        <div className="w-full max-w-lg rounded-card border border-edge bg-sidebar p-10">
          {step === 'welcome' && (
            <div className="flex flex-col items-center gap-4 text-center">
              <Captions className="h-10 w-10 text-accent" />
              <h1 className="text-xl font-semibold text-primary">欢迎使用实时字幕翻译</h1>
              <p className="text-base leading-relaxed text-secondary">
                捕获系统音频，实时语音识别并翻译，
                <br />
                以透明悬浮字幕呈现，完全离线可用。
              </p>
              <Button variant="primary" className="mt-4" onClick={() => setStep('audio')}>
                开始设置
              </Button>
            </div>
          )}

          {step === 'audio' && (
            <div className="flex flex-col gap-4">
              <h1 className="text-xl font-semibold text-primary">选择音频源</h1>
              <p className="text-base text-secondary">
                字幕来自"回环捕获"——你听到的声音就是字幕的输入。默认"整个系统"即可用于绝大多数场景；
                也可只捕获某个应用的声音。
              </p>
              <Select
                options={audioList ? toSelectOptions(audioList) : []}
                value={selectedKey ?? audioList?.system.key ?? ''}
                onChange={setSelectedKey}
                disabled={audioList === null}
              />
              {audioList && !audioData?.processError && !audioList.appsSupported && (
                <p className="text-xs text-secondary opacity-60">{AUDIO_APPS_UNSUPPORTED_HINT}</p>
              )}
              {audioList && !audioData?.processError && audioList.appsSupported
                && audioList.apps.length === 0 && (
                <p className="text-xs text-secondary opacity-60">{AUDIO_APPS_EMPTY_HINT}</p>
              )}
              <p className="text-xs text-secondary opacity-60">{audioHint || ' '}</p>
              <div className="mt-2 flex items-center justify-between">
                {skip}
                <Button
                  variant="primary"
                  onClick={() => {
                    // 仅当选择了非默认源时下发；默认"整个系统"保持后端默认
                    if (selectedKey && audioList && selectedKey !== audioList.system.key) {
                      const source = decodeAudioSource(selectedKey);
                      if (source) {
                        void window.appAPI.dispatch({ type: 'setAudioSource', source });
                      }
                    }
                    setStep('download');
                  }}
                >
                  下一步
                </Button>
              </div>
            </div>
          )}

          {step === 'download' && (
            <div className="flex flex-col gap-4">
              <h1 className="text-xl font-semibold text-primary">模型准备</h1>
              {state?.modelDownload ? (
                <>
                  <ProgressBar
                    value={state.modelDownload.progress}
                    label={state.modelDownload.name}
                  />
                  <p className="text-xs text-secondary">{state.modelDownload.message}</p>
                </>
              ) : state?.connection === 'open' ? (
                <p className="flex items-center gap-2 text-base text-ok">
                  <Check className="h-4 w-4" />
                  模型已就绪（本机已有缓存）
                </p>
              ) : (
                <p className="text-base text-secondary">
                  正在启动后端服务…（首次运行需下载识别与翻译模型，约 400MB）
                </p>
              )}
              <div className="mt-2 flex items-center justify-between">
                {skip}
                <Button
                  variant="primary"
                  disabled={state?.connection !== 'open' || Boolean(state?.modelDownload)}
                  onClick={() => setStep('done')}
                >
                  {state?.modelDownload ? '下载中…' : '下一步'}
                </Button>
              </div>
            </div>
          )}

          {step === 'done' && (
            <div className="flex flex-col items-center gap-4 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-pill bg-accent">
                <Check className="h-6 w-6 text-onaccent" />
              </span>
              <h1 className="text-xl font-semibold text-primary">一切就绪</h1>
              <p className="text-base text-secondary">
                播放任意含语音的音频，字幕将自动出现。
                <br />
                样式、语言、模型随时可在设置中调整。
              </p>
              <Button variant="primary" className="mt-4" onClick={finish}>
                进入主界面
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
