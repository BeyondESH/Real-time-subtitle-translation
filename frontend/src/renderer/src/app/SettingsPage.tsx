import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { FolderOpen, FileText, RefreshCw } from 'lucide-react';
import {
  Button, Modal, Pill, ProgressBar, SegmentedNav, Select, Slider, StatusDot, Toggle
} from '../components/ui';
import { useAppConfig, useAppState, useEnv } from '../state/hooks';
import {
  buildAudioSelectModel,
  buildAudioSourceList,
  decodeAudioSource,
  fetchAudioSources,
  type AudioSourcesData
} from '../state/audio-sources';
import {
  buildTranslationSelectOptions,
  fetchTranslationModels,
  translationModelStatusText,
  type TranslationModelsData
} from '../state/translation-models';
import { ToastHost } from './ToastHost';
import {
  ENGINE_MODEL_DISPLAY, SOURCE_LANGUAGE_LABELS, SOURCE_LANGUAGES
} from '../../../shared/ipc-types';

const SECTIONS = [
  { value: 'general', label: '通用' },
  { value: 'subtitle', label: '字幕外观' },
  { value: 'audio', label: '音频' },
  { value: 'model', label: '模型' },
  { value: 'shortcuts', label: '快捷键' },
  { value: 'advanced', label: '高级' }
];

const SOURCE_LANGUAGE_OPTIONS = SOURCE_LANGUAGES.map((code) => ({
  value: code,
  label: `${SOURCE_LANGUAGE_LABELS[code]}（${code}）`
}));

const FONT_OPTIONS = [
  { value: 'Microsoft YaHei', label: '微软雅黑' },
  { value: 'SimHei', label: '黑体' },
  { value: 'SimSun', label: '宋体' },
  { value: 'KaiTi', label: '楷体' }
];

const DISPLAY_MODES = [
  { value: 'original_and_translation', label: '原文 + 译文' },
  { value: 'translation_only', label: '仅译文' }
];

const DEVICE_OPTIONS = [
  { value: 'auto', label: '自动（检测到 GPU 时使用）' },
  { value: 'cpu', label: 'CPU' },
  { value: 'cuda', label: 'GPU' }
];

/**
 * 实际设备 + 原因 → 状态文案（settings-management spec「推理设备设置与状态显示」）。
 * resolved=null/缺失 → 检测中；MUST NOT 以配置偏好冒充实际设备。
 */
export function deviceStatusText(device: AppStateView['device'] | undefined): string {
  const asr = device?.asr;
  if (!asr || asr.resolved === null) return '正在检测推理设备…';
  if (asr.resolved === 'cuda') {
    return asr.reason === 'user' ? 'GPU（用户指定）' : 'GPU（CUDA 自动检测）';
  }
  switch (asr.reason) {
    case 'no_cuda':
      return 'CPU（未检测到兼容的 CUDA 环境）';
    case 'load_failed':
      return 'CPU（GPU 加载失败，已自动降级）';
    case 'runtime_failed':
      return 'CPU（GPU 运行时不可用，已自动降级）';
    default:
      return 'CPU（用户指定）';
  }
}

const THEME_ITEMS = [
  { value: 'dark', label: '暗色' },
  { value: 'light', label: '亮色' },
  { value: 'system', label: '跟随系统' }
];

/** 设置页（settings-management spec：六分段、改动即落盘、深链定位） */
export function SettingsPage() {
  const { section } = useParams();
  const navigate = useNavigate();
  const cfg = useAppConfig();
  const state = useAppState();
  const env = useEnv();

  const current = SECTIONS.some((s) => s.value === section) ? section! : 'general';

  if (!cfg) {
    return <div className="flex h-full items-center justify-center text-secondary">加载中…</div>;
  }

  return (
    <div className="flex h-full">
      <div className="w-52 shrink-0 border-r border-edge bg-sidebar p-3">
        <h1 className="px-3 pb-3 text-base font-semibold text-primary">设置</h1>
        <SegmentedNav
          direction="vertical"
          items={SECTIONS}
          value={current}
          onChange={(v) => navigate(`/settings/${v}`)}
        />
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto px-8 py-6">
        {current === 'general' && <GeneralSection cfg={cfg} />}
        {current === 'subtitle' && <SubtitleSection cfg={cfg} supportsAcrylic={env?.supportsAcrylic ?? false} />}
        {current === 'audio' && <AudioSection cfg={cfg} />}
        {current === 'model' && <ModelSection state={state} cfg={cfg} />}
        {current === 'shortcuts' && <ShortcutsSection cfg={cfg} />}
        {current === 'advanced' && <AdvancedSection version={env?.version ?? ''} />}
        <ToastHost />
      </div>
    </div>
  );
}

