import { useState } from 'react';
import { Moon, Sun, Play, Search } from 'lucide-react';
import {
  Button, Toggle, Slider, Select, Modal, Pill, SegmentedNav,
  ListItem, StatusDot, ProgressBar, EmptyState
} from '../components/ui';

const LANG_OPTIONS = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
  { value: 'ja', label: '日文' }
];

const MODEL_OPTIONS = [
  { value: 'tiny', label: 'tiny（最快）' },
  { value: 'base', label: 'base（推荐）' },
  { value: 'small', label: 'small（较准）' },
  { value: 'medium', label: 'medium（准确）' },
  { value: 'large-v3', label: 'large-v3（最准）' }
];

/**
 * 设计系统走查页（开发辅助路由 /styleguide）：
 * 全部组件一次呈现，兼作 tokens/双主题/动效的人工验收面。
 */
export function StyleGuidePage() {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [toggleOn, setToggleOn] = useState(true);
  const [fontSize, setFontSize] = useState(24);
  const [language, setLanguage] = useState('zh');
  const [model, setModel] = useState('base');
  const [segment, setSegment] = useState('live');
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedSession, setSelectedSession] = useState('s2');

  const flipTheme = (): void => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.dataset.theme = next;
  };

  return (
    <div className="min-h-screen bg-base">
      <header className="sticky top-0 z-40 flex h-14 items-center justify-between border-b border-edge bg-sidebar px-6">
        <div className="flex items-center gap-2 text-lg font-semibold text-primary">
          <Play className="h-4 w-4 text-accent" />
          设计系统走查
        </div>
        <Button
          size="sm"
          variant="ghost"
          icon={theme === 'dark'
            ? <Sun className="h-4 w-4" />
            : <Moon className="h-4 w-4" />}
          onClick={flipTheme}
        >
          {theme === 'dark' ? '亮色' : '暗色'}
        </Button>
      </header>

      <div className="mx-auto flex max-w-4xl flex-col gap-10 p-6">
        <section className="flex flex-col gap-3">
          <h2 className="text-base font-semibold text-primary">Button</h2>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary">主操作</Button>
            <Button>次级操作</Button>
            <Button variant="ghost">幽灵按钮</Button>
            <Button variant="danger">危险操作</Button>
            <Button size="sm">小尺寸</Button>
            <Button variant="primary" disabled>
              禁用
            </Button>
          </div>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-base font-semibold text-primary">Toggle / Slider / Select</h2>
          <div className="flex flex-wrap items-center gap-8">
            <Toggle checked={toggleOn} onChange={setToggleOn} label="开机自启" />
            <div className="w-56">
              <Slider min={12} max={72} value={fontSize} onChange={setFontSize} label="字幕字号" />
            </div>
            <div className="w-44">
              <Select options={LANG_OPTIONS} value={language} onChange={setLanguage} />
            </div>
            <div className="w-52">
              <Select options={MODEL_OPTIONS} value={model} onChange={setModel} />
            </div>
          </div>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-base font-semibold text-primary">Pill / StatusDot / ProgressBar</h2>
          <div className="flex flex-wrap items-center gap-3">
            <Pill>系统回环（扬声器）</Pill>
            <Pill active>{model}</Pill>
            <Pill warn>丢句 3</Pill>
            <Pill icon={<Search className="h-3.5 w-3.5" />}>搜索</Pill>
          </div>
          <div className="flex flex-wrap items-center gap-5">
            <StatusDot status="ok" label="运行中" />
            <StatusDot status="ok" pulse label="聆听中" />
            <StatusDot status="paused" label="已暂停" />
            <StatusDot status="warn" label="过载" />
            <StatusDot status="danger" label="已断连" />
            <StatusDot status="idle" label="空闲" />
          </div>
          <div className="w-80">
            <ProgressBar value={64} label="whisper-base 下载中" />
          </div>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-base font-semibold text-primary">SegmentedNav / ListItem</h2>
          <div className="flex gap-8">
            <div className="w-48 rounded-card border border-edge bg-sidebar p-2">
              <SegmentedNav
                direction="vertical"
                value={segment}
                onChange={setSegment}
                items={[
                  { value: 'live', label: '直播字幕' },
                  { value: 'history', label: '历史会话' },
                  { value: 'settings', label: '设置' }
                ]}
              />
            </div>
            <div className="flex w-72 flex-col gap-1 rounded-card border border-edge bg-sidebar p-2">
              <ListItem
                title="生肉直播"
                subtitle="今天 14:32 · 128 句"
                selected={selectedSession === 's1'}
                onClick={() => setSelectedSession('s1')}
                trailing={<StatusDot status="idle" />}
              />
              <ListItem
                title="晨会录音"
                subtitle="今天 09:10 · 96 句"
                selected={selectedSession === 's2'}
                onClick={() => setSelectedSession('s2')}
                trailing={<StatusDot status="ok" label="进行中" />}
              />
              <ListItem title="Nvidia 发布会" subtitle="昨天 23:05 · 1024 句" />
            </div>
          </div>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-base font-semibold text-primary">Modal / EmptyState</h2>
          <Button onClick={() => setModalOpen(true)}>打开模态框</Button>
          <Modal
            open={modalOpen}
            onClose={() => setModalOpen(false)}
            title="删除会话"
            footer={(
              <>
                <Button variant="ghost" onClick={() => setModalOpen(false)}>取消</Button>
                <Button variant="danger" onClick={() => setModalOpen(false)}>删除</Button>
              </>
            )}
          >
            将同时删除该会话的全部语句记录，且不可恢复。
          </Modal>
          <div className="rounded-card border border-edge bg-sidebar">
            <EmptyState
              icon={<Search className="h-8 w-8" />}
              title="暂无字幕"
              description="播放任意包含语音的音频，字幕会自动出现在这里。"
              action={<Button variant="primary" size="sm">选择音频源</Button>}
            />
          </div>
        </section>
      </div>
    </div>
  );
}
