const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

let win;
let backend;
let updateInfo = null;
let updateTimer = null;

function loadRepoRoot() {
  if (process.env.ACADEMIC_DB_REPO) return process.env.ACADEMIC_DB_REPO;
  try {
    const p = path.join(__dirname, 'repo-path.json');
    if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8')).repoRoot;
  } catch (_) {}
  // Dev fallback: desktop/electron -> repo root
  return path.resolve(__dirname, '..', '..');
}

function isGitRepo(dir) {
  return !!dir && fs.existsSync(path.join(dir, '.git'));
}

async function ensureRepoRoot() {
  if (isGitRepo(repoRoot)) return repoRoot;
  const fallback = path.join(app.getPath('userData'), 'Academic-Database');
  if (isGitRepo(fallback)) {
    repoRoot = fallback;
    return repoRoot;
  }
  fs.mkdirSync(path.dirname(fallback), { recursive: true });
  appendEarlyLog(`No git repo found at ${repoRoot}. Cloning into ${fallback}...
`);
  const clone = await new Promise((resolve) => {
    execFile('git', ['clone', 'https://github.com/whoseyci/Academic-Database.git', fallback], (error, stdout, stderr) => resolve({ ok: !error, stdout, stderr, error }));
  });
  if (!clone.ok) throw new Error(`Could not clone Academic-Database into ${fallback}: ${clone.stderr || clone.error}`);
  repoRoot = fallback;
  return repoRoot;
}

function appendEarlyLog(line) {
  try {
    const fallbackReports = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(fallbackReports, { recursive: true });
    fs.appendFileSync(path.join(fallbackReports, 'launcher.log'), line);
  } catch (_) {}
}

let repoRoot = loadRepoRoot();
const host = process.env.RH_REVIEW_HOST || '127.0.0.1';
const port = Number(process.env.RH_REVIEW_PORT || 8765);
const url = `http://${host}:${port}`;
let reportsDir = path.join(repoRoot, 'reports');
let runDir = path.join(repoRoot, '.run');
let logFile = path.join(reportsDir, 'electron_backend.log');
function refreshPaths() {
  reportsDir = path.join(repoRoot, 'reports');
  runDir = path.join(repoRoot, '.run');
  logFile = path.join(reportsDir, 'electron_backend.log');
}

function appendLog(line) {
  fs.mkdirSync(reportsDir, { recursive: true });
  fs.appendFileSync(logFile, line);
  if (win && !win.isDestroyed()) win.webContents.send('backend-log', line);
}

function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd: repoRoot, ...opts }, (error, stdout, stderr) => {
      resolve({ ok: !error, code: error && error.code, stdout, stderr, error });
    });
  });
}

async function pythonCmd() {
  const venvPython = path.join(repoRoot, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPython)) return venvPython;
  return 'python3';
}

async function ensureVenvAndDeps() {
  fs.mkdirSync(reportsDir, { recursive: true });
  fs.mkdirSync(runDir, { recursive: true });
  const venvPython = path.join(repoRoot, '.venv', 'bin', 'python');
  const setupStamp = path.join(repoRoot, '.venv', '.electron_core_setup_stamp');
  let createdVenv = false;
  if (!fs.existsSync(venvPython)) {
    appendLog('Creating .venv...\n');
    await run('python3', ['-m', 'venv', '.venv']);
    createdVenv = true;
  }
  const py = await pythonCmd();
  // The review backend itself is stdlib-only. Avoid slow pip work on every launch.
  if (process.env.ACADEMIC_DB_SKIP_PIP !== '1' && (createdVenv || !fs.existsSync(setupStamp))) {
    appendLog('Preparing Python environment...\n');
    await run(py, ['-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', 'setuptools']);
    fs.writeFileSync(setupStamp, new Date().toISOString());
  }
  const parserReq = path.join(repoRoot, 'requirements-parser.txt');
  const parserStamp = path.join(repoRoot, '.venv', '.electron_parser_setup_stamp');
  if (process.env.PDF2MD_INSTALL_PARSER === '1' && fs.existsSync(parserReq)) {
    const reqMtime = fs.statSync(parserReq).mtimeMs;
    const stampMtime = fs.existsSync(parserStamp) ? fs.statSync(parserStamp).mtimeMs : 0;
    if (reqMtime > stampMtime) {
      appendLog('Installing parser dependencies (set PDF2MD_INSTALL_PARSER=0 to skip)...\n');
      await run(py, ['-m', 'pip', 'install', '-r', 'requirements-parser.txt']);
      fs.writeFileSync(parserStamp, new Date().toISOString());
    }
  }
}

