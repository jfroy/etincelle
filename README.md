# ✨ etincelle

Bootstrap utility server for the [kantai](https://github.com/jfroy/flatops) Kubernetes cluster. Runs Fedora image-mode (bootc), managed via GitOps — push to `main` builds and publishes a new OS image.

## Services

| Service | Purpose |
|---------|---------|
| [Talos discovery service](https://github.com/siderolabs/discovery-service) | Node discovery for Talos cluster bootstrap (`ds.etincelle.cloud`) |
| [Talos image factory](https://github.com/siderolabs/image-factory) | Builds custom Talos OS images for cluster nodes (`tif.etincelle.cloud`) |
| [Distribution registry](https://github.com/distribution/distribution) | OCI registry used as image factory artifact cache (`registry.etincelle.cloud`) |
| [OpenBao](https://openbao.org) | Secrets store for the cluster's external-secrets operator (`bao.etincelle.cloud`); Raft storage, static-key auto-unseal, daily encrypted backups to R2 |
| OpenBao snapshot job | Daily Raft snapshot + KV export, age-encrypted and uploaded to Cloudflare R2 (`openbao-snapshot/`, image `ghcr.io/jfroy/openbao-snapshot-etincelle`, run by `openbao-snapshot.timer`) |
| [Caddy](https://caddyserver.com) | Reverse proxy with automatic TLS via Cloudflare DNS-01 |
| [Beszel agent](https://beszel.dev) | System and container metrics agent reporting to an external hub |
| [Prometheus node exporter](https://github.com/prometheus/node_exporter) | Host metrics on `:9100` (default collectors + textfile collector from `/var/node-exporter/textfile`) |
| [Trove agent](https://github.com/Techdox/trove) | Read-only container inventory agent reporting to an external server |

## Build

Produces a bootable qcow2 from the current `Containerfile` via [bootc-image-builder](https://github.com/osbuild/bootc-image-builder). Output lands at `output/qcow2/disk.qcow2` (10 GiB virtual, ~1.2 GiB sparse).

Prerequisites:

- `podman`, `qemu-img`, `go-task`
- **Linux**: SELinux-enforcing hosts need `osbuild-selinux` installed. The build runs rootless (no `sudo`) using bootc-image-builder's `--in-vm` KVM mode, so `/dev/kvm` must be accessible to the invoking user.
- **macOS**: rootful `podman machine` is required (rootless `--in-vm` cannot reach KVM inside Apple's hypervisor):

  ```sh
  podman machine init --cpus 2 --memory 2048 --disk-size 40
  podman machine set --rootful
  podman machine start
  ```

Build:

```sh
task bake
```

The bake task pulls `ghcr.io/jfroy/etincelle:latest` for the host architecture (amd64 or arm64; the GitHub Actions workflow publishes both) and builds a matching qcow2.

## Deploy

One-time install on a fresh VM:

1. **(Optional) Resize the qcow2** before first boot. The image ships at 10 GiB; the root partition auto-grows to fill the disk on first boot via `systemd-growfs`, but the underlying disk must be enlarged first:

   ```sh
   qemu-img resize output/qcow2/disk.qcow2 100G
   ```

2. **Boot the VM.** Any UEFI-capable hypervisor works (UTM on Apple Silicon, `virt-install`/libvirt on Linux, etc.). The image has no BIOS fallback — UEFI is required.

3. **SSH in** as the user defined in `config.toml` (currently `etincelle`). The key in `config.toml` is the only authorized credential; there is no console login or password.

   ```sh
   ssh etincelle@<vm-ip>
   ```

4. **Provision secrets and join Tailscale.** Requires the [1Password CLI](https://developer.1password.com/docs/cli/) signed in to the `kantai` vault on the workstation running the task:

   ```sh
   task provision HOST=<vm-ip>
   ```

   This installs `/etc/image-factory/keys/*`, `/etc/etincelle/secrets/caddy.env`, `/etc/etincelle/secrets/beszel-agent.env`, `/etc/etincelle/secrets/trove-agent.env`, `/etc/etincelle/secrets/openbao-seal.key`, `/etc/etincelle/secrets/openbao-backup.env` (and `openbao-snapshot.env` once it exists in 1Password) on the VM, starts `caddy.service`, `image-factory.service`, `beszel-agent.service`, `trove-agent.service`, and `openbao.service`, then prompts for a Tailscale auth key and runs `tailscale up`. Pass the key non-interactively with `TS_AUTHKEY=tskey-...`; submit an empty key to skip.

   If the OpenBao seal key does not exist in 1Password yet, the script prints the `op item create` command and offers to generate it (a 32-byte key stored as 64 hex characters). The key must be in 1Password before OpenBao is ever started with it: without it the Raft data can never be unsealed again.

5. **Initialise OpenBao** (first boot only; safe to re-run). Requires `op`, `python3` (stdlib only) and the cluster's service-account JWKS; talks to `https://bao.etincelle.cloud` directly, so DNS/TLS must already work:

   ```sh
   kubectl get --raw /openid/v1/jwks > kantai-jwks.json
   task openbao-init HOST=<vm-ip> JWKS=kantai-jwks.json
   ```

   See [OpenBao](#openbao) below for what this sets up.

## Updates

Ongoing updates are automatic: pushes to `main` build a new image via GitHub Actions, and `bootc-fetch-apply-updates.timer` on the VM applies it on the next interval (reboots into the new deployment).

Two independent mechanisms are at work:

| Timer | Scope | Default schedule |
|-------|-------|------------------|
| `bootc-fetch-apply-updates.timer` | The OS image itself (`ghcr.io/jfroy/etincelle`), including quadlet units and baked-in service configs | `OnBootSec=1h`, then every `8h` with up to `2h` of jitter — worst case ~10h between checks |
| `podman-auto-update.timer` | Container images referenced by quadlets marked `AutoUpdate=registry` (including the one-shot `openbao-snapshot` job image) | `OnCalendar=daily` with 15m jitter |

Because quadlet units live in the OS image at `/etc/containers/systemd/`, adding or changing a service requires a bootc update and reboot, not just a container image pull.

To apply a freshly built image immediately instead of waiting for the timer:

```sh
sudo bootc upgrade --check   # is a newer image available?
sudo bootc upgrade --apply   # fetch, stage, and reboot into it
```

Other useful commands on the host:

```sh
sudo bootc status                                    # booted, staged, and rollback deployments
sudo bootc upgrade                                   # stage without rebooting (applies on next boot)
sudo bootc rollback && sudo systemctl reboot         # boot the previous deployment
sudo podman auto-update                              # pull updated container images now
systemctl list-timers 'bootc-*' 'podman-auto-update*'
journalctl -u bootc-fetch-apply-updates.service      # diagnose failed pulls
```

## OpenBao

OpenBao runs as `openbao.service` (quadlet `containers/systemd/openbao.container`, config `openbao/config.hcl` baked to `/etc/openbao/config.hcl`) with single-node integrated Raft storage in `/var/openbao/data`, a file audit log in `/var/openbao/audit`, and a plaintext listener on `127.0.0.1:8200` that Caddy exposes as `https://bao.etincelle.cloud`. It auto-unseals with the [static seal](https://openbao.org/docs/configuration/seal/static/) using the 32-byte key in `/etc/etincelle/secrets/openbao-seal.key` (`current_key_id = "etincelle-2026-09"`; rotate by adding `previous_key`/`previous_key_id` and bumping the id). The web UI is enabled at `https://bao.etincelle.cloud/ui/`; see *Human access* below for how to sign in.

### CLI

Nothing is installed on the host: run the `bao` CLI inside the container, pointed at the local listener (add `-e BAO_TOKEN=...` to authenticate):

```sh
sudo podman exec -e BAO_ADDR=http://127.0.0.1:8200 openbao bao status
sudo podman exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN=... openbao bao kv list -mount=kantai /
```

### Initialisation (`task openbao-init`)

`scripts/openbao-init.py` drives the HTTP API (`--addr`, default `https://bao.etincelle.cloud`): it initialises the server (`sys/init` with `recovery_shares=1`, `recovery_threshold=1`; with an auto-seal, init yields a *recovery* key rather than unseal keys), stores the recovery key and root token in the 1Password item `openbao-etincelle`, then configures:

| Object | Detail |
|--------|--------|
| Audit device | `file` at `/openbao/logs/audit.log` (host: `/var/openbao/audit/audit.log`) |
| KV v2 mount | `kantai/`, `max_versions=10` |
| Policy `kantai-eso` | read+list on `kantai/data/*` and `kantai/metadata/*`; create/update/read/delete/list under `kantai/data/generated/*` and `kantai/metadata/generated/*` (PushSecret) |
| Policy `openbao-snapshot` | `read` on `sys/storage/raft/snapshot`; read+list on `kantai/*` |
| Auth `jwt` | Static validation keys (`jwt_validation_pubkeys`) converted from the cluster JWKS — no callback to the cluster. Role `kantai-eso`: `role_type=jwt`, `bound_audiences=["openbao"]`, `bound_subject=system:serviceaccount:external-secrets:external-secrets`, `user_claim=sub`, `token_policies=kantai-eso`, `token_ttl=1h` |
| Auth `approle` | Role `openbao-snapshot` (policy `openbao-snapshot`, 15m tokens, bound to `127.0.0.1/32`); role-id/secret-id written to `/etc/etincelle/secrets/openbao-snapshot.env` and to the 1Password item `openbao-snapshot-etincelle` |

Every step checks before it creates, so re-running the script is the way to re-apply policies or load a rotated cluster JWKS. The root token is revoked at the end of the run: on re-runs (and for break-glass access) a fresh one is generated from the recovery key via `sys/generate-root` (the script does this automatically; by hand: `bao operator generate-root`). The only ssh step is installing the AppRole env file on the host.

The external-secrets `ClusterSecretStore` on the cluster should use the `vault` provider with `server: https://bao.etincelle.cloud`, `path: kantai`, `version: v2`, and `auth.jwt` with `role: kantai-eso` and a `kubernetesServiceAccountToken` for service account `external-secrets/external-secrets` with `audiences: [openbao]`. Rotating the cluster's service-account signing key requires re-running `task openbao-init` with the new JWKS.

### Human access (UI and CLI)

Two ways in, both granting the `kantai-admin` policy (full control of `kantai/*`, read-only on what the UI needs):

- **OIDC via Pocket ID** — the everyday path, but it depends on the kantai cluster being up. One-time setup in Pocket ID: create an OIDC client named *OpenBao* with callback URLs `https://bao.etincelle.cloud/ui/vault/auth/oidc/oidc/callback` and `http://localhost:8250/oidc/callback` (the latter for `bao login -method=oidc`), restrict it to the user groups that should administer secrets, and store its credentials in the 1Password item `openbao-oidc` (fields `client-id`, `client-secret`). `task openbao-init` then mounts `oidc/` against `https://pid.kantai.xyz` with role `admin` (`user_claim: email`, scopes `openid email profile groups`, 8 h tokens); pass `OIDC_GROUP=<group>` to additionally bind the role to a Pocket ID group. In the UI pick method *OIDC* (mount path `oidc`); from a workstation with the `bao` CLI installed, `BAO_ADDR=https://bao.etincelle.cloud bao login -method=oidc` (it opens the browser; the host wrapper cannot).
- **Token from the recovery key** — works with the cluster down. `task openbao-token` (`TTL=1h`, `POLICY=kantai-admin`, or `ROOT=1` for an unscoped root token) reads the recovery key from 1Password, mints a root token, uses it to create an orphan token with that policy and TTL, revokes the root token, and prints only the new token, so `export BAO_TOKEN=$(task --silent openbao-token)` works. Paste it into the UI's *Token* method or use it with `bao`.

### Backups

`openbao-snapshot.timer` (daily, up to 1h jitter, `Persistent=true`) starts the one-shot quadlet `containers/systemd/openbao-snapshot.container`, an Alpine image with python3, age and rclone built from `openbao-snapshot/` (`openbao-snapshot.py`, stdlib Python) by `.github/workflows/openbao-snapshot.yml`. It runs with host networking (the AppRole is bound to `127.0.0.1/32`) and logs in with the `openbao-snapshot` AppRole, takes a Raft snapshot (`GET /v1/sys/storage/raft/snapshot`) and a logical JSON export of every secret in `kantai/`, encrypts both with [age](https://age-encryption.org) to the recipient in `openbao/backup-age.pub`, and uploads them with rclone to the R2 bucket:

- `r2:<bucket>/raft/<host>-<timestamp>.snap.age`
- `r2:<bucket>/kv/<host>-<timestamp>.json.age`

Plaintext only exists on the container's `/tmp` tmpfs and is removed on exit; the age recipient file is baked into the host image at `/etc/openbao/backup-age.pub` and bind-mounted read-only. On success the script writes `openbao_snapshot_last_success_timestamp_seconds` (and `openbao_snapshot_kv_secrets`) to `/var/node-exporter/textfile/openbao_snapshot.prom`, so a stale timestamp can be alerted on via node exporter. Retention is not handled here: set an R2 object lifecycle rule on the bucket (e.g. delete objects under `raft/` after 30 days and `kv/` after 90 days).

Before the first run, replace the placeholder in `openbao/backup-age.pub` with a real age recipient (`age-keygen`), keep the private key in 1Password (`openbao-backup-age`), and create the R2 bucket plus an API token scoped to it; the script refuses to run while the placeholder is in place. Run it on demand with `task openbao-snapshot-now HOST=<vm-ip>`.

### Restore

On a fresh VM (or after losing `/var/openbao`):

1. `task provision HOST=<vm-ip>` — installs the same seal key (the snapshot is encrypted with it; a different key cannot open it) and starts `openbao.service`.
2. Copy the latest snapshot to the host and decrypt it with the age private key:

   ```sh
   rclone copyto r2:<bucket>/raft/<host>-<timestamp>.snap.age ./raft.snap.age
   age -d -i openbao-backup.key -o raft.snap raft.snap.age
   scp raft.snap etincelle@<vm-ip>:/tmp/raft.snap
   ```

3. Initialise the empty server (the resulting keys are discarded by the restore) and restore, forcing past the seal consistency check:

   ```sh
   bao() { sudo podman exec -e BAO_ADDR=http://127.0.0.1:8200 ${BAO_TOKEN:+-e BAO_TOKEN="$BAO_TOKEN"} openbao bao "$@"; }
   bao operator init -recovery-shares=1 -recovery-threshold=1
   export BAO_TOKEN=<init root token>
   sudo podman cp /tmp/raft.snap openbao:/tmp/raft.snap
   bao operator raft snapshot restore -force /tmp/raft.snap
   sudo systemctl restart openbao.service && bao status
   ```

   After the restore the server holds the snapshot's data, policies and auth config, and the recovery key from 1Password (item `openbao-etincelle`) is the valid one again. Re-run `task provision` to reinstall `openbao-snapshot.env` (or `task openbao-init` to mint new AppRole credentials), then `shred -u /tmp/raft.snap` on the host.

The `kv/*.json.age` logical exports are a last resort for when a Raft snapshot cannot be restored (e.g. a lost seal key): decrypt one and `bao kv put` each entry into a freshly initialised server.

### Break-glass: what lives in 1Password (`kantai` vault)

| Item | Fields | Purpose |
|------|--------|---------|
| `openbao-etincelle` | `seal-key` (64 hex chars), `recovery-key`, `root-token` (revoked after init) | Unseal the Raft data; generate a new root token (`bao operator generate-root`) |
| `openbao-backup-r2` | `access-key-id`, `secret-access-key`, `endpoint` (`https://<account-id>.r2.cloudflarestorage.com`), `bucket` | rclone access to the backup bucket |
| `openbao-backup-age` | age private key (`AGE-SECRET-KEY-...`) | Decrypt backups (the public recipient is `openbao/backup-age.pub`) |
| `openbao-snapshot-etincelle` | `role-id`, `secret-id` | AppRole used by the backup job (regenerable with `task openbao-init`) |
| `openbao-oidc` | `client-id`, `client-secret` | Pocket ID OIDC client for human login (`task openbao-init`, optional) |

Losing the seal key makes every Raft snapshot unreadable; losing the age key makes every backup unreadable. Keep both.

## Secrets and host state

Provisioned post-install by `scripts/provision-secrets.sh`, never committed to this repo:

- `/etc/image-factory/keys/` — Talos image factory signing keys
- `/etc/etincelle/secrets/caddy.env` — Cloudflare API token for ACME DNS challenge
- `/etc/etincelle/secrets/beszel-agent.env` — Beszel agent `TOKEN`
- `/etc/etincelle/secrets/trove-agent.env` — Trove agent `TROVE_TOKEN`
- `/etc/etincelle/secrets/openbao-seal.key` — OpenBao static seal key (32 bytes as 64 hex chars, owned by the container UID)
- `/etc/etincelle/secrets/openbao-backup.env` — R2 credentials/endpoint/bucket for `openbao-snapshot` (`RCLONE_CONFIG_R2_*`, `OPENBAO_BACKUP_BUCKET`)
- `/etc/etincelle/secrets/openbao-snapshot.env` — `openbao-snapshot` AppRole `OPENBAO_ROLE_ID`/`OPENBAO_SECRET_ID` (written by `task openbao-init`)
- `/var/openbao/data/` — OpenBao Raft storage (the secrets themselves; backed up daily to R2)
- `/var/openbao/audit/` — OpenBao audit log
- `/var/lib/tailscale/` — Tailscale node identity (created on first `tailscale up`)

The image grants passwordless `sudo` to the `wheel` group via `/etc/sudoers.d/wheel-nopasswd`, so the user defined in `config.toml` (currently `etincelle`) can run privileged commands without a password.
