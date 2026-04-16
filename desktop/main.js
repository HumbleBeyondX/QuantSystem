/**
 * QuantSystem Desktop — Electron Main Process
 *
 * Production responsibilities:
 *   - Single-instance lock (prevent duplicate port-8002 backends)
 *   - File logging via electron-log (%APPDATA%/QuantSystem/logs/electron.log)
 *   - Validate user .env before loading UI (warn, don't block)
 *   - Spawn qs-backend.exe (bundled) with BackendWatchdog
 *   - Load static Next.js frontend via app:// protocol
 *   - System tray with emergency controls
 *   - Graceful shutdown signaling Python → wait → force kill
 */

const { app, BrowserWindow, ipcMain, dialog, protocol, net, shell } = require("electron");
const path = require("path");
const http = require("http");
const fs = require("fs");
const { pathToFileURL } = require("url");
const log = require("electron-log/main");

const { setupTray } = require("./tray");
const { setupUpdater } = require("./updater");
const { BackendWatchdog } = require("./watchdog");
const { validateEnv, showEnvSetupDialog } = require("./env_check");

// ------------------------------------------------------------------
// Single-instance lock — ALWAYS acquire first. If we lose, exit.
// ------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // Another instance is already running — the other instance will be
  // notified via second-instance event.
  app.quit();
  process.exit(0);
}

// ------------------------------------------------------------------
// Electron-log setup — route console.* and main process logs to file.
// ------------------------------------------------------------------

const userLogsDir = path.join(app.getPath("userData"), "logs");
try {
  fs.mkdirSync(userLogsDir, { recursive: true });
} catch (_) {}

log.initialize();
log.transports.file.resolvePathFn = () => path.join(userLogsDir, "electron.log");
log.transports.file.maxSize = 10 * 1024 * 1024; // 10 MB per file
log.transports.file.archiveLog = file => {
  // rotate: electron.log → electron.1.log, electron.1.log → electron.2.log, ...
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  return path.join(userLogsDir, `electron-${ts}.log`);
};
log.transports.console.level = "info";
log.transports.file.level = "info";
Object.assign(console, log.functions);

// ------------------------------------------------------------------
// Configuration
// ------------------------------------------------------------------

const isDev = process.env.NODE_ENV === "development";
const BACKEND_PORT = process.env.QS_BACKEND_PORT || 8002;
const BACKEND_HOST = "127.0.0.1";
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const HEALTH_CHECK_INTERVAL = 500;
const HEALTH_CHECK_TIMEOUT = 30000;

let mainWindow = null;
let watchdog = null;
let tray = null;
let envValidationResult = null;

// ------------------------------------------------------------------
// Path helpers
// ------------------------------------------------------------------

function getProjectRoot() {
  if (isDev) return path.resolve(__dirname, "..");
  const candidates = [
    path.resolve(__dirname, ".."),
    path.resolve(process.resourcesPath, "..", ".."),
    path.resolve(app.getPath("exe"), "..", ".."),
  ];
  for (const root of candidates) {
    if (fs.existsSync(path.join(root, "dashboard", "backend", "main.py"))) {
      return root;
    }
  }
  return path.resolve(__dirname, "..");
}

function getFrontendPath() {
  if (isDev) return null;
  const packaged = path.join(process.resourcesPath, "frontend");
  if (fs.existsSync(packaged)) return packaged;
  const local = path.resolve(__dirname, "..", "dashboard", "frontend", "out");
  if (fs.existsSync(local)) return local;
  return null;
}

function getBundledBackendPath() {
  // Packaged: resources/backend/qs-backend.exe
  const packaged = path.join(
    process.resourcesPath,
    "backend",
    "qs-backend.exe"
  );
  if (fs.existsSync(packaged)) return packaged;
  // Dev fallback (if user built the bundle locally):
  const local = path.join(
    __dirname,
    "..",
    "deploy",
    "dist",
    "qs-backend",
    "qs-backend.exe"
  );
  if (fs.existsSync(local)) return local;
  return null;
}

