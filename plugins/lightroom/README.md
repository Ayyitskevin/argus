# Argus Lightroom Export Filter (Phase 8)

Post-export hook for **Lightroom Classic**: after you export a gallery folder,
Argus analyzes the images on mickey and writes `.argus.json` / `.xmp` sidecars
beside your files.

## Prerequisites

- Lightroom Classic 11+ (Lua SDK 6.0+)
- Python 3.11+ with Argus installed (`pip install -e .` from this repo)
- Tailnet reachability to your Argus host (e.g. `http://mickey:8010`)
- `ARGUS_API_TOKEN` bearer (same token as Mise/Hermes fleet)

## Install

1. Copy `Argus.lrplugin` into Lightroom's plug-ins folder:
   - **macOS:** `~/Library/Application Support/Adobe/Lightroom/Plugins/`
   - **Windows:** `%APPDATA%\Adobe\Lightroom\Plugins\`
2. Restart Lightroom → **File → Plug-in Manager** → enable **Argus Vision**.
3. In **Plug-in Manager → Argus Vision → Plug-in Preferences**, set:
   - **Python executable** — e.g. `/usr/bin/python3` or a venv `python`
   - **Argus script** — path to `docs/lightroom_export_stub.py` in this repo
   - **Base URL** — `http://mickey:8010` (or your tailnet hostname)
   - **API token** — your `ARGUS_API_TOKEN`
   - **Client ID** (optional) — feeds learned prefs, e.g. `blue-plate`

## Use

1. Export photos to a folder (normal Lightroom export).
2. In the export dialog, enable **Argus vision analyze** under **Post-Process Actions**.
3. Set **Limit** (max images) and enable **Recursive** for nested selects folders.
4. Export — Lightroom runs the Python stub when export finishes; sidecars land in
   the export directory.

## CLI equivalent (no Lightroom)

```bash
ARGUS_API_TOKEN=secret python docs/lightroom_export_stub.py /path/to/export \
  --base-url http://mickey:8010 --client-id kevin --limit 20 --recursive \
  --manifest-out /path/to/export/manifest.json
```

## Troubleshooting

- **"script not found"** — fix the script path in Plug-in Preferences.
- **401** — token mismatch; rotate `ARGUS_API_TOKEN` on mickey and update prefs.
- **Timeout on large sets** — lower limit or use `POST /jobs` with callback (queue mode).

See also `docs/lightroom_export_stub.py` and `GET /runs/{id}/manifest.json`.