function setCfg(path: string, value: unknown): void {
  void window.appAPI.setConfig(path, value);
}

function SectionTitle({ children }: { children: string }) {
  return <h2 className="mb-4 text-lg font-semibold text-primary">{children}</h2>;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-5 flex items-center justify-between gap-6">
      <span className="shrink-0 text-base text-primary">{label}</span>
      <div className="flex min-w-0 flex-1 justify-end">{children}</div>
    </div>
  );
}

// ---------- 通用 ----------

function GeneralSection({ cfg }: { cfg: AppConfigView }) {
  const [updateEvent, setUpdateEvent] = useState<UpdateEventView | null>(null);

  useEffect(
    () => window.appAPI.onUpdate((e) => setUpdateEvent(e)),
    []
  );

  const checkNow = async (): Promise<void> => {
    setUpdateEvent({ phase: 'checking' });
    const r = await window.appAPI.checkUpdate();
    if (r.phase === 'not-available' || r.phase === 'error') {
      setUpdateEvent(r);
    }
  };

  const updateHint = ((): ReactNode => {
    if (!updateEvent) return null;
    switch (updateEvent.phase) {
      case 'checking':
        return <span className="text-xs text-secondary">正在检查更新…</span>;
      case 'available':
        return (
          <span className="text-xs text-secondary">
            发现新版本 {updateEvent.version}，后台下载中…
          </span>
        );
      case 'not-available':
        return <span className="text-xs text-ok">已是最新版本</span>;
      case 'downloading':
        return (
          <span className="w-48">
            <ProgressBar
              value={updateEvent.percent ?? 0}
              label={`下载新版本 ${updateEvent.version ?? ''}`}
            />
          </span>
        );
      case 'downloaded':
        return (
          <span className="flex items-center gap-2">
            <span className="text-xs text-ok">新版本 {updateEvent.version} 已下载</span>
            <Button
              size="sm"
              variant="primary"
              onClick={() => void window.appAPI.quitAndInstall()}
            >
              立即重启安装
            </Button>
          </span>
        );
      case 'error':
        return (
          <span className="max-w-72 text-xs text-danger">
            检查失败：{updateEvent.error}（可稍后重试）
          </span>
        );
      default:
        return null;
    }
  })();

  return (
    <div className="max-w-xl">
      <SectionTitle>通用</SectionTitle>
      <Row label="主题">
        <SegmentedNav
          items={THEME_ITEMS}
          value={cfg.theme}
          onChange={(v) => setCfg('theme', v)}
        />
      </Row>
      <Row label="开机自启">
        <Toggle
          checked={cfg.system.autoStart}
          onChange={(v) => setCfg('system.autoStart', v)}
        />
      </Row>
      <Row label="检查更新">
        <span className="flex items-center gap-3">
          {updateHint}
          <Button size="sm" onClick={() => void checkNow()}>
            检查更新
          </Button>
        </span>
      </Row>
    </div>
  );
}

// ---------- 显示器选择 ----------

function DisplaySelect({ cfg }: { cfg: AppConfigView }) {
  const [displays, setDisplays] = useState<DisplayView[]>([]);

  useEffect(() => {
    window.appAPI
      .getDisplays()
      .then(setDisplays)
      .catch((e: unknown) => console.error('显示器枚举失败', e));
  }, []);

  const current = cfg.window.displayId === null || cfg.window.displayId === undefined
    ? ''
    : String(cfg.window.displayId);

  return (
    <span className="w-64">
      <Select
        options={[
          { value: '', label: '主显示器（默认）' },
          ...displays.map((d) => ({ value: String(d.id), label: d.label }))
        ]}
        value={current}
        onChange={(v) => setCfg('window.displayId', v === '' ? null : Number(v))}
      />
    </span>
  );
}

