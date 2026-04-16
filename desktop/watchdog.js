/**
 * Backend watchdog — spawns and supervises the Python/qs-backend process.
 *
 * Responsibilities:
 *   - Spawn qs-backend.exe (packaged) or `python -m uvicorn ...` (dev)
 *   - Detect unexpected exits and auto-restart with exponential backoff
 *   - Stop after MAX_RAPID_RESTARTS to avoid crash loops
 *   - Surface crash reports via callback for the UI layer
 *   - Clean shutdown: SIGTERM, then taskkill /F after grace
 */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const HEALTHY_UPTIME_MS = 60_000;    // reset restart counter after 1m clean run
const BACKOFF_SCHEDULE = [2_000, 5_000, 15_000]; // 3 restart attempts
const SHUTDOWN_GRACE_MS = 5_000;

class BackendWatchdog {
  /**
   * @param {Object} opts
   * @param {string} opts.executablePath - Path to qs-backend.exe or "python"
   * @param {string[]} opts.args         - Args for the executable
   * @param {string} opts.cwd            - Working directory
   * @param {Object} opts.env            - Env vars
   * @param {Function} opts.onCrash      - Called when max restarts exceeded
   * @param {Function} opts.logger       - Logger { info, warn, error }
   */
  constructor(opts) {
    this.executablePath = opts.executablePath;
    this.args = opts.args || [];
    this.cwd = opts.cwd;
    this.env = opts.env || process.env;
    this.onCrash = opts.onCrash || (() => {});
    this.logger = opts.logger || console;

    this.process = null;
    this.shuttingDown = false;
    this.restartAttempts = 0;
    this.restartTimer = null;
    this.uptimeTimer = null;
    this.lastStartTime = 0;
  }

  start() {
    if (this.process) return;
    this.shuttingDown = false;
    this._spawn();
  }

  _spawn() {
    this.lastStartTime = Date.now();

    this.logger.info(
      `[watchdog] Spawning backend: ${this.executablePath} ${this.args.join(" ")}`
    );

    this.process = spawn(this.executablePath, this.args, {
      cwd: this.cwd,
      env: this.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });

    this.process.stdout.on("data", data => {
      process.stdout.write(`[backend] ${data}`);
    });
    this.process.stderr.on("data", data => {
      process.stderr.write(`[backend] ${data}`);
    });

    this.process.on("error", err => {
      this.logger.error(`[watchdog] Backend spawn error: ${err.message}`);
    });

    this.process.on("exit", (code, signal) => {
      const exitedPid = this.process?.pid;
      this.process = null;
      if (this.uptimeTimer) {
        clearTimeout(this.uptimeTimer);
        this.uptimeTimer = null;
      }

      if (this.shuttingDown) {
        this.logger.info(
          `[watchdog] Backend exited cleanly (pid=${exitedPid}, code=${code}, signal=${signal})`
        );
        return;
      }

      this.logger.warn(
        `[watchdog] Backend exited unexpectedly (pid=${exitedPid}, code=${code}, signal=${signal})`
      );
      this._scheduleRestart();
    });

    // Reset restart counter if backend runs cleanly for HEALTHY_UPTIME_MS
    this.uptimeTimer = setTimeout(() => {
      if (this.restartAttempts > 0) {
        this.logger.info(
          `[watchdog] Backend healthy for ${HEALTHY_UPTIME_MS / 1000}s — resetting restart counter`
        );
        this.restartAttempts = 0;
      }
    }, HEALTHY_UPTIME_MS);
  }

  _scheduleRestart() {
    if (this.restartAttempts >= BACKOFF_SCHEDULE.length) {
      this.logger.error(
        `[watchdog] Max restart attempts (${BACKOFF_SCHEDULE.length}) exceeded — giving up`
      );
      this._writeCrashReport();
      this.onCrash({
        reason: "max_restarts_exceeded",
        attempts: this.restartAttempts,
      });
      return;
    }

    const delay = BACKOFF_SCHEDULE[this.restartAttempts];
    this.restartAttempts += 1;
    this.logger.warn(
      `[watchdog] Restarting backend in ${delay / 1000}s (attempt ${this.restartAttempts}/${BACKOFF_SCHEDULE.length})`
    );

    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      this._spawn();
    }, delay);
  }

  _writeCrashReport() {
    try {
      const logsDir = path.join(
        this.env.QS_APP_DATA_DIR || process.cwd(),
        "logs"
      );
      if (!fs.existsSync(logsDir)) {
        fs.mkdirSync(logsDir, { recursive: true });
      }
      const ts = new Date().toISOString().replace(/[:.]/g, "-");
      const reportPath = path.join(logsDir, `crash-${ts}.log`);
      fs.writeFileSync(
        reportPath,
        [
          `QuantSystem Backend Watchdog — Crash Report`,
          `Timestamp: ${new Date().toISOString()}`,
          `Executable: ${this.executablePath}`,
          `Args: ${this.args.join(" ")}`,
          `CWD: ${this.cwd}`,
          `Attempts: ${this.restartAttempts}`,
          ``,
          `The backend exited ${this.restartAttempts} times in rapid succession`,
          `and the watchdog stopped trying. Check backend.log for details.`,
        ].join("\n"),
        "utf-8"
      );
      this.logger.info(`[watchdog] Crash report written to ${reportPath}`);
    } catch (err) {
      this.logger.error(`[watchdog] Failed to write crash report: ${err.message}`);
    }
  }

  stop() {
    return new Promise(resolve => {
      this.shuttingDown = true;

      if (this.restartTimer) {
        clearTimeout(this.restartTimer);
        this.restartTimer = null;
      }
      if (this.uptimeTimer) {
        clearTimeout(this.uptimeTimer);
        this.uptimeTimer = null;
      }

      if (!this.process) {
        resolve();
        return;
      }

      const pid = this.process.pid;

      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(pid), "/t"]);
      } else {
        this.process.kill("SIGTERM");
      }

      const forceTimer = setTimeout(() => {
        if (this.process) {
          this.logger.warn(`[watchdog] Force-killing backend (pid=${pid})`);
          if (process.platform === "win32") {
            spawn("taskkill", ["/pid", String(pid), "/f", "/t"]);
          } else {
            this.process.kill("SIGKILL");
          }
        }
        resolve();
      }, SHUTDOWN_GRACE_MS);

      this.process.on("exit", () => {
        clearTimeout(forceTimer);
        resolve();
      });
    });
  }

  get pid() {
    return this.process?.pid ?? null;
  }

  get isRunning() {
    return this.process !== null;
  }
}

module.exports = { BackendWatchdog };
