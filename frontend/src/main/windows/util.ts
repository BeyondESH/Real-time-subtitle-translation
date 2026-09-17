/**
 * 窗口通用工具：渲染入口加载（dev=vite URL，build/打包=磁盘产物）
 * 加载失败必须留痕：did-fail-load / promise rejection 全部进日志
 * （electron-log initialize 会接管 console，打包环境写入 frontend.log）
 */
import * as path from 'path';
import type { BrowserWindow } from 'electron';

const DEV_RENDERER_URL = process.env['ELECTRON_RENDERER_URL'];

export type RendererEntry = 'index' | 'overlay';

export function rendererMode(): string {
  return DEV_RENDERER_URL ? 'dev-server' : 'file';
}

export function loadRenderer(win: BrowserWindow, name: RendererEntry): void {
  const target = DEV_RENDERER_URL
    ? `${DEV_RENDERER_URL}/${name}.html`
    : path.join(__dirname, '..', 'renderer', `${name}.html`);

  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[renderer] did-fail-load: ${name} code=${code} desc=${desc} url=${url}`);
  });

  const loading = DEV_RENDERER_URL
    ? win.loadURL(target)
    : win.loadFile(target);
  loading.catch((err: unknown) => {
    console.error(`[renderer] load rejected: ${name} target=${target}`, err);
  });
  console.log(`[renderer] loading ${name} via ${rendererMode()}: ${target}`);
}

export function preloadPath(): string {
  return path.join(__dirname, '..', 'preload', 'index.js');
}