// ---------- 字幕外观 ----------

function SubtitleSection({
  cfg, supportsAcrylic
}: {
  cfg: AppConfigView;
  supportsAcrylic: boolean;
}) {
  const s = cfg.subtitle;
  const previewStyle: CSSProperties = {
    fontFamily: s.fontFamily,
    fontSize: `${s.fontSize}px`,
    color: s.fontColor,
    textShadow: `${s.strokeWidth}px ${s.strokeWidth}px 4px ${s.strokeColor}`
  };

  return (
    <div className="max-w-xl">
      <SectionTitle>字幕外观</SectionTitle>

      <Row label="悬浮窗显示器">
        <DisplaySelect cfg={cfg} />
      </Row>

      <Row label="外观预设">
        <span className="flex items-center gap-2">
          <Pill active={s.preset === 'text'} onClick={() => setCfg('subtitle.preset', 'text')}>
            纯文字
          </Pill>
          <span title={supportsAcrylic ? undefined : '需要 Windows 11'}>
            <Pill
              active={s.preset === 'acrylic'}
              onClick={supportsAcrylic ? () => setCfg('subtitle.preset', 'acrylic') : undefined}
            >
              毛玻璃胶囊
            </Pill>
          </span>
          {!supportsAcrylic && <span className="text-xs text-secondary opacity-60">需要 Windows 11</span>}
        </span>
      </Row>

      <Row label="显示模式">
        <span className="w-56">
          <Select
            options={DISPLAY_MODES}
            value={s.displayMode}
            onChange={(v) => setCfg('subtitle.displayMode', v)}
          />
        </span>
      </Row>

      <Row label="字体">
        <span className="w-56">
          <Select
            options={FONT_OPTIONS}
            value={s.fontFamily}
            onChange={(v) => setCfg('subtitle.fontFamily', v)}
          />
        </span>
      </Row>

      <div className="mb-5">
        <Slider
          min={12}
          max={72}
          value={s.fontSize}
          onChange={(v) => setCfg('subtitle.fontSize', v)}
          label="字号"
        />
      </div>

      <div className="mb-5">
        <Slider
          min={0}
          max={10}
          value={s.strokeWidth}
          onChange={(v) => setCfg('subtitle.strokeWidth', v)}
          label="描边宽度"
        />
      </div>

      <Row label="文字颜色">
        <ColorInput value={s.fontColor} onChange={(v) => setCfg('subtitle.fontColor', v)} />
      </Row>

      <Row label="描边颜色">
        <ColorInput value={s.strokeColor} onChange={(v) => setCfg('subtitle.strokeColor', v)} />
      </Row>

      <div className="mb-5">
        <Slider
          min={30}
          max={100}
          value={Math.round(cfg.window.opacity * 100)}
          onChange={(v) => setCfg('window.opacity', v / 100)}
          label="悬浮窗不透明度（%）"
        />
      </div>

      <div className="rounded-card border border-edge bg-sidebar p-6 text-center">
        <div style={previewStyle}>今天天气真好啊</div>
      </div>
    </div>
  );
}

function ColorInput({ value, onChange }: { value: string; onChange(v: string): void }) {
  return (
    <span className="flex items-center gap-2">
      <span className="font-mono text-xs text-secondary">{value}</span>
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 w-14 cursor-pointer rounded-control border border-edge bg-elevated p-1"
      />
    </span>
  );
}

// ---------- 音频 ----------