// ------------------------------------------------------------------
// Backend startup — prefer bundled exe, fall back to system Python (dev)
// ------------------------------------------------------------------

function buildBackendEnv() {
  return {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    QS_BACKEND_PORT: String(BACKEND_PORT),
    QS_BACKEND_HOST: BACKEND_HOST,
    QS_APP_DATA_DIR: app.getPath("userData"),
    QS_USE_APPDATA_IN_DEV: "1",
    // Deliberately DO NOT set QS_LIVE_CONFIG — desktop is monitor + paper only.
  };
}

function startBackendWithWatchdog() {
  const bundled = getBundledBackendPath();
  const env = buildBackendEnv();
  const projectRoot = getProjectRoot();

  let executable;
  let args;
  let cwd;

  if (bundled && !isDev) {
    executable = bundled;
    args = [];
    cwd = path.dirname(bundled);
    log.info(`[main] Using bundled backend: ${bundled}`);
  } else {
    // Dev fallback: system Python
    executable = process.platform === "win32" ? "python" : "python3";
    args = [
      "-m",
      "uvicorn",
      "dashboard.backend.main:app",
      "--host",
      BACKEND_HOST,
      "--port",
      String(BACKEND_PORT),
      "--log-level",
      "info",
    ];
    cwd = projectRoot;
    log.info(`[main] Using dev backend: ${executable} ${args.join(" ")}`);
  }

  watchdog = new BackendWatchdog({
    executablePath: executable,
    args,
    cwd,
    env,
    logger: log,
    onCrash: async info => {
      const response = await dialog.showMessageBox(mainWindow ?? null, {
        type: "error",
        title: "QuantSystem — Backend Unavailable",
        message: "The backend process has crashed repeatedly.",
        detail:
          `The backend exited ${info.attempts} times in rapid succession.\n\n` +
          `A crash report has been written to:\n  ${userLogsDir}\n\n` +
          `Click "Open logs folder" to inspect, or quit to exit the app.`,
        buttons: ["Open logs folder", "Quit"],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (response.response === 0) {
        shell.openPath(userLogsDir);
      }
      app.quit();
    },
  });

  watchdog.start();
}

// ------------------------------------------------------------------
// Backend health check
// ------------------------------------------------------------------

function waitForBackend() {
  return new Promise((resolve, reject) => {
    const startTime = Date.now();
    const check = () => {
      if (Date.now() - startTime > HEALTH_CHECK_TIMEOUT) {
        reject(new Error("Backend health check timed out"));
        return;
      }
      const req = http.get(`${BACKEND_URL}/health`, res => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          setTimeout(check, HEALTH_CHECK_INTERVAL);
        }
      });
      req.on("error", () => setTimeout(check, HEALTH_CHECK_INTERVAL));
      req.setTimeout(2000, () => {
        req.destroy();
        setTimeout(check, HEALTH_CHECK_INTERVAL);
      });
    };
    check();
  });
}