function stopBackend() {
  if (backend && !backend.killed) {
    backend.kill();
    backend = null;
  }
}

async function killProcessesOnPort() {
  // Prevent accidentally connecting to an old/stale review-ui server that is
  // still bound to the port from a previous launch.
  if (process.platform !== 'darwin' && process.platform !== 'linux') return;
  const res = await run('sh', ['-lc', `lsof -ti tcp:${port} 2>/dev/null || true`]);
  const pids = (res.stdout || '').split(/\s+/).filter(Boolean).filter(pid => String(process.pid) !== pid);
  for (const pid of pids) {
    appendLog(`Killing stale process on port ${port}: ${pid}
`);
    await run('kill', [pid]);
  }
}

async function startBackend() {
  stopBackend();
  await ensureRepoRoot();
  refreshPaths();
  await killProcessesOnPort();
  await ensureVenvAndDeps();
  const py = await pythonCmd();
  appendLog(`Starting backend: ${py} rh2.py review-ui --host ${host} --port ${port}\n`);
  backend = spawn(py, ['rh2.py', 'review-ui', '--host', host, '--port', String(port)], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONUNBUFFERED: '1' }
  });
  backend.stdout.on('data', (d) => appendLog(d.toString()));
  backend.stderr.on('data', (d) => appendLog(d.toString()));
  backend.on('exit', (code, signal) => appendLog(`Backend exited code=${code} signal=${signal}\n`));
}

function waitForServer(timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve(true);
      });
      req.on('error', () => {
        if (Date.now() - started > timeoutMs) reject(new Error(`Timed out waiting for ${url}`));
        else setTimeout(tick, 500);
      });
      req.setTimeout(1000, () => req.destroy());
    };
    tick();
  });
}

async function safeLoadURL(targetUrl) {
  if (!win || win.isDestroyed()) {
    appendLog(`Skip loadURL; window destroyed: ${targetUrl}
`);
    return false;
  }
  try {
    await win.loadURL(targetUrl);
    return true;
  } catch (e) {
    appendLog(`loadURL failed: ${String(e)}
`);
    return false;
  }
}

function errorPage(title, detail) {
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(
    `<body style="font-family:system-ui;background:#111;color:#eee;padding:24px"><h2>${title}</h2><pre style="white-space:pre-wrap;background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:14px">${detail}</pre><p>Use the app menu: Academic Database → Open Logs / Open Repo Folder.</p></body>`
  );
}

function injectUpdateBanner(info) {
  if (!win || win.isDestroyed()) return;
  const payload = JSON.stringify(info || updateInfo || {});
  win.webContents.executeJavaScript(`
    (() => {
      const info = ${payload};
      let b = document.getElementById('academic-db-update-banner');
      if (!b) {
        b = document.createElement('div');
        b.id = 'academic-db-update-banner';
        b.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:99999;background:#f4d06f;color:#18120a;border:1px solid #926d1f;border-radius:12px;padding:12px 14px;box-shadow:0 8px 30px rgba(0,0,0,.35);font-family:system-ui;display:flex;align-items:center;gap:12px;justify-content:space-between';
        document.body.appendChild(b);
      }
      b.innerHTML = '<div><b>Update available</b><div style="font-size:12px;opacity:.8">A newer Git commit is available. Restart to install?</div></div><button id="academic-db-install-update" style="border:0;background:#18120a;color:#fff;border-radius:8px;padding:8px 10px;cursor:pointer">Restart to install</button>';
      document.getElementById('academic-db-install-update').onclick = () => window.academicDB.installUpdate();
    })();
  `).catch(() => {});
}

