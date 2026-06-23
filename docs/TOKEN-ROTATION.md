# Argus bearer token rotation (fleet)

Homelab Argus uses one shared bearer (`ARGUS_API_TOKEN`) for:

- Outbound callers (Mise, Mnemosyne, Lightroom/C1 stubs, scripts)
- Inbound API routes (`Authorization: Bearer …`)
- UI session cookie (`/ui/token` stores the same value)

Rotate on the same cadence as Hermes/Odysseus service tokens — after a leak,
when a contractor offboards, or on a quarterly calendar reminder.

## Generate

```bash
NEW="$(openssl rand -hex 32)"
echo "$NEW"   # copy once; do not commit
```

## Update mickey (Argus host)

```bash
cd ~/ai-workspace/argus
cp .env .env.bak.$(date +%Y%m%d%H%M%S)

# Edit .env
#   ARGUS_API_TOKEN=<NEW>

sudo systemctl restart argus
curl -sf -H "Authorization: Bearer $NEW" http://127.0.0.1:8010/healthz
```

## Update callers (same token everywhere)

| Service | Env var | Host |
|---------|---------|------|
| Mise | `MISE_ARGUS_TOKEN` | flow |
| Mise → Platekit | `MISE_PLATEKIT_API_TOKEN` | flow (if armed) |
| Mnemosyne | `ARGUS_API_TOKEN` | mickey / flow |
| Dionysus | `DIONYSUS_ARGUS_API_TOKEN` | platekit host |

```bash
# flow — Mise
sudo systemctl restart mise

# mickey — Mnemosyne / other consumers
systemctl --user restart <unit>
```

## Lightroom / Capture One

Update plug-in preferences **API token** or `ARGUS_API_TOKEN` in shell hooks.

## UI sessions

Old browser cookies stop working immediately (401 on analyze/corrections).
Operators re-save token at `/` → **Save session**.

## SaaS mode

Tenant API keys are separate from `ARGUS_API_TOKEN`. Admin token still gates
operator routes; rotate tenant keys per workspace in the SaaS dashboard.

## Verify

```bash
# Should 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer OLD" \
  http://mickey:8010/runs/1/export

# Should 200 (when run exists)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $NEW" \
  http://mickey:8010/runs/1/export
```

## Rollback

```bash
cp .env.bak.<timestamp> .env
sudo systemctl restart argus
# restore caller envs to the old token
```