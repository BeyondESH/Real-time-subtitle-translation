/**
 * Preload 脚本 - 安全地暴露 IPC 方法
 */
import { contextBridge, ipcRenderer } from 'electron';

// 暴露安全的 API 给渲染进程
contextBridge.exposeInMainWorld('electronAPI', {
  // 窗口控制
  setIgnoreMouseEvents: (ignore: boolean) => {
    ipcRenderer.send('set-ignore-mouse', ignore);
  },
  setOpacity: (opacity: number) => {
    ipcRenderer.send('set-opacity', opacity);
  },
  setAlwaysOnTop: (flag: boolean) => {
    ipcRenderer.send('set-always-on-top', flag);
  },

  // 事件监听
  onTogglePause: (callback: (paused: boolean) => void) => {
    ipcRenderer.on('toggle-pause', (event, paused) => callback(paused));
  },
  onSwitchLanguage: (callback: () => void) => {
    ipcRenderer.on('switch-language', () => callback());
  },

  // 移除监听器
  removeAllListeners: (channel: string) => {
    ipcRenderer.removeAllListeners(channel);
  }
});
