/**
 * Electron 主进程
 */
import { app, BrowserWindow, ipcMain, screen, Tray, Menu, globalShortcut } from 'electron';
import * as path from 'path';
import Store from 'electron-store';

// 配置存储
const store = new Store({
  defaults: {
    window: {
      width: 800,
      height: 200,
      x: null,
      y: null,
      opacity: 0.9
    },
    subtitle: {
      fontFamily: 'Microsoft YaHei',
      fontSize: 24,
      fontColor: '#FFFFFF',
      strokeColor: '#000000',
      strokeWidth: 2,
      displayMode: 'original_and_translation'
    },
    websocket: {
      host: 'localhost',
      port: 8765
    },
    shortcuts: {
      togglePause: 'Ctrl+Shift+Space',
      switchLanguage: 'Ctrl+Shift+L'
    }
  }
});

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let isPaused = false;

function createWindow(): void {
  const windowConfig = store.get('window');

  // 获取屏幕尺寸
  const { width: screenWidth, height: screenHeight } = screen.getPrimaryDisplay().workAreaSize;

  // 计算窗口位置（默认居中底部）
  const x = windowConfig.x ?? (screenWidth - windowConfig.width) / 2;
  const y = windowConfig.y ?? screenHeight - windowConfig.height - 50;

  mainWindow = new BrowserWindow({
    width: windowConfig.width,
    height: windowConfig.height,
    x: x,
    y: y,
    transparent: true,          // 透明背景
    frame: false,               // 无边框
    alwaysOnTop: true,          // 始终置顶
    skipTaskbar: true,          // 不在任务栏显示
    resizable: true,            // 可缩放
    hasShadow: false,           // 无阴影
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  });

  // 设置窗口透明度
  mainWindow.setOpacity(windowConfig.opacity);

  // 鼠标穿透（点击穿透到下层窗口）
  mainWindow.setIgnoreMouseEvents(true, { forward: true });

  // 加载字幕页面
  mainWindow.loadFile(path.join(__dirname, 'overlay.html'));

  // 窗口关闭事件
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // 窗口移动事件
  mainWindow.on('moved', () => {
    if (mainWindow) {
      const bounds = mainWindow.getBounds();
      store.set('window.x', bounds.x);
      store.set('window.y', bounds.y);
    }
  });

  // 窗口大小改变事件
  mainWindow.on('resized', () => {
    if (mainWindow) {
      const bounds = mainWindow.getBounds();
      store.set('window.width', bounds.width);
      store.set('window.height', bounds.height);
    }
  });
}

function createTray(): void {
  // 创建托盘图标（使用系统默认图标或自定义图标）
  tray = new Tray(path.join(__dirname, '../src/icon.png'));

  // 创建右键菜单
  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示/隐藏字幕',
      click: () => {
        if (mainWindow) {
          if (mainWindow.isVisible()) {
            mainWindow.hide();
          } else {
            mainWindow.show();
          }
        }
      }
    },
    {
      label: isPaused ? '恢复' : '暂停',
      click: () => {
        isPaused = !isPaused;
        if (mainWindow) {
          mainWindow.webContents.send('toggle-pause', isPaused);
        }
        updateTrayMenu();
      }
    },
    { type: 'separator' },
    {
      label: '设置',
      click: () => {
        // TODO: 打开设置窗口
      }
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        app.quit();
      }
    }
  ]);

  tray.setToolTip('实时字幕翻译');
  tray.setContextMenu(contextMenu);

  // 点击托盘图标显示/隐藏窗口
  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) {
        mainWindow.hide();
      } else {
        mainWindow.show();
      }
    }
  });
}

function updateTrayMenu(): void {
  if (tray) {
    const contextMenu = Menu.buildFromTemplate([
      {
        label: '显示/隐藏字幕',
        click: () => {
          if (mainWindow) {
            if (mainWindow.isVisible()) {
              mainWindow.hide();
            } else {
              mainWindow.show();
            }
          }
        }
      },
      {
        label: isPaused ? '恢复' : '暂停',
        click: () => {
          isPaused = !isPaused;
          if (mainWindow) {
            mainWindow.webContents.send('toggle-pause', isPaused);
          }
          updateTrayMenu();
        }
      },
      { type: 'separator' },
      {
        label: '设置',
        click: () => {
          // TODO: 打开设置窗口
        }
      },
      { type: 'separator' },
      {
        label: '退出',
        click: () => {
          app.quit();
        }
      }
    ]);
    tray.setContextMenu(contextMenu);
  }
}

function registerShortcuts(): void {
  const shortcuts = store.get('shortcuts');

  // 暂停/恢复快捷键
  globalShortcut.register(shortcuts.togglePause, () => {
    isPaused = !isPaused;
    if (mainWindow) {
      mainWindow.webContents.send('toggle-pause', isPaused);
    }
    updateTrayMenu();
  });

  // 切换语言快捷键
  globalShortcut.register(shortcuts.switchLanguage, () => {
    if (mainWindow) {
      mainWindow.webContents.send('switch-language');
    }
  });
}

// IPC 事件处理
ipcMain.on('set-ignore-mouse', (event, ignore: boolean) => {
  if (mainWindow) {
    mainWindow.setIgnoreMouseEvents(ignore, { forward: true });
  }
});

ipcMain.on('set-opacity', (event, opacity: number) => {
  if (mainWindow) {
    mainWindow.setOpacity(opacity);
    store.set('window.opacity', opacity);
  }
});

ipcMain.on('set-always-on-top', (event, flag: boolean) => {
  if (mainWindow) {
    mainWindow.setAlwaysOnTop(flag);
  }
});

// 应用生命周期
app.whenReady().then(() => {
  createWindow();
  createTray();
  registerShortcuts();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  globalShortcut.unregisterAll();
  app.quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});
