/**
 * QuantSystem Desktop — Electron Main Process
 *
 * Responsibilities:
 * - Spawn Python backend as a child process
 * - Wait for backend health check before loading frontend
 * - Load static frontend in production, dev server in development
 * - System tray with strategy status indicators
 * - Graceful shutdown: signal Python → wait → force kill
 */

const { app, BrowserWindow, ipcMain, dialog, protocol, net } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");
const { pathToFileURL } = require("url");
const { setupTray } = require("./tray");
const { setupUpdater } = require("./updater");

// Configuration
const isDev = process.env.NODE_ENV === "development";
const BACKEND_PORT = process.env.QS_BACKEND_PORT || 8002;
const BACKEND_HOST = "127.0.0.1";
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const HEALTH_CHECK_INTERVAL = 500; // ms
const HEALTH_CHECK_TIMEOUT = 30000; // ms
const SHUTDOWN_GRACE_MS = 5000;

let mainWindow = null;
let pythonProcess = null;
let tray = null;

// ------------------------------------------------------------------
// Path helpers — resolve project root correctly in dev vs packaged
// ------------------------------------------------------------------

function getProjectRoot() {
  if (isDev) {
    // Development: desktop/ is inside project root
    return path.resolve(__dirname, "..");
  }
  // Packaged: app is in resources/app.asar, project root is alongside
  // Expect project to be installed / available at a known path
  // For portable builds, look for a sibling QuantSystem folder
  const possibleRoots = [
    path.resolve(__dirname, ".."),                          // dev
    path.resolve(process.resourcesPath, "..", ".."),        // installed
    path.resolve(app.getPath("exe"), "..", ".."),            // exe dir parent
  ];
  for (const root of possibleRoots) {
    if (fs.existsSync(path.join(root, "dashboard", "backend", "main.py"))) {
      return root;
    }
  }
  return path.resolve(__dirname, "..");
}

function getFrontendPath() {
  if (isDev) {
    return null; // Use dev server URL
  }
  // Packaged: static frontend in resources/frontend/
  const resourceFrontend = path.join(process.resourcesPath, "frontend");
  if (fs.existsSync(resourceFrontend)) {
    return resourceFrontend;
  }
  // Fallback: try next to desktop/
  const localBuild = path.resolve(__dirname, "..", "dashboard", "frontend", "out");
  if (fs.existsSync(localBuild)) {
    return localBuild;
  }
  return null;
}

// ------------------------------------------------------------------
// Python Backend Process Management
// ------------------------------------------------------------------

function findPython() {
  return process.platform === "win32" ? "python" : "python3";
}

function startBackend() {
  const projectRoot = getProjectRoot();
  const pythonExe = findPython();

  const args = [
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

  console.log(`[Desktop] Starting backend: ${pythonExe} ${args.join(" ")}`);
  console.log(`[Desktop] Project root: ${projectRoot}`);

  pythonProcess = spawn(pythonExe, args, {
    cwd: projectRoot,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      QS_BACKEND_PORT: String(BACKEND_PORT),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  pythonProcess.stdout.on("data", (data) => {
    process.stdout.write(`[Backend] ${data}`);
  });

  pythonProcess.stderr.on("data", (data) => {
    process.stderr.write(`[Backend] ${data}`);
  });

  pythonProcess.on("error", (err) => {
    console.error("[Desktop] Failed to start Python backend:", err.message);
    dialog.showErrorBox(
      "Backend Error",
      `Failed to start Python backend: ${err.message}\n\nMake sure Python is installed and in your PATH.`
    );
  });

  pythonProcess.on("exit", (code, signal) => {
    console.log(
      `[Desktop] Python backend exited (code=${code}, signal=${signal})`
    );
    pythonProcess = null;
  });
}

function stopBackend() {
  return new Promise((resolve) => {
    if (!pythonProcess) {
      resolve();
      return;
    }

    console.log("[Desktop] Stopping backend...");

    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(pythonProcess.pid), "/t"]);
    } else {
      pythonProcess.kill("SIGTERM");
    }

    const forceKillTimer = setTimeout(() => {
      if (pythonProcess) {
        console.log("[Desktop] Force-killing backend...");
        if (process.platform === "win32") {
          spawn("taskkill", ["/pid", String(pythonProcess.pid), "/f", "/t"]);
        } else {
          pythonProcess.kill("SIGKILL");
        }
      }
      resolve();
    }, SHUTDOWN_GRACE_MS);

    if (pythonProcess) {
      pythonProcess.on("exit", () => {
        clearTimeout(forceKillTimer);
        resolve();
      });
    } else {
      clearTimeout(forceKillTimer);
      resolve();
    }
  });
}

