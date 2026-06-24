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

const repoRoot = loadRepoRoot();
const host = process.env.RH_REVIEW_HOST || '127.0.0.1';
const port = Number(process.env.RH_REVIEW_PORT || 8765);
const url = `http://${host}:${port}`;
const reportsDir = path.join(repoRoot, 'reports');
const runDir = path.join(repoRoot, '.run');
const logFile = path.join(reportsDir, 'electron_backend.log');

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
  if (!fs.existsSync(venvPython)) {
    appendLog('Creating .venv...\n');
    await run('python3', ['-m', 'venv', '.venv']);
  }
  const py = await pythonCmd();
  await run(py, ['-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', 'setuptools']);
  const parserReq = path.join(repoRoot, 'requirements-parser.txt');
  const stamp = path.join(repoRoot, '.venv', '.electron_setup_stamp');
  if (process.env.PDF2MD_INSTALL_PARSER !== '0' && fs.existsSync(parserReq)) {
    const reqMtime = fs.statSync(parserReq).mtimeMs;
    const stampMtime = fs.existsSync(stamp) ? fs.statSync(stamp).mtimeMs : 0;
    if (reqMtime > stampMtime) {
      appendLog('Installing parser dependencies...\n');
      await run(py, ['-m', 'pip', 'install', '-r', 'requirements-parser.txt']);
      fs.writeFileSync(stamp, new Date().toISOString());
    }
  }
}

function stopBackend() {
  if (backend && !backend.killed) {
    backend.kill();
    backend = null;
  }
}

async function startBackend() {
  stopBackend();
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

async function gitStatusClean() {
  const st = await run('git', ['status', '--porcelain']);
  return st.ok && !st.stdout.trim();
}

async function checkForUpdates({ prompt = false } = {}) {
  const fetch = await run('git', ['fetch', 'origin', 'main']);
  if (!fetch.ok) return { available: false, error: fetch.stderr || fetch.stdout };
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
        buttons: clean ? ['Restart to install', 'Later'] : ['Working tree dirty — later'],
        defaultId: 0,
        message: 'Academic Database update available',
        detail: clean ? `Current ${updateInfo.local}, remote ${updateInfo.remote}. Restart to install?` : 'Your working tree has local changes, so auto-update is paused.'
      });
      if (clean && result.response === 0) await installUpdate();
    } else {
      injectUpdateBanner(updateInfo);
    }
  }
  return updateInfo;
}

async function installUpdate() {
  const clean = await gitStatusClean();
  if (!clean) {
    await dialog.showMessageBox(win, { type: 'warning', message: 'Cannot update automatically', detail: 'Working tree has local changes. Commit/stash them first.' });
    return { ok: false };
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
    {
      label: 'Academic Database',
      submenu: [
        { label: 'Check for Updates…', click: () => checkForUpdates({ prompt: true }) },
        { label: 'Restart Backend', click: async () => { await startBackend(); await waitForServer(); win.loadURL(url); } },
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
  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<h2 style="font-family:system-ui">Starting Academic Database…</h2><p style="font-family:system-ui">Launching local review server.</p>'));
  createMenu();
  await startBackend();
  try {
    await waitForServer();
    await win.loadURL(url);
    setTimeout(() => checkForUpdates({ prompt: false }), 2500);
    updateTimer = setInterval(() => checkForUpdates({ prompt: false }), 5 * 60 * 1000);
  } catch (e) {
    const log = fs.existsSync(logFile) ? fs.readFileSync(logFile, 'utf8').slice(-4000) : '';
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<h2>Backend failed to start</h2><pre>${String(e)}\n\n${log}</pre>`));
  }
}

ipcMain.handle('install-update', () => installUpdate());
ipcMain.handle('check-for-updates', () => checkForUpdates({ prompt: true }));
ipcMain.handle('restart-backend', async () => { await startBackend(); await waitForServer(); await win.loadURL(url); return { ok: true }; });
ipcMain.handle('open-logs', () => shell.openPath(logFile));
ipcMain.handle('open-repo', () => shell.openPath(repoRoot));

app.whenReady().then(createWindow);
app.on('before-quit', () => { if (updateTimer) clearInterval(updateTimer); stopBackend(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
