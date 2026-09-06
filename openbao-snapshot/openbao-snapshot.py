#!/usr/bin/env python3
"""OpenBao backup: Raft snapshot + logical KV export, age-encrypted, uploaded to R2.

Entrypoint of the ghcr.io/jfroy/openbao-snapshot-etincelle image, run daily on
the host by openbao-snapshot.timer via the openbao-snapshot.container quadlet.
Retention is an R2 lifecycle rule, not handled here.

Environment (from /etc/etincelle/secrets/openbao-snapshot.env and
openbao-backup.env via the quadlet):
  OPENBAO_ROLE_ID, OPENBAO_SECRET_ID   AppRole `openbao-snapshot` credentials
  OPENBAO_BACKUP_BUCKET                R2 bucket name
  RCLONE_CONFIG_R2_*                   rclone `r2:` remote (s3 / Cloudflare)
Optional: BAO_ADDR (http://127.0.0.1:8200), OPENBAO_KV_MOUNT (kantai),
  OPENBAO_AGE_RECIPIENT_FILE (/etc/openbao/backup-age.pub),
  NODE_EXPORTER_TEXTFILE_DIR (/var/node-exporter/textfile),
  OPENBAO_BACKUP_HOST (object name prefix; default: this container's hostname)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

METRIC = "openbao_snapshot"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"ERROR: {name} is not set (see /etc/etincelle/secrets/openbao-*.env)")
    return value


class Bao:
    def __init__(self, addr: str, token: str = "") -> None:
        self.addr, self.token = addr.rstrip("/"), token

    def request(self, method: str, path: str, data: dict | None = None):
        headers = {"X-Vault-Token": self.token} if self.token else {}
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.addr}/v1/{path}", data=body, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=120)

    def json(self, method: str, path: str, data: dict | None = None) -> dict:
        with self.request(method, path, data) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def download(self, path: str, dest: str) -> None:
        with self.request("GET", path) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def age_encrypt(recipient: str, src: str) -> str:
    dst = src + ".age"
    subprocess.run(["age", "-r", recipient, "-o", dst, src], check=True)
    os.remove(src)
    return dst


def upload(src: str, dest: str) -> None:
    subprocess.run(["rclone", "copyto", src, dest], check=True)
    print(f"    Uploaded {dest}")


def export_kv(bao: Bao, mount: str, prefix: str = "") -> list[dict]:
    """Recursively read every secret (latest version) under the KV v2 mount."""
    try:
        keys = bao.json("LIST", f"{mount}/metadata/{prefix}")["data"]["keys"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    secrets = []
    for key in keys:
        if key.endswith("/"):
            secrets += export_kv(bao, mount, prefix + key)
            continue
        try:
            data = bao.json("GET", f"{mount}/data/{prefix}{key}")["data"]
        except urllib.error.HTTPError as e:
            if e.code != 404:  # soft-deleted latest version
                raise
            print(f"    WARN: skipping {prefix}{key} (deleted or unreadable)")
            continue
        secrets.append({"path": prefix + key, "data": data["data"], "metadata": data["metadata"]})
    return secrets


def write_metric(textfile_dir: str, count: int) -> None:
    if not os.path.isdir(textfile_dir):
        return
    body = (f"# HELP {METRIC}_last_success_timestamp_seconds Unix time of the last successful OpenBao backup.\n"
            f"# TYPE {METRIC}_last_success_timestamp_seconds gauge\n"
            f"{METRIC}_last_success_timestamp_seconds {int(time.time())}\n"
            f"# HELP {METRIC}_kv_secrets Number of KV secrets in the last logical export.\n"
            f"# TYPE {METRIC}_kv_secrets gauge\n"
            f"{METRIC}_kv_secrets {count}\n")
    tmp = os.path.join(textfile_dir, f"{METRIC}.prom.{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, os.path.join(textfile_dir, f"{METRIC}.prom"))


def main() -> None:
    role_id, secret_id, bucket = env("OPENBAO_ROLE_ID"), env("OPENBAO_SECRET_ID"), env("OPENBAO_BACKUP_BUCKET")
    mount = env("OPENBAO_KV_MOUNT", "kantai")
    recipient_file = env("OPENBAO_AGE_RECIPIENT_FILE", "/etc/openbao/backup-age.pub")
    textfile_dir = env("NODE_EXPORTER_TEXTFILE_DIR", "/var/node-exporter/textfile")
    bao = Bao(env("BAO_ADDR", "http://127.0.0.1:8200"))

    with open(recipient_file) as f:  # refuse to run with the placeholder shipped in the repo
        recipients = [line.strip() for line in f if re.fullmatch(r"age1[0-9a-z]+", line.strip())]
    if not recipients:
        sys.exit(f"ERROR: {recipient_file} holds no age recipient (replace the placeholder in openbao/backup-age.pub)")
    recipient = recipients[0]

    host = env("OPENBAO_BACKUP_HOST", os.uname().nodename.split(".")[0])
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = f"r2:{bucket}"

    print("--> Logging in with AppRole...")
    bao.token = bao.json("POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id})["auth"]["client_token"]
    try:
        # Plaintext only ever lives in this 0700 temp dir (on the container's /tmp tmpfs).
        with tempfile.TemporaryDirectory(prefix="openbao-snapshot.") as tmp:
            print("--> Taking Raft snapshot...")
            snap = os.path.join(tmp, "raft.snap")
            bao.download("sys/storage/raft/snapshot", snap)
            if os.path.getsize(snap) == 0:
                sys.exit("ERROR: empty snapshot")
            upload(age_encrypt(recipient, snap), f"{dest}/raft/{host}-{stamp}.snap.age")

            print(f"--> Exporting KV mount {mount}/ ...")
            secrets = export_kv(bao, mount)
            export = os.path.join(tmp, "kv.json")
            with open(export, "w") as f:
                json.dump({"exported_at": stamp, "host": host, "mount": mount, "secrets": secrets}, f)
            upload(age_encrypt(recipient, export), f"{dest}/kv/{host}-{stamp}.json.age")
            print(f"    {len(secrets)} secrets exported")
        write_metric(textfile_dir, len(secrets))
    finally:
        try:
            bao.json("POST", "auth/token/revoke-self")
        except (urllib.error.URLError, OSError):
            pass
    print("==> Done.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: {e.geturl()}: HTTP {e.code}: {e.read().decode(errors='replace')}")
    except (urllib.error.URLError, subprocess.CalledProcessError, OSError) as e:
        sys.exit(f"ERROR: {e}")