function injectStatusBanner(title, detail) {
  if (!win || win.isDestroyed()) return;
  const t = JSON.stringify(title || 'Status');
  const d = JSON.stringify(String(detail || ''));
  win.webContents.executeJavaScript(`
    (() => {
      let b = document.getElementById('academic-db-update-banner');
      if (!b) {
        b = document.createElement('div');
        b.id = 'academic-db-update-banner';
        b.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:99999;background:#fecaca;color:#3f1010;border:1px solid #b91c1c;border-radius:12px;padding:12px 14px;box-shadow:0 8px 30px rgba(0,0,0,.35);font-family:system-ui';
        document.body.appendChild(b);
      }
      b.innerHTML = '<b>'+${t}+'</b><div style="font-size:12px;opacity:.85;white-space:pre-wrap">'+${d}+'</div>';
    })();
  `).catch(() => {});
}

async function gitStatus() {
  const st = await run('git', ['status', '--porcelain']);
  return { ok: st.ok, text: st.stdout || '', clean: st.ok && !st.stdout.trim() };
}

async function gitStatusClean() {
  return (await gitStatus()).clean;
}

async function checkForUpdates({ prompt = false } = {}) {
  try { await ensureRepoRoot(); refreshPaths(); } catch (e) {
    if (prompt) await dialog.showMessageBox(win, { type: 'error', message: 'No Git repo available', detail: String(e) });
    else injectStatusBanner('Update check failed', String(e));
    return { available: false, error: String(e) };
  }
  const fetch = await run('git', ['fetch', 'origin', 'main']);
  if (!fetch.ok) {
    const err = fetch.stderr || fetch.stdout || 'git fetch failed';
    if (prompt) await dialog.showMessageBox(win, { type: 'error', message: 'Update check failed', detail: err });
    else injectStatusBanner('Update check failed', err);
    return { available: false, error: err };
  }
  const local = await run('git', ['rev-parse', 'HEAD']);
  const remote = await run('git', ['rev-parse', 'origin/main']);
  if (!local.ok || !remote.ok) return { available: false };
  const localHash = local.stdout.trim();
  const remoteHash = remote.stdout.trim();
  const clean = await gitStatusClean();
  const available = localHash !== remoteHash;
  updateInfo = { available, local: localHash.slice(0, 8), remote: remoteHash.slice(0, 8), clean };
  if (available) {
    if (prompt) {
      const result = await dialog.showMessageBox(win, {
        type: 'info',
        buttons: clean ? ['Restart to install', 'Later'] : ['Stash changes & restart', 'Open repo folder', 'Later'],
        defaultId: 0,
        message: 'Academic Database update available',
        detail: clean ? `Current ${updateInfo.local}, remote ${updateInfo.remote}. Restart to install?` : 'Your working tree has local changes. You can stash them automatically, or open the repo folder to inspect.'
      });
      if (clean && result.response === 0) await installUpdate();
      if (!clean && result.response === 0) await installUpdate({ stashDirty: true });
      if (!clean && result.response === 1) shell.openPath(repoRoot);
    } else {
      injectUpdateBanner(updateInfo);
    }
  }
  return updateInfo;
}

