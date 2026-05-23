# ✨ etincelle

Bootstrap utility server for the [flatops](https://github.com/jfroy/flatops) Kubernetes cluster. Runs Fedora image-mode (bootc), managed via GitOps — push to `main` builds and publishes a new OS image to ghcr.io.

## Services

| Service | Purpose |
|---------|---------|
| [Talos discovery service](https://github.com/siderolabs/discovery-service) | Node discovery for Talos cluster bootstrap (`ds.etincelle.cloud`) |
| [Talos image factory](https://github.com/siderolabs/image-factory) | Builds custom Talos OS images for cluster nodes (`tif.etincelle.cloud`) |
| [Distribution registry](https://github.com/distribution/distribution) | OCI registry used as image factory artifact cache (`registry.etincelle.cloud`) |
| [Caddy](https://caddyserver.com) | Reverse proxy with automatic TLS via Cloudflare DNS-01 |
| [Beszel agent](https://beszel.dev) | System and container metrics agent reporting to an external hub |

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

   This installs `/etc/image-factory/keys/*`, `/etc/etincelle/secrets/caddy.env`, and `/etc/etincelle/secrets/beszel-agent.env` on the VM, starts `caddy.service`, `image-factory.service`, and `beszel-agent.service`, then prompts for a Tailscale auth key and runs `tailscale up`. Pass the key non-interactively with `TS_AUTHKEY=tskey-...`; submit an empty key to skip.

Ongoing updates are automatic: pushes to `main` build a new image via GitHub Actions, and `bootc-fetch-apply-updates.timer` on the VM applies it on the next interval (reboots into the new deployment).

## Secrets and host state

Provisioned post-install by `scripts/provision-secrets.sh`, never committed to this repo:

- `/etc/image-factory/keys/` — Talos image factory signing keys
- `/etc/etincelle/secrets/caddy.env` — Cloudflare API token for ACME DNS challenge
- `/etc/etincelle/secrets/beszel-agent.env` — Beszel agent `TOKEN`
- `/var/lib/tailscale/` — Tailscale node identity (created on first `tailscale up`)

The image grants passwordless `sudo` to the `wheel` group via `/etc/sudoers.d/wheel-nopasswd`, so the user defined in `config.toml` (currently `etincelle`) can run privileged commands without a password.
