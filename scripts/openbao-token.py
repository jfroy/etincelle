#!/usr/bin/env python3
"""Mint an OpenBao token for a human (web UI or `bao` CLI) from the root token in 1Password.

Usage: ./scripts/openbao-token.py [--ttl 1h] [--policy kantai-admin] [--root] [--addr URL]

Default: use the root token stored in 1Password (item openbao-etincelle) to create an
*orphan* token with the given policy and TTL, and print only the new token on stdout
(everything else goes to stderr, so `BAO_TOKEN=$(...)` works). --root prints the stored
root token itself.

For day-to-day use prefer OIDC login through Pocket ID (`bao login -method=oidc`, or the
"OIDC" method in the UI); this script is for when the kantai cluster (and so Pocket ID)
is down, or for scripted access.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from openbaolib import DEFAULT_ADDR, Bao, BaoError, print_err, root_token_from_1password


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an OpenBao token from the root token in 1Password.")
    parser.add_argument("--ttl", default="1h", help="token TTL (default 1h)")
    parser.add_argument("--policy", default="kantai-admin", help="policy for the token (default kantai-admin)")
    parser.add_argument("--root", action="store_true", help="print the stored root token instead of a scoped one")
    parser.add_argument("--addr", default=DEFAULT_ADDR, help=f"OpenBao API address (default {DEFAULT_ADDR})")
    args = parser.parse_args()

    bao = Bao(args.addr)
    try:
        print_err(f"--> Reading the root token from 1Password ({args.addr})...")
        root_token_from_1password(bao)
        if args.root:
            print(bao.token)
            return
        token = bao.write("auth/token/create-orphan", {
            "policies": [args.policy], "ttl": args.ttl, "display_name": "openbao-token",
        })["auth"]["client_token"]
        print_err(f"    {args.policy} token valid for {args.ttl}:")
        print(token)
    except BaoError as e:
        sys.exit(f"ERROR: {e}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: {' '.join(e.cmd[:3])} failed: {(e.stderr or '').strip()}")


if __name__ == "__main__":
    main()
