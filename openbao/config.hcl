# OpenBao server configuration. Baked into the image at /etc/openbao/config.hcl
# and bind-mounted into the container at /openbao/config/config.hcl.

ui = true

# Recommended with integrated (Raft) storage: bbolt mmaps the database and
# mlock would pin all of it in memory. The container user is unprivileged and
# could not mlock anyway.
disable_mlock = true

# Public address (Caddy terminates TLS and proxies to the plaintext listener).
api_addr     = "https://bao.etincelle.cloud"
cluster_addr = "https://127.0.0.1:8201"

listener "tcp" {
  address     = "127.0.0.1:8200"
  tls_disable = true
  x_forwarded_for_authorized_addrs = ["127.0.0.1"]
}

storage "raft" {
  path    = "/openbao/data"
  node_id = "etincelle"
}

# Auto-unseal with a static 32-byte key provisioned post-install at
# /etc/etincelle/secrets/openbao-seal.key (64 hex characters, no trailing
# newline). Rotate by adding previous_key/previous_key_id and bumping the id.
seal "static" {
  current_key_id = "etincelle-2026-09"
  current_key    = "file:///openbao/secrets/seal.key"
}