/** 音频分段：回环设备选择（settings-management spec：实时拉取、失败可重试） */
export function AudioSection({ cfg }: { cfg: AppConfigView }) {
  const [data, setData] = useState<AudioSourcesData | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('loading');
  const [errorText, setErrorText] = useState('');

  const load = (): void => {
    setStatus('loading');
    fetchAudioSources()
      .then((d) => {
        setData(d);
        setStatus('idle');
      })
      .catch((e: unknown) => {
        setData(null);
        setErrorText(String(e));
        setStatus('error');
      });
  };

  // 打开分段即拉取一次（settings-management spec：实时拉取）
  useEffect(() => {
    load();
  }, []);

  const list = data ? buildAudioSourceList(data) : null;
  const model = list ? buildAudioSelectModel(list, cfg.audio.source) : null;

  const onChange = (value: string): void => {
    const source = decodeAudioSource(value);
    if (source) void window.appAPI.dispatch({ type: 'setAudioSource', source });
  };

  const retry = (
    <button type="button" className="ml-2 underline" onClick={load}>
      重试
    </button>
  );

  return (
    <div className="max-w-xl">
      <SectionTitle>音频</SectionTitle>
      <Row label="音频源（回环设备）">
        <span className="flex w-full items-center gap-2">
          <span className="min-w-0 flex-1">
            <Select
              options={model?.options ?? []}
              value={model?.value ?? ''}
              onChange={onChange}
              disabled={status !== 'idle' || model === null}
            />
          </span>
          <Button
            size="sm"
            variant="ghost"
            icon={<RefreshCw className="h-3.5 w-3.5" />}
            onClick={load}
          >
            刷新
          </Button>
        </span>
      </Row>
      {status === 'loading' && <p className="text-xs text-secondary">加载中…</p>}
      {status === 'error' && <p className="text-xs text-danger">{errorText}{retry}</p>}
      {status === 'idle' && data && list && (
        <>
          {data.deviceError && (
            <p className="text-xs text-danger">设备列表：{data.deviceError}{retry}</p>
          )}
          {!data.deviceError && (
            <p className="text-xs text-secondary">发现 {list.devices.length} 个回环设备</p>
          )}
        </>
      )}
      {status === 'idle' && !data && (
        <p className="text-xs text-secondary opacity-60">点击"刷新"从后端拉取设备列表</p>
      )}
    </div>
  );
}

// ---------- 模型 ----------

/**
 * 翻译模型区块（settings-management spec「翻译模型设置与状态显示」）：
 * 当前实际模型 + 注册表可选模型（体积/下载状态）；选择即派发 changeLlm Intent；
 * 失败回退与错误提示由 Controller 的乐观回退链路负责。
 */
export function TranslationModelRow({ cfg }: { cfg: AppConfigView }) {
  const [data, setData] = useState<TranslationModelsData | null>(null);
  const [status, setStatus] = useState<'loading' | 'idle' | 'error'>('loading');
  const storeModel = cfg.translation.model;

  useEffect(() => {
    setStatus('loading');
    fetchTranslationModels()
      .then((d) => {
        setData(d);
        setStatus('idle');
      })
      .catch(() => {
        setData(null);
        setStatus('error');
      });
  }, []);

  return (
    <>
      <Row label="翻译模型">
        <span className="w-56">
          <Select
            options={buildTranslationSelectOptions(data, storeModel)}
            value={storeModel}
            onChange={(v) => void window.appAPI.dispatch({ type: 'setLlm', modelId: v })}
          />
        </span>
      </Row>
      <p className="mb-5 -mt-3 text-xs text-secondary">
        {status === 'error'
          ? '翻译模型信息拉取失败（后端未连接）'
          : translationModelStatusText(data, storeModel)}
      </p>
      <p className="mb-5 text-xs text-secondary opacity-60">
        切换立即生效；未下载的模型首次使用会自动下载，进度见下方。
      </p>
    </>
  );
}

