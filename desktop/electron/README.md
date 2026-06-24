# Academic Database Electron Shell

This is a lightweight desktop shell around the local `rh2.py review-ui` server.

## Development

```bash
cd desktop/electron
npm install
npm start
```

The app starts the Python review backend from the repo root, waits for `http://127.0.0.1:8765`, then opens it in an Electron `BrowserWindow`.

## Updates

The app checks `origin/main` on launch and periodically while running. If a newer commit is available, it shows an in-app banner with a **Restart to install** button. Updating is skipped if the working tree has local changes.

## Build

```bash
cd desktop/electron
npm install
npm run build
```

`prepare-build.js` writes the current repo path into `repo-path.json`, so the built app knows which local checkout to launch.

This is intentionally not a fully standalone app: PDFs, SQLite DBs, blobs, parser outputs, and the Python virtualenv remain in the local repo checkout.
