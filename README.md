# STAAD | VIKTOR Plug-In

Push active STAAD connectivity into a VIKTOR application either via the programmatic API or through a small PyQt helper UI.

## Running the PyQt front-end

```bash
uv run python -m frontend.qt_app
```

1. Paste the VIKTOR editor URL (e.g. `https://beta.viktor.ai/workspaces/2539/app/editor/2262`).
2. Paste a valid API token.
3. Click **Push STAAD data** - the app collects geometry from the open STAAD session and uploads it to VIKTOR.

## Build the Windows executable

```bash
uv run --group dev pyinstaller --clean --noconfirm --windowed --name staad_viktor ^
  --paths . ^
  --collect-submodules backend ^
  --collect-submodules comtypes --collect-all PyQt6 ^
  frontend/qt_app.py
```

The bundled app lands in `dist/staad_viktor`; rerun after closing any running exe.
