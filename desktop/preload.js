/**
 * Preload script — secure bridge between renderer and main process.
 *
 * Exposes a limited API via contextBridge. The renderer (React app)
 * can access these via `window.quantsystem`.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("quantsystem", {
  // Get the backend API URL
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),

  // Emergency stop — sends to main process which calls the API
  emergencyStop: () => ipcRenderer.invoke("emergency-stop"),

  // Platform info
  platform: process.platform,
  isElectron: true,

  // Window controls
  onNotification: (callback) => {
    ipcRenderer.on("notification", (_event, data) => callback(data));
  },

  // Strategy state updates from main process
  onStrategyUpdate: (callback) => {
    ipcRenderer.on("strategy-update", (_event, data) => callback(data));
  },
});
