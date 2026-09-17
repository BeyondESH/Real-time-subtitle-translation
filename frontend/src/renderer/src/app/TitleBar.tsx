import type { CSSProperties } from 'react';
import { Captions } from 'lucide-react';

/** Electron 专有 CSS 属性（React 类型未收录，结构化声明而非 as any） */
export type DragStyle = CSSProperties & { WebkitAppRegion?: 'drag' | 'no-drag' };

export const DRAG_REGION: DragStyle = { WebkitAppRegion: 'drag' };
export const NO_DRAG_REGION: DragStyle = { WebkitAppRegion: 'no-drag' };

/**
 * 自定义标题栏（WCO）：左侧品牌区可拖动，右侧留白给 Windows 原生按钮。
 * 高度与主进程 TITLEBAR_HEIGHT(36px) 对齐。
 */
export function TitleBar() {
  return (
    <div
      className="flex h-9 shrink-0 select-none items-center gap-2 border-b border-edge bg-sidebar pl-4"
      style={DRAG_REGION}
    >
      <Captions className="h-4 w-4 text-accent" />
      <span className="text-xs font-semibold text-secondary">实时字幕翻译</span>
      {/* 右侧 140px 留给 WCO 原生最小化/最大化/关闭按钮 */}
      <span className="ml-auto inline-block h-full w-[140px]" />
    </div>
  );
}