function isBackendAlreadyRunning() {
  return new Promise(resolve => {
    const req = http.get(`${BACKEND_URL}/health`, res => {
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

// ------------------------------------------------------------------
// Main window
// ------------------------------------------------------------------

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: "QuantSystem",
    icon: path.join(__dirname, "icons", "icon.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  if (isDev) {
    const devUrl = process.env.QS_FRONTEND_URL || "http://localhost:3000";
    mainWindow.loadURL(devUrl);
    mainWindow.webContents.openDevTools({ mode: "right" });
  } else {
    const frontendPath = getFrontendPath();
    if (frontendPath) {
      mainWindow.loadURL("app://./index.html");
    } else {
      mainWindow.loadURL(BACKEND_URL);
    }
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    if (envValidationResult && !envValidationResult.ok) {
      // Send warning banner data to renderer via IPC
      mainWindow.webContents.send("env-validation", envValidationResult);
    }
  });

  mainWindow.on("close", event => {
    if (tray) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ------------------------------------------------------------------
// IPC
// ------------------------------------------------------------------

ipcMain.handle("get-backend-url", () => BACKEND_URL);
ipcMain.handle("get-env-validation", () => envValidationResult);
ipcMain.handle("open-app-data-folder", () => {
  shell.openPath(app.getPath("userData"));
  return true;
});
ipcMain.handle("open-env-file", () => {
  const { spawn: spawnChild } = require("child_process");
  const envPath = path.join(app.getPath("userData"), ".env");
  spawnChild("notepad.exe", [envPath], { detached: true, stdio: "ignore" }).unref();
  return true;
});

ipcMain.handle("emergency-stop", async () => {
  try {
    const res = await fetch(`${BACKEND_URL}/api/live/emergency-stop`, {
      method: "POST",
    });
    return await res.json();
  } catch (err) {
    return { error: err.message };
  }
});

// ------------------------------------------------------------------
// Custom app:// protocol for static frontend
// ------------------------------------------------------------------

protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
      stream: true,
    },
  },
]);

function registerAppProtocol() {
  const frontendDir = getFrontendPath();
  if (!frontendDir || isDev) return;
  protocol.handle("app", request => {
    const url = new URL(request.url);
    let pathname = decodeURIComponent(url.pathname);
    if (pathname.startsWith("/")) pathname = pathname.substring(1);
    if (!pathname) pathname = "index.html";
    let filePath = path.join(frontendDir, pathname);
    try {
      if (fs.statSync(filePath).isDirectory()) {
        filePath = path.join(filePath, "index.html");
      }
    } catch {
      if (!path.extname(filePath) && fs.existsSync(filePath + ".html")) {
        filePath = filePath + ".html";
      }
    }
    return net.fetch(pathToFileURL(filePath).toString());
  });
  log.info(`[main] Custom app:// protocol registered → ${frontendDir}`);
}

// ------------------------------------------------------------------
// Second instance handler
// ------------------------------------------------------------------

app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
  }
});

// ------------------------------------------------------------------
// Lifecycle
// ------------------------------------------------------------------

app.whenReady().then(async () => {
  log.info("[main] QuantSystem starting…");
  log.info(`[main] userData: ${app.getPath("userData")}`);

  registerAppProtocol();

  // Pre-check: if another backend is already running on the port, don't
  // spawn another (might be a dev uvicorn the user started). We'll just
  // connect to it.
  const alreadyUp = await isBackendAlreadyRunning();
  if (alreadyUp) {
    log.info(`[main] Backend already running on :${BACKEND_PORT} — attaching`);
  } else {
    startBackendWithWatchdog();
  }

  try {
    await waitForBackend();
    log.info("[main] Backend is ready");
  } catch (err) {
    log.error(`[main] Backend failed to start: ${err.message}`);
    dialog.showErrorBox(
      "QuantSystem — Startup Error",
      `The backend did not become healthy within ${HEALTH_CHECK_TIMEOUT / 1000}s.\n\n` +
        `Check logs at ${userLogsDir}`
    );
  }

  // Validate .env AFTER the backend ran its scaffolding (creates the template).
  const envPath = path.join(app.getPath("userData"), ".env");
  envValidationResult = validateEnv(envPath);
  if (!envValidationResult.ok) {
    const choice = await showEnvSetupDialog(null, envValidationResult);
    if (choice === 2) {
      log.info("[main] User chose to quit from env setup dialog");
      app.quit();
      return;
    }
    // Re-validate after user potentially edited the file
    envValidationResult = validateEnv(envPath);
  }

  createMainWindow();
  tray = setupTray(mainWindow, BACKEND_URL);

  if (!isDev) setupUpdater(mainWindow);
});

app.on("activate", () => {
  if (mainWindow === null) {
    createMainWindow();
  } else {
    mainWindow.show();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && !tray) {
    app.quit();
  }
});

app.on("before-quit", async event => {
  event.preventDefault();
  log.info("[main] Graceful shutdown initiated");
  if (watchdog) {
    await watchdog.stop();
  }
  app.exit(0);
});
