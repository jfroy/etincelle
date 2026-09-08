"""Shared helpers for the openbao-*.py scripts: HTTP client, 1Password CLI, root-token minting."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ADDR = "https://bao.etincelle.cloud"
OP_VAULT = "kantai"
OP_ITEM = "openbao-etincelle"


class BaoError(Exception):
    pass


class Bao:
    """Minimal OpenBao HTTP API client; raises BaoError with the body on non-2xx."""

    def __init__(self, addr: str, token: str | None = None) -> None:
        self.addr = addr.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, data: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Vault-Token"] = self.token
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.addr}/v1/{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise BaoError(f"{method} {path}: HTTP {e.code}: {e.read().decode(errors='replace')}") from None
        except urllib.error.URLError as e:
            raise BaoError(f"{method} {path}: {e.reason}") from None
        return json.loads(raw) if raw else {}

    def read(self, path: str) -> dict:
        return self.request("GET", path)

    def write(self, path: str, data: dict | None = None) -> dict:
        return self.request("POST", path, data if data is not None else {})

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)

    def has(self, listing_path: str, key: str) -> bool:
        resp = self.read(listing_path)
        return key in resp.get("data", resp)


def op(*args: str) -> str:
    return subprocess.run(["op", *args], check=True, capture_output=True, text=True).stdout.strip()


def op_read(ref: str) -> str | None:
    """`op read` a secret reference; None if the item or field does not exist."""
    proc = subprocess.run(["op", "read", ref], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def op_set(item: str, fields: dict[str, str]) -> None:
    """Create or update a Secure Note with the given password fields."""
    assignments = [f"{k}[password]={v}" for k, v in fields.items()]
    exists = subprocess.run(["op", "item", "get", item, "--vault", OP_VAULT],
                            capture_output=True).returncode == 0
    if exists:
        op("item", "edit", item, "--vault", OP_VAULT, *assignments)
    else:
        op("item", "create", "--vault", OP_VAULT, "--category", "Secure Note", "--title", item, *assignments)


def wait_unsealed(bao: Bao) -> None:
    for _ in range(30):
        if not bao.read("sys/seal-status").get("sealed", True):
            return
        time.sleep(1)
    sys.exit("ERROR: server still sealed; check the seal key and 'journalctl -u openbao'")


def root_token_from_1password(bao: Bao) -> str:
    """The root token stored by openbao-init at sys/init time (server must be initialised)."""
    if not bao.read("sys/seal-status").get("initialized"):
        sys.exit("ERROR: OpenBao is not initialised; run 'task openbao-init' first")
    token = op_read(f"op://{OP_VAULT}/{OP_ITEM}/root-token")
    if not token or token.startswith("revoked"):
        sys.exit(f"ERROR: no usable root token at op://{OP_VAULT}/{OP_ITEM}/root-token")
    bao.token = token
    try:
        bao.read("auth/token/lookup-self")
    except BaoError as e:
        sys.exit(f"ERROR: stored root token rejected ({e}); create a new one from a sudo token "
                 "with 'bao token create -policy=root' and update the 1Password item")
    return token


def print_err(msg: str) -> None:
    print(msg, file=sys.stderr)
