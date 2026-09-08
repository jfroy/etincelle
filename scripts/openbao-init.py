#!/usr/bin/env python3
"""One-time (idempotent) OpenBao initialisation for etincelle, run from a workstation.

Usage: ./scripts/openbao-init.py <host> <jwks.json> [--addr URL] [--ssh-user USER]
  <host>       the VM (only used for one ssh step: installing the AppRole env file)
  <jwks.json>  the kantai cluster's service-account keys:
               kubectl get --raw /openid/v1/jwks > kantai-jwks.json

Requires: python3 (stdlib only), the 1Password CLI `op` signed in to the kantai
vault, and ssh access to the host. All OpenBao calls go over its HTTP API.

Steps (each checks before it creates, so re-running is safe):
  1. sys/init with one recovery key (static seal => recovery keys, not unseal keys);
     recovery key + root token stored in 1Password item openbao-etincelle; re-runs
     authenticate with that root token.
  2. KV v2 at kantai/ with max_versions=10 (the file audit device is declared in config.hcl).
  3. policies kantai-eso and openbao-snapshot.
  4. jwt auth with static validation keys from the JWKS; role kantai-eso.
  5. oidc auth against Pocket ID (item openbao-oidc: client-id, client-secret; skipped
     if absent) with role admin -> policy kantai-admin. Only usable while kantai is up.
  6. approle auth; role openbao-snapshot; its role-id/secret-id installed in
     /etc/etincelle/secrets/openbao-snapshot.env on the host and stored in 1Password
     item openbao-snapshot-etincelle for re-provisioning.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys

from openbaolib import (DEFAULT_ADDR, OP_ITEM, OP_VAULT, Bao, BaoError, op, op_read, op_set,
                        root_token_from_1password, wait_unsealed)

OP_SNAPSHOT_ITEM = "openbao-snapshot-etincelle"
OP_OIDC_ITEM = "openbao-oidc"
DEFAULT_OIDC_ISSUER = "https://pid.kantai.xyz"
KV_MOUNT = "kantai"
ESO_SUBJECT = "system:serviceaccount:external-secrets:external-secrets"
SNAPSHOT_ENV = "/etc/etincelle/secrets/openbao-snapshot.env"

POLICIES = {
    "kantai-eso": f"""# external-secrets operator on the kantai cluster: read everything, write only
# under generated/ (PushSecret).
path "{KV_MOUNT}/data/*"     {{ capabilities = ["read", "list"] }}
path "{KV_MOUNT}/metadata/*" {{ capabilities = ["read", "list"] }}
path "{KV_MOUNT}/data/generated/*"     {{ capabilities = ["create", "update", "read", "delete", "list"] }}
path "{KV_MOUNT}/metadata/generated/*" {{ capabilities = ["create", "update", "read", "delete", "list"] }}
""",
    "kantai-admin": f"""# Humans (OIDC via Pocket ID, or a token from `task openbao-token`): full control of
# the kantai KV mount plus what the web UI needs to browse it.
path "{KV_MOUNT}/*"                  {{ capabilities = ["create", "read", "update", "delete", "list", "patch"] }}
path "sys/mounts"                    {{ capabilities = ["read"] }}
path "sys/mounts/*"                  {{ capabilities = ["read"] }}
path "sys/internal/ui/*"             {{ capabilities = ["read", "list"] }}
path "sys/capabilities-self"         {{ capabilities = ["update"] }}
path "auth/token/lookup-self"        {{ capabilities = ["read"] }}
path "auth/token/renew-self"         {{ capabilities = ["update"] }}
path "sys/storage/raft/snapshot"     {{ capabilities = ["read"] }}
""",
    "openbao-snapshot": f"""# openbao-snapshot backup job: Raft snapshot plus logical export of the KV mount.
