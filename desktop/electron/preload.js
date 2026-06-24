const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('academicDB', {
  installUpdate: () => ipcRenderer.invoke('install-update'),
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  openLogs: () => ipcRenderer.invoke('open-logs'),
  openRepo: () => ipcRenderer.invoke('open-repo'),
  onUpdateAvailable: (cb) => ipcRenderer.on('update-available', (_event, info) => cb(info)),
  onBackendLog: (cb) => ipcRenderer.on('backend-log', (_event, line) => cb(line))
});