// ------------------------------------------------------------------
// Health Check
// ------------------------------------------------------------------

function waitForBackend() {
  return new Promise((resolve, reject) => {
    const startTime = Date.now();

    const check = () => {
      if (Date.now() - startTime > HEALTH_CHECK_TIMEOUT) {
        reject(new Error("Backend health check timed out"));
        return;
      }

      const req = http.get(`${BACKEND_URL}/health`, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          setTimeout(check, HEALTH_CHECK_INTERVAL);
        }
      });

      req.on("error", () => {
        setTimeout(check, HEALTH_CHECK_INTERVAL);
      });

      req.setTimeout(2000, () => {
        req.destroy();
        setTimeout(check, HEALTH_CHECK_INTERVAL);
      });
    };

    check();
  });
}

// ------------------------------------------------------------------
// Window Management
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
    // Development: load from Next.js dev server
    const FRONTEND_DEV_URL =
      process.env.QS_FRONTEND_URL || "http://localhost:3000";
    mainWindow.loadURL(FRONTEND_DEV_URL);
    mainWindow.webContents.openDevTools({ mode: "right" });
  } else {
    // Production: load via custom app:// protocol for correct asset resolution
    const frontendPath = getFrontendPath();
    if (frontendPath) {
      console.log(`[Desktop] Loading frontend via app:// protocol from: ${frontendPath}`);
      mainWindow.loadURL("app://./index.html");
    } else {
      // Fallback: load from backend URL
      console.log(`[Desktop] No static frontend found, loading from: ${BACKEND_URL}`);
      mainWindow.loadURL(BACKEND_URL);
    }
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("close", (event) => {
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
// IPC Handlers
// ------------------------------------------------------------------

ipcMain.handle("get-backend-url", () => BACKEND_URL);

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
// Custom Protocol — serves static frontend with correct path resolution
// ------------------------------------------------------------------
// Must be called BEFORE app.whenReady().
// This allows absolute paths like /_next/static/css/xxx.css to resolve
// correctly from any page (root, /workshop/, /strategies/xxx/, etc.)

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

// ------------------------------------------------------------------
// App Lifecycle
// ------------------------------------------------------------------

app.whenReady().then(async () => {
  console.log("[Desktop] QuantSystem starting...");

  // Register custom protocol handler for serving static frontend
  const frontendDir = getFrontendPath();
  if (frontendDir && !isDev) {
    protocol.handle("app", (request) => {
      const url = new URL(request.url);
      let pathname = decodeURIComponent(url.pathname);

      // Remove leading slash for path.join
      if (pathname.startsWith("/")) {
        pathname = pathname.substring(1);
      }

      // Default to index.html for empty path
      if (!pathname) {
        pathname = "index.html";
      }

      let filePath = path.join(frontendDir, pathname);

      // If path is a directory, serve its index.html
      try {
        if (fs.statSync(filePath).isDirectory()) {
          filePath = path.join(filePath, "index.html");
        }
      } catch {
        // File doesn't exist — try .html extension for extensionless paths
        if (!path.extname(filePath) && fs.existsSync(filePath + ".html")) {
          filePath = filePath + ".html";
        }
      }

      // Convert to proper file:// URL (handles Windows backslashes)
      return net.fetch(pathToFileURL(filePath).toString());
    });
    console.log(`[Desktop] Custom 'app://' protocol registered → ${frontendDir}`);
  }

  // Check if backend is already running
  let backendAlreadyRunning = false;
  try {
    await new Promise((resolve, reject) => {
      const req = http.get(`${BACKEND_URL}/health`, (res) => {
        backendAlreadyRunning = res.statusCode === 200;
        resolve();
      });
      req.on("error", () => resolve());
      req.setTimeout(1000, () => { req.destroy(); resolve(); });
    });
  } catch { /* ignore */ }

  if (backendAlreadyRunning) {
    console.log("[Desktop] Backend already running on port " + BACKEND_PORT);
  } else {
    startBackend();
  }

  // Wait for backend to be ready
  try {
    await waitForBackend();
    console.log("[Desktop] Backend is ready");
  } catch (err) {
    console.error("[Desktop] Backend failed to start:", err.message);
    dialog.showErrorBox(
      "Startup Error",
      "The Python backend failed to start. Check the console for details."
    );
  }

  createMainWindow();
  tray = setupTray(mainWindow, BACKEND_URL);

  if (!isDev) {
    setupUpdater(mainWindow);
  }
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

app.on("before-quit", async (event) => {
  event.preventDefault();
  await stopBackend();
  app.exit(0);
});
