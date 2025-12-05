# STAAD | VIKTOR Plug-In

Push active STAAD connectivity into a VIKTOR application either via the programmatic API or through a small PyQt helper UI.

## Running the PyQt front-end

```bash
uv run python -m frontend.qt_app
```

1. Paste the VIKTOR editor URL (e.g. `https://beta.viktor.ai/workspaces/2539/app/editor/2262`).
2. Paste a valid API token.
3. Click **Push STAAD data** – the app collects geometry from the open STAAD session and uploads it to VIKTOR.
