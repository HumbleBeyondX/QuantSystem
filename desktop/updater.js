/**
 * Auto-updater — checks for updates and notifies the user.
 *
 * Uses electron-updater for GitHub Releases or custom update server.
 */

function setupUpdater(mainWindow) {
  try {
    const { autoUpdater } = require("electron-updater");

    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = true;

    autoUpdater.on("update-available", (info) => {
      console.log("[Updater] Update available:", info.version);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("notification", {
          type: "update",
          title: "Update Available",
          message: `Version ${info.version} is available. Click to download.`,
          version: info.version,
        });
      }
    });

    autoUpdater.on("update-downloaded", (info) => {
      console.log("[Updater] Update downloaded:", info.version);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("notification", {
          type: "update-ready",
          title: "Update Ready",
          message: `Version ${info.version} will be installed on restart.`,
          version: info.version,
        });
      }
    });

    autoUpdater.on("error", (err) => {
      console.error("[Updater] Error:", err.message);
    });

    // Check for updates every 4 hours
    autoUpdater.checkForUpdates().catch(() => {});
    setInterval(
      () => {
        autoUpdater.checkForUpdates().catch(() => {});
      },
      4 * 60 * 60 * 1000
    );
  } catch (err) {
    console.warn("[Updater] Not available:", err.message);
  }
}

module.exports = { setupUpdater };
