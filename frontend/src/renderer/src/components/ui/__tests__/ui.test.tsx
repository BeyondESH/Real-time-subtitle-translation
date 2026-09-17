// @vitest-environment jsdom
/**
 * UI 组件层冒烟测试（design-system spec "组件一致性"）
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import {
  Button, Toggle, Slider, Select, Modal, Pill, SegmentedNav,
  ListItem, StatusDot, ProgressBar, EmptyState
} from '../index';

// vitest 未开 globals，RTL 自动清理不生效 → 显式 cleanup
afterEach(cleanup);

describe('Button', () => {
  it('渲染文本并触发 onClick', () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>暂停</Button>);
    fireEvent.click(screen.getByText('暂停'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('variant 映射 token 类名', () => {
    render(<Button variant="primary">主操作</Button>);
    expect(screen.getByText('主操作').className).toContain('bg-accent');
    render(<Button variant="danger">危险</Button>);
    expect(screen.getByText('危险').className).toContain('text-danger');
  });

  it('默认 type=button', () => {
    render(<Button>默认</Button>);
    expect(screen.getByText('默认').getAttribute('type')).toBe('button');
  });
});

describe('Toggle', () => {
  it('点击翻转并回调，aria-checked 同步', () => {
    const onChange = vi.fn();
    render(<Toggle checked={false} onChange={onChange} label="自启" />);
    const sw = screen.getByRole('switch');
    expect(sw.getAttribute('aria-checked')).toBe('false');
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('激活态使用 accent token', () => {
    render(<Toggle checked onChange={() => undefined} />);
    expect(screen.getByRole('switch').className).toContain('bg-accent');
  });
});

describe('Slider', () => {
  it('change 回调数值', () => {
    const onChange = vi.fn();
    render(<Slider min={12} max={72} value={24} onChange={onChange} label="字号" />);
    fireEvent.change(screen.getByRole('slider'), { target: { value: '36' } });
    expect(onChange).toHaveBeenCalledWith(36);
  });
});

describe('Select', () => {
  it('change 回调选中值', () => {
    const onChange = vi.fn();
    render(
      <Select
        options={[{ value: 'zh', label: '中文' }, { value: 'en', label: '英文' }]}
        value="zh"
        onChange={onChange}
      />
    );
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'en' } });
    expect(onChange).toHaveBeenCalledWith('en');
  });
});

describe('Modal', () => {
  it('open=false 不渲染', () => {
    render(<Modal open={false} onClose={() => undefined} title="t">body</Modal>);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('open=true 渲染标题与内容；Escape 触发 onClose', () => {
    const onClose = vi.fn();
    render(<Modal open onClose={onClose} title="删除会话">不可恢复</Modal>);
    expect(screen.getByRole('dialog')).not.toBeNull();
    expect(screen.getByText('不可恢复')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('点击背板关闭', () => {
    const onClose = vi.fn();
    const { container } = render(<Modal open onClose={onClose}>x</Modal>);
    const backdrop = container.querySelector('[aria-hidden]');
    expect(backdrop).not.toBeNull();
    fireEvent.click(backdrop!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('Pill', () => {
  it('有 onClick 时为 button 并触发', () => {
    const onClick = vi.fn();
    render(<Pill onClick={onClick}>base</Pill>);
    fireEvent.click(screen.getByText('base'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('无 onClick 时为纯展示 span；active/warn 映射 token', () => {
    const { rerender } = render(<Pill active>zh</Pill>);
    const el = screen.getByText('zh');
    expect(el.tagName.toLowerCase()).toBe('span');
    expect(el.className).toContain('text-accent');
    rerender(<Pill warn>丢句</Pill>);
    expect(screen.getByText('丢句').className).toContain('text-warn');
  });
});

describe('SegmentedNav', () => {
  it('点击项回调 value，激活项 aria-current', () => {
    const onChange = vi.fn();
    render(
      <SegmentedNav
        items={[{ value: 'live', label: '直播' }, { value: 'settings', label: '设置' }]}
        value="live"
        onChange={onChange}
      />
    );
    expect(screen.getByText('直播').getAttribute('aria-current')).toBe('true');
    fireEvent.click(screen.getByText('设置'));
    expect(onChange).toHaveBeenCalledWith('settings');
  });
});

describe('ListItem', () => {
  it('标题/副标题渲染，onClick 触发，selected 高亮', () => {
    const onClick = vi.fn();
    render(
      <ListItem title="生肉直播" subtitle="128 句" selected onClick={onClick} />
    );
    const btn = screen.getByRole('button');
    expect(screen.getByText('生肉直播')).not.toBeNull();
    expect(screen.getByText('128 句')).not.toBeNull();
    expect(btn.getAttribute('aria-current')).toBe('true');
    expect(btn.className).toContain('bg-elevated');
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe('StatusDot', () => {
  it('status 映射语义 token 类名', () => {
    const { rerender } = render(<StatusDot status="ok" label="运行中" />);
    expect(screen.getByText('运行中')).not.toBeNull();
    expect(document.querySelector('[data-status="ok"]')?.className).toContain('bg-ok');
    rerender(<StatusDot status="danger" />);
    expect(document.querySelector('[data-status="danger"]')?.className).toContain('bg-danger');
  });

  it('pulse 追加动效类', () => {
    render(<StatusDot status="ok" pulse />);
    expect(document.querySelector('[data-status="ok"]')?.className).toContain('animate-pulse');
  });
});

describe('ProgressBar', () => {
  it('宽度随 value，越界截断', () => {
    const { rerender } = render(<ProgressBar value={42} label="下载" />);
    const bar = screen.getByRole('progressbar').firstElementChild as HTMLElement;
    expect(bar.style.width).toBe('42%');
    rerender(<ProgressBar value={150} />);
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
  });
});

describe('EmptyState', () => {
  it('标题/描述/动作渲染', () => {
    render(
      <EmptyState title="暂无字幕" description="播放音频试试" action={<Button>选择</Button>} />
    );
    expect(screen.getByText('暂无字幕')).not.toBeNull();
    expect(screen.getByText('播放音频试试')).not.toBeNull();
    expect(screen.getByText('选择')).not.toBeNull();
  });
});