export function ModelSection({ state, cfg }: { state: AppStateView | null; cfg: AppConfigView }) {
  const device = cfg.inference?.device ?? 'auto';
  return (
    <div className="max-w-xl">
      <SectionTitle>模型</SectionTitle>
      <Row label="识别引擎">
        <span className="w-56 text-sm text-primary">{ENGINE_MODEL_DISPLAY}</span>
      </Row>
      <p className="mb-5 -mt-3 text-xs text-secondary opacity-60">
        单引擎模型（sherpa-onnx，Fun-ASR-Nano INT8）；首次使用自动下载（约 948MB），进度见下方与直播流页。
      </p>
      <Row label="源语言">
        <span className="w-56">
          <Select
            options={SOURCE_LANGUAGE_OPTIONS}
            value={cfg.asr.language}
            onChange={(v) => void window.appAPI.dispatch({
              type: 'setSourceLanguage', language: v as 'ja' | 'zh' | 'en'
            })}
          />
        </span>
      </Row>
      <p className="mb-5 -mt-3 text-xs text-secondary">
        识别与翻译的源语言，支持日语/中文/英文；切换热生效，失败自动回退并提示。
      </p>
      <TranslationModelRow cfg={cfg} />
      <Row label="推理设备">
        <span className="w-56">
          <Select
            options={DEVICE_OPTIONS}
            value={device}
            onChange={(v) => void window.appAPI.dispatch({ type: 'setDevice', device: v })}
          />
        </span>
      </Row>
      <p className="mb-5 -mt-3 text-xs text-secondary">
        当前使用：{deviceStatusText(state?.device)}
      </p>
      <p className="mb-5 text-xs text-secondary opacity-60">
        切换设备将重新加载模型，期间字幕可能短暂延迟。
      </p>
      {state?.modelDownload && (
        <div className="max-w-md">
          <ProgressBar
            value={state.modelDownload.progress}
            label={`${state.modelDownload.name} · ${state.modelDownload.message}`}
          />
        </div>
      )}
    </div>
  );
}

// ---------- 快捷键 ----------

const DEFAULT_SHORTCUTS = {
  togglePause: 'Ctrl+Shift+Space',
  switchLanguage: 'Ctrl+Shift+L',
  toggleLock: 'Ctrl+Shift+D'
};

const KEY_MAP: Record<string, string> = {
  ' ': 'Space',
  ArrowUp: 'Up',
  ArrowDown: 'Down',
  ArrowLeft: 'Left',
  ArrowRight: 'Right',
  Delete: 'Delete',
  Insert: 'Insert',
  Home: 'Home',
  End: 'End',
  PageUp: 'PageUp',
  PageDown: 'PageDown',
  Enter: 'Enter',
  Tab: 'Tab',
  Minus: '-',
  Equal: '=',
  BracketLeft: '[',
  BracketRight: ']',
  Semicolon: ';',
  Quote: "'",
  Comma: ',',
  Period: '.',
  Slash: '/',
  Backslash: '\\'
};

function normalizeKey(key: string): string | null {
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(key)) return key;
  if (key.length === 1) return key.toUpperCase();
  return KEY_MAP[key] ?? null;
}

type ShortcutField = keyof typeof DEFAULT_SHORTCUTS;

