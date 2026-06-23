# Argus + Capture One (Phase 8 spike)

Capture One has no Lua export filter like Lightroom. The v1 integration is a
**post-export shell hook** that reuses the same Python stub as Lightroom.

## Flow

1. Export selects to a folder in Capture One (same as your normal delivery export).
2. Run `argus_post_export.sh` on that folder (manually, via macOS Folder Action,
   or a C1 **Process Recipe** terminal hook on export completion).
3. The script calls `docs/lightroom_export_stub.py` → Argus `analyze-folder` →
   pulls `.argus.json` / `.xmp` sidecars beside the files.

## Setup

```bash
chmod +x plugins/capture-one/argus_post_export.sh

export ARGUS_BASE_URL=http://mickey:8010
export ARGUS_API_TOKEN=your-bearer
export ARGUS_CLIENT_ID=blue-plate   # optional prefs client

./plugins/capture-one/argus_post_export.sh /path/to/exported/gallery
```

Environment overrides:

| Variable | Default |
|----------|---------|
| `ARGUS_SCRIPT` | `docs/lightroom_export_stub.py` (repo root) |
| `ARGUS_PYTHON` | `python3` |
| `ARGUS_LIMIT` | `20` |
| `ARGUS_RECURSIVE` | `false` — set `true` for nested selects |

## Capture One recipe hook (macOS)

On export completion, some studios attach an **AppleScript / shell** action to the
process recipe. Point it at this script with the export destination as `$1`.

If your C1 version only supports “Open with Application”, create a `.command` file:

```bash
#!/bin/bash
cd /path/to/argus/repo
./plugins/capture-one/argus_post_export.sh "$1"
```

## Requirements

Same as Lightroom: tailnet URL to mickey, bearer token, Python 3.11+ with Argus
installed. HEIC/RAW in the export folder are supported when `pillow-heif` and/or
`exiftool` are on PATH (see root README).