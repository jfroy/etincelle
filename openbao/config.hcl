# OpenBao server configuration. Baked into the image at /etc/openbao/config.hcl
# and bind-mounted into the container at /openbao/config/config.hcl.

ui = true

disable_mlock = true

# Public address (TLS terminated by Caddy)
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

# Audit log
audit "file" "file" {
  description = "Request audit log"
  options {
    file_path = "/openbao/logs/audit.log"
  }
}

# Static-key auto-unseal; key provisioned by scripts/provision-secrets.sh
# (64 hex chars, no trailing newline). Rotate via previous_key/previous_key_id.
seal "static" {
  current_key_id = "etincelle-2026-09"
  current_key    = "file:///openbao/secrets/seal.key"
}