function ShortcutsSection({ cfg }: { cfg: AppConfigView }) {
  const [capturing, setCapturing] = useState<ShortcutField | null>(null);
  const [hint, setHint] = useState('');

  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent): void => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === 'Escape') {
        setCapturing(null);
        setHint('');
        return;
      }
      const mods: string[] = [];
      if (e.ctrlKey) mods.push('Ctrl');
      if (e.altKey) mods.push('Alt');
      if (e.shiftKey) mods.push('Shift');
      if (e.metaKey) mods.push('Super');
      const key = normalizeKey(e.key);
      if (!key) return; // 修饰键本身或不可映射键，继续等待
      if (mods.length === 0 && !/^F\d+$/.test(key)) {
        setHint('需要至少一个修饰键（Ctrl/Alt/Shift）或 F 功能键');
        return;
      }
      const accel = [...mods, key].join('+');
      void window.appAPI.setConfig('shortcuts', { ...cfg.shortcuts, [capturing]: accel });
      setCapturing(null);
      setHint(`已设为 ${accel}，注册结果见对应状态点`);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [capturing, cfg.shortcuts]);

  const rows: Array<[ShortcutField, string]> = [
    ['togglePause', '暂停/恢复'],
    ['switchLanguage', '切换语言'],
    ['toggleLock', '锁定/解锁窗口']
  ];

  return (
    <div className="max-w-xl">
      <SectionTitle>快捷键</SectionTitle>
      <div className="rounded-card border border-edge">
        {rows.map(([field, label]) => {
          const ok = cfg.shortcutStatus[field];
          const isCapturing = capturing === field;
          return (
            <div
              key={field}
              className="flex items-center justify-between border-b border-edge px-4 py-3 last:border-b-0"
            >
              <span className="flex items-center gap-3">
                <span className="text-base text-primary">{label}</span>
                {!ok && !isCapturing && (
                  <span className="flex items-center gap-1 text-xs text-warn">
                    <StatusDot status="warn" />
                    注册失败（可能被占用）
                  </span>
                )}
              </span>
              <span className="flex items-center gap-2">
                {isCapturing ? (
                  <span className="rounded-control border border-accent px-2.5 py-1 text-xs text-accent">
                    按下新快捷键…（Esc 取消）
                  </span>
                ) : (
                  <span className="rounded-control border border-edge bg-elevated px-2.5 py-1 font-mono text-xs text-secondary">
                    {cfg.shortcuts[field]}
                  </span>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setHint('');
                    setCapturing(isCapturing ? null : field);
                  }}
                >
                  {isCapturing ? '取消' : '修改'}
                </Button>
              </span>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center gap-4">
        <Button
          size="sm"
          onClick={() => void window.appAPI.setConfig('shortcuts', { ...DEFAULT_SHORTCUTS })}
        >
          恢复默认
        </Button>
        {hint && <span className="text-xs text-secondary">{hint}</span>}
      </div>
    </div>
  );
}

// ---------- 高级 ----------

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function AdvancedSection({ version }: { version: string }) {
  const [stats, setStats] = useState<HistoryStatsView | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [feedback, setFeedback] = useState('');

  const reload = (): void => {
    window.appAPI.historyStats().then(setStats).catch((e: unknown) => console.error(e));
  };

  useEffect(reload, []);

  return (
    <div className="max-w-xl">
      <SectionTitle>高级</SectionTitle>
      <Row label="配置目录">
        <Button
          size="sm"
          icon={<FolderOpen className="h-3.5 w-3.5" />}
          onClick={() => void window.appAPI.openPath('configDir')}
        >
          打开配置目录
        </Button>
      </Row>
      <p className="mb-5 -mt-3 text-xs text-secondary opacity-60">
        含用户 config.yaml 副本（后端管线参数，可手工编辑）与前端偏好存储。
      </p>
      <Row label="日志目录">
        <Button
          size="sm"
          icon={<FileText className="h-3.5 w-3.5" />}
          onClick={() => void window.appAPI.openPath('logDir')}
        >
          打开日志目录
        </Button>
      </Row>
      <Row label="历史记录">
        <span className="flex items-center gap-3">
          <span className="text-xs text-secondary">
            {stats
              ? `${stats.sessionCount} 个会话 · ${stats.utteranceCount} 条语句 · ${formatBytes(stats.sizeBytes)}`
              : '统计中…'}
          </span>
          <Button size="sm" variant="danger" onClick={() => setClearOpen(true)}>
            清空全部历史
          </Button>
        </span>
      </Row>
      <Row label="版本">
        <span className="font-mono text-xs text-secondary">{version || '—'}</span>
      </Row>

      {feedback && <p className="text-xs text-secondary">{feedback}</p>}

      <Modal
        open={clearOpen}
        onClose={() => setClearOpen(false)}
        title="清空全部历史"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setClearOpen(false)}>取消</Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                void window.appAPI.clearHistory().then((s) => {
                  setStats(s);
                  setClearOpen(false);
                  setFeedback('历史已清空，新字幕将记入新会话');
                });
              }}
            >
              确认清空
            </Button>
          </>
        )}
      >
        将删除全部会话与语句记录并收缩数据库文件，且不可恢复。当前直播不受影响。
      </Modal>
    </div>
  );
}