path "sys/storage/raft/snapshot" {{ capabilities = ["read"] }}
path "{KV_MOUNT}/*"              {{ capabilities = ["read", "list"] }}
""",
}


def ssh(host: str, user: str, command: str, stdin: str | None = None, check: bool = True) -> int:
    return subprocess.run(["ssh", f"{user}@{host}", command], input=stdin, text=True, check=check).returncode


def jwks_to_pems(jwks: dict) -> list[str]:
    """Convert RFC 7517 RSA keys to PEM SubjectPublicKeyInfo (DER built by hand)."""
    def b64u(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def tlv(tag: int, value: bytes) -> bytes:
        n = len(value)
        if n < 128:
            length = bytes([n])
        else:
            lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
            length = bytes([0x80 | len(lb)]) + lb
        return bytes([tag]) + length + value

    def integer(raw: bytes) -> bytes:
        raw = raw.lstrip(b"\x00") or b"\x00"
        return tlv(0x02, b"\x00" + raw if raw[0] & 0x80 else raw)

    pems = []
    for key in jwks.get("keys", []):
        if key.get("kty") != "RSA":
            print(f"    skipping non-RSA key {key.get('kid')}")
            continue
        rsa = tlv(0x30, integer(b64u(key["n"])) + integer(b64u(key["e"])))
        algid = tlv(0x30, bytes.fromhex("06092a864886f70d010101") + b"\x05\x00")  # rsaEncryption, NULL
        spki = tlv(0x30, algid + tlv(0x03, b"\x00" + rsa))
        b64 = base64.b64encode(spki).decode()
        lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
        pems.append(f"-----BEGIN PUBLIC KEY-----\n{lines}\n-----END PUBLIC KEY-----\n")
    if not pems:
        sys.exit("ERROR: no RSA keys found in JWKS")
    return pems


def obtain_root_token(bao: Bao) -> str:
    if bao.read("sys/seal-status").get("initialized"):
        print("--> Already initialised; using the root token from 1Password...")
        return root_token_from_1password(bao)
    print("--> Running sys/init (recovery_shares=1, recovery_threshold=1)...")
    init = bao.request("PUT", "sys/init", {"recovery_shares": 1, "recovery_threshold": 1})
    recovery_key, root_token = init["recovery_keys_base64"][0], init["root_token"]
    print(f"--> Storing recovery key and root token in 1Password ({OP_VAULT}/{OP_ITEM})...")
    op_set(OP_ITEM, {"recovery-key": recovery_key, "root-token": root_token})
    print("    Done.")
    return root_token


def configure_oidc(bao: Bao, issuer: str, group: str | None) -> None:
    """Human login through Pocket ID (only works while the kantai cluster is up)."""
    print("--> Configuring oidc auth (Pocket ID) for humans...")
    client_id = op_read(f"op://{OP_VAULT}/{OP_OIDC_ITEM}/client-id")
    client_secret = op_read(f"op://{OP_VAULT}/{OP_OIDC_ITEM}/client-secret")
    if not client_id or not client_secret:
        print(f"    Skipped: create an OIDC client in Pocket ID and store it as "
              f"op://{OP_VAULT}/{OP_OIDC_ITEM} (fields client-id, client-secret), then re-run.")
        return
    if not bao.has("sys/auth", "oidc/"):
        bao.write("sys/auth/oidc", {"type": "oidc"})
    bao.write("auth/oidc/config", {
        "oidc_discovery_url": issuer, "oidc_client_id": client_id,
        "oidc_client_secret": client_secret, "default_role": "admin",
    })
    role: dict = {
        "role_type": "oidc", "user_claim": "email", "oidc_scopes": ["openid", "email", "profile", "groups"],
        "allowed_redirect_uris": [f"{DEFAULT_ADDR}/ui/vault/auth/oidc/oidc/callback",
                                  "http://localhost:8250/oidc/callback"],
        "token_policies": ["kantai-admin"], "token_ttl": "8h", "token_max_ttl": "24h",
    }
    if group:
        role["bound_claims"] = {"groups": [group]}
    bao.write("auth/oidc/role/admin", role)
    print(f"    Done (issuer {issuer}" + (f", group {group}" if group else ", any user the client allows") + ").")


def configure(bao: Bao, pems: list[str], host: str, ssh_user: str, oidc_issuer: str, oidc_group: str | None) -> None:
    # The file audit device is declared in openbao/config.hcl.
    if not bao.has("sys/audit", "file/"):
        print("    WARN: no file audit device is enabled; check the audit stanza in openbao/config.hcl")

    print(f"--> Mounting KV v2 at {KV_MOUNT}/ ...")
    if bao.has("sys/mounts", f"{KV_MOUNT}/"):
        print("    Already mounted.")
    else:
        bao.write(f"sys/mounts/{KV_MOUNT}", {"type": "kv", "options": {"version": "2"}})
    bao.write(f"{KV_MOUNT}/config", {"max_versions": 10})

    print("--> Writing policies...")
    for name, policy in POLICIES.items():
        bao.write(f"sys/policies/acl/{name}", {"policy": policy})

    print("--> Configuring jwt auth for external-secrets...")
    if not bao.has("sys/auth", "jwt/"):
        bao.write("sys/auth/jwt", {"type": "jwt"})
    print(f"    {len(pems)} validation key(s) from the JWKS")
    bao.write("auth/jwt/config", {"jwt_validation_pubkeys": pems, "jwt_supported_algs": ["RS256"]})
    bao.write("auth/jwt/role/kantai-eso", {
        "role_type": "jwt", "bound_audiences": ["openbao"], "bound_subject": ESO_SUBJECT,
        "user_claim": "sub", "token_policies": ["kantai-eso"], "token_ttl": "1h",
    })

    configure_oidc(bao, oidc_issuer, oidc_group)

    print("--> Configuring approle auth for openbao-snapshot...")
    if not bao.has("sys/auth", "approle/"):
        bao.write("sys/auth/approle", {"type": "approle"})
    bao.write("auth/approle/role/openbao-snapshot", {
        "token_policies": ["openbao-snapshot"], "token_ttl": "15m", "token_max_ttl": "30m",
        "token_bound_cidrs": ["127.0.0.1/32"], "secret_id_bound_cidrs": ["127.0.0.1/32"],
        "secret_id_ttl": 0, "secret_id_num_uses": 0,
    })
    if ssh(host, ssh_user, f"sudo test -s {SNAPSHOT_ENV}", check=False) == 0:
        print(f"    {SNAPSHOT_ENV} already present; keeping the existing secret-id")
        print("    (delete the file on the host and re-run to rotate it).")
        return
    role_id = bao.read("auth/approle/role/openbao-snapshot/role-id")["data"]["role_id"]
    secret_id = bao.write("auth/approle/role/openbao-snapshot/secret-id")["data"]["secret_id"]
    ssh(host, ssh_user, f"sudo install -m 600 -o root -g root /dev/stdin {SNAPSHOT_ENV}",
        stdin=f"OPENBAO_ROLE_ID={role_id}\nOPENBAO_SECRET_ID={secret_id}\n")
    print(f"    Storing credentials in 1Password ({OP_VAULT}/{OP_SNAPSHOT_ITEM})...")
    op_set(OP_SNAPSHOT_ITEM, {"role-id": role_id, "secret-id": secret_id})
    print("    Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialise and configure OpenBao on etincelle.")
    parser.add_argument("host", help="VM hostname or IP (ssh target for the AppRole env file)")
    parser.add_argument("jwks", type=argparse.FileType("r"), help="cluster service-account JWKS (JSON)")
    parser.add_argument("--addr", default=DEFAULT_ADDR, help=f"OpenBao API address (default {DEFAULT_ADDR})")
    parser.add_argument("--ssh-user", default="etincelle")
    parser.add_argument("--oidc-issuer", default=DEFAULT_OIDC_ISSUER, help="Pocket ID issuer URL")
    parser.add_argument("--oidc-group", default=None,
                        help="restrict OIDC login to this Pocket ID group (default: rely on Pocket ID's own client allow-list)")
    args = parser.parse_args()

    pems = jwks_to_pems(json.load(args.jwks))
    bao = Bao(args.addr)
    print(f"==> Initialising OpenBao at {args.addr}")
    try:
        bao.token = obtain_root_token(bao)
        print("--> Waiting for auto-unseal...")
        wait_unsealed(bao)
        configure(bao, pems, args.host, args.ssh_user, args.oidc_issuer, args.oidc_group or None)
    except BaoError as e:
        sys.exit(f"ERROR: {e}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: {' '.join(e.cmd[:3])} failed: {(e.stderr or '').strip()}")
    print(f"==> Done. Test the backup job with: task openbao-snapshot-now HOST={args.host}")


if __name__ == "__main__":
    main()