async function installUpdate(options = {}) {
  const status = await gitStatus();
  if (!status.clean) {
    if (!options.stashDirty) {
      const result = await dialog.showMessageBox(win, {
        type: 'warning',
        buttons: ['Stash changes & install update', 'Open repo folder', 'Cancel'],
        defaultId: 0,
        cancelId: 2,
        message: 'Working tree has local changes',
        detail: `Auto-update cannot overwrite local changes. Current changes:

${status.text.slice(0, 3000)}

Stash changes and restart to install?`
      });
      if (result.response === 1) shell.openPath(repoRoot);
      if (result.response !== 0) return { ok: false };
    }
    const stash = await run('git', ['stash', 'push', '-u', '-m', `Academic Database auto-stash before update ${new Date().toISOString()}`]);
    if (!stash.ok) {
      await dialog.showMessageBox(win, { type: 'error', message: 'Could not stash local changes', detail: stash.stderr || stash.stdout || 'git stash failed' });
      return { ok: false };
    }
  }
  stopBackend();
  const pull = await run('git', ['pull', '--ff-only', 'origin', 'main']);
  if (!pull.ok) {
    await dialog.showMessageBox(win, { type: 'error', message: 'Update failed', detail: pull.stderr || pull.stdout });
    await startBackend();
    return { ok: false };
  }
  app.relaunch();
  app.exit(0);
  return { ok: true };
}

function createMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'editMenu' },
    { role: 'viewMenu' },
    {
      label: 'Academic Database',
      submenu: [
        { label: 'Check for Updates…', click: () => checkForUpdates({ prompt: true }) },
        { label: 'Stash Local Changes…', click: async () => { const st = await gitStatus(); if (st.clean) return dialog.showMessageBox(win, { message: 'Working tree is clean' }); const r = await dialog.showMessageBox(win, { type: 'warning', buttons: ['Stash', 'Cancel'], message: 'Stash local changes?', detail: st.text.slice(0,3000) }); if (r.response === 0) await run('git', ['stash', 'push', '-u', '-m', `Manual stash from Academic Database ${new Date().toISOString()}`]); } },
        { label: 'Restart Backend', click: async () => { await startBackend(); await waitForServer(); win.loadURL(url); } },
        { label: 'Show Repo Path', click: () => dialog.showMessageBox(win, { message: 'Current repo path', detail: repoRoot }) },
        { label: 'Open Repo Folder', click: () => shell.openPath(repoRoot) },
        { label: 'Open Logs', click: () => shell.openPath(logFile) },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'quit' }
      ]
    }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1380,
    height: 920,
    title: 'Academic Database',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  win.on('closed', () => { win = null; });
  await safeLoadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<h2 style="font-family:system-ui">Starting Academic Database…</h2><p style="font-family:system-ui">Launching local review server.</p>'));
  createMenu();
  await startBackend();
  try {
    await waitForServer();
    await safeLoadURL(url);
    setTimeout(() => checkForUpdates({ prompt: false }), 2500);
    updateTimer = setInterval(() => checkForUpdates({ prompt: false }), 5 * 60 * 1000);
  } catch (e) {
    const log = fs.existsSync(logFile) ? fs.readFileSync(logFile, 'utf8').slice(-4000) : '';
    await safeLoadURL(errorPage('Backend failed to start', `${String(e)}\n\n${log}`));
  }
}

ipcMain.handle('install-update', () => installUpdate());
ipcMain.handle('check-for-updates', () => checkForUpdates({ prompt: true }));
ipcMain.handle('restart-backend', async () => { await startBackend(); await waitForServer(); await safeLoadURL(url); return { ok: true }; });
ipcMain.handle('open-logs', () => shell.openPath(logFile));
ipcMain.handle('open-repo', () => shell.openPath(repoRoot));

process.on('unhandledRejection', (reason) => { appendLog(`Unhandled rejection: ${String(reason)}\n`); });
process.on('uncaughtException', (err) => { appendLog(`Uncaught exception: ${err && err.stack ? err.stack : String(err)}\n`); });

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) { app.quit(); }
else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(createWindow);
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
}
app.on('before-quit', () => { if (updateTimer) clearInterval(updateTimer); stopBackend(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
