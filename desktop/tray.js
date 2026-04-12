/**
 * System tray — shows strategy status, provides quick actions.
 */

const { Tray, Menu, nativeImage } = require("electron");
const path = require("path");
const http = require("http");

let tray = null;
let pollInterval = null;

function setupTray(mainWindow, backendUrl) {
  // Create a simple tray icon (16x16 colored square as fallback)
  const iconPath = path.join(__dirname, "icons", "tray.png");
  let icon;
  try {
    icon = nativeImage.createFromPath(iconPath);
  } catch {
    // Fallback: create a tiny colored icon programmatically
    icon = nativeImage.createEmpty();
  }

  tray = new Tray(icon);
  tray.setToolTip("QuantSystem — Trading Platform");

  // Build context menu
  function buildMenu(strategies = {}) {
    const strategyItems = Object.entries(strategies).map(([id, info]) => {
      const stateEmoji =
        info.state === "RUNNING"
          ? "[RUN]"
          : info.state === "PAUSED"
          ? "[PAUSE]"
          : info.state === "ERROR"
          ? "[ERR]"
          : "[OFF]";

      return {
        label: `${stateEmoji} ${id}`,
        enabled: false,
      };
    });

    return Menu.buildFromTemplate([
      { label: "QuantSystem", enabled: false },
      { type: "separator" },
      ...strategyItems,
      ...(strategyItems.length > 0 ? [{ type: "separator" }] : []),
      {
        label: "Show Window",
        click: () => {
          if (mainWindow) {
            mainWindow.show();
            mainWindow.focus();
          }
        },
      },
      {
        label: "Emergency Stop",
        click: async () => {
          try {
            const postData = "";
            const options = {
              hostname: "127.0.0.1",
              port: new URL(backendUrl).port,
              path: "/api/live/emergency-stop",
              method: "POST",
              headers: { "Content-Length": 0 },
            };
            const req = http.request(options);
            req.end();
          } catch (err) {
            console.error("[Tray] Emergency stop failed:", err.message);
          }
        },
      },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          const { app } = require("electron");
          app.quit();
        },
      },
    ]);
  }

  tray.setContextMenu(buildMenu());

  // Poll orchestrator status to update tray menu
  pollInterval = setInterval(async () => {
    try {
      const port = new URL(backendUrl).port;
      const req = http.get(
        `http://127.0.0.1:${port}/api/live/status`,
        (res) => {
          let data = "";
          res.on("data", (chunk) => (data += chunk));
          res.on("end", () => {
            try {
              const status = JSON.parse(data);
              tray.setContextMenu(buildMenu(status.strategies || {}));

              // Forward to renderer
              if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send("strategy-update", status);
              }
            } catch {
              // ignore parse errors
            }
          });
        }
      );
      req.on("error", () => {}); // ignore connection errors
      req.setTimeout(3000, () => req.destroy());
    } catch {
      // ignore
    }
  }, 5000);

  tray.on("double-click", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  return tray;
}

function destroyTray() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
  if (tray) {
    tray.destroy();
    tray = null;
  }
}

module.exports = { setupTray, destroyTray };
