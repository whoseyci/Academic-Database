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


## Repo path and fallback clone

The app is a shell around a Git checkout. In dev builds, `repo-path.json` points at the checkout that built the app. If that path is not a Git repo on the current Mac, the app now falls back to cloning `https://github.com/whoseyci/Academic-Database.git` into Electron's user-data folder and uses that checkout.

Use **Academic Database → Show Repo Path** in the app menu to see which checkout the app is controlling. Run `git pull origin main` only from that folder (or any other real clone containing a `.git` directory), not from the `.app` bundle or a renamed folder without `.git`.
