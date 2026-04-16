/**
 * .env validation — checks that required API keys are present in the
 * user's .env file before letting the main window load.
 *
 * Philosophy: don't block — warn. Missing keys only prevent live trading;
 * the user can still view Cloud deployments (VPS proxy) and receive
 * daily reports even with an empty local .env.
 */

const fs = require("fs");
const path = require("path");
const { dialog, shell } = require("electron");

const REQUIRED_KEYS = ["OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"];
const OPTIONAL_KEYS = [
  "ALERT_EMAIL_RECIPIENT",
  "ALERT_EMAIL_SENDER",
  "ALERT_EMAIL_PASSWORD",
];

function parseEnv(envPath) {
  if (!fs.existsSync(envPath)) return {};
  const content = fs.readFileSync(envPath, "utf-8");
  const result = {};
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

/**
 * Check the env file. Returns
 *   { ok: true,  missing: [], envPath }             — all good
 *   { ok: false, missing: [...], envPath, mode }    — needs attention
 *   mode = "missing_file" | "incomplete"
 */
function validateEnv(envPath) {
  if (!fs.existsSync(envPath)) {
    return {
      ok: false,
      missing: REQUIRED_KEYS.slice(),
      envPath,
      mode: "missing_file",
    };
  }
  const env = parseEnv(envPath);
  const missing = REQUIRED_KEYS.filter(k => !env[k] || env[k].trim() === "");
  return {
    ok: missing.length === 0,
    missing,
    envPath,
    mode: missing.length ? "incomplete" : "ok",
  };
}

/**
 * Show a modal when the env is incomplete. Returns the button index the
 * user picked:
 *   0 = "Open .env and fill it in"   (opens in default editor)
 *   1 = "Continue anyway"            (run with limited features)
 *   2 = "Quit"
 */
async function showEnvSetupDialog(parentWindow, result) {
  const title =
    result.mode === "missing_file"
      ? "QuantSystem — First-Run Setup"
      : "QuantSystem — Incomplete Configuration";

  const detailLines = [
    result.mode === "missing_file"
      ? `A new .env template has been created at:`
      : `The following required variables are missing from your .env:`,
    "",
    result.mode === "missing_file"
      ? `  ${result.envPath}`
      : result.missing.map(k => `  • ${k}`).join("\n"),
    "",
    result.mode === "incomplete" ? `File: ${result.envPath}` : "",
    "",
    "Without these, OKX trading features are disabled. You can still",
    "monitor cloud deployments and receive daily reports.",
  ].filter(Boolean);

  const response = await dialog.showMessageBox(parentWindow ?? null, {
    type: "warning",
    title,
    message:
      result.mode === "missing_file"
        ? "Welcome! Please configure your API keys."
        : "Some required settings are missing.",
    detail: detailLines.join("\n"),
    buttons: ["Open .env", "Continue anyway", "Quit"],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  });

  if (response.response === 0) {
    try {
      await shell.openPath(result.envPath);
    } catch (err) {
      console.error("[env_check] shell.openPath failed:", err);
    }
  }

  return response.response;
}

module.exports = { validateEnv, showEnvSetupDialog, REQUIRED_KEYS, OPTIONAL_KEYS };
