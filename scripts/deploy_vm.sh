#!/usr/bin/env bash
# Deploy the LLM Gateway to a VM — bootstrap, container, HTTPS, systemd.
#
# One command turns a fresh Ubuntu 22.04/24.04 VM (Debian works too — the
# bootstrap is apt-based) into a running, HTTPS-capable gateway:
#
#   1. BOOTSTRAP  install Docker CE + the compose plugin from the official
#                 apt repo, enable the service (idempotent — every step
#                 is skipped when already present)
#   2. SYNC       rsync the repo to the app dir (never --delete; state
#                 lives in Docker volumes, not the source tree)
#   3. IMAGE      build on the VM (default) or pull a prebuilt --image
#   4. TLS        merge deploy/docker-compose.tls.yml: Caddy on :80/:443
#                 (auto HTTP→HTTPS redirect), the gateway pinned to
#                 127.0.0.1:8000 — Caddy becomes the only public port
#   5. SERVICE    render + install llm-gateway.service (compose up --wait
#                 on boot, down — inside the drain window — on stop)
#   6. VERIFY     poll /readyz through the proxy before reporting success
#
# Examples:
#   scripts/deploy_vm.sh --host root@203.0.113.5                      # HTTP
#   scripts/deploy_vm.sh --host root@203.0.113.5 --tls self-signed    # dev HTTPS
#   scripts/deploy_vm.sh --host root@203.0.113.5 --tls letsencrypt \
#       --domain api.example.com --email ops@example.com             # prod HTTPS
#   scripts/deploy_vm.sh --tls self-signed                            # this machine
#
# TLS modes (deploy/Caddyfile is rendered from deploy/Caddyfile.template):
#   none          the gateway is published on :8000 — terminate TLS at
#                 your own load balancer, or keep it plain
#   self-signed   Caddy issues a dev certificate (`tls internal`) for
#                 --domain (default: the --host address, else localhost)
#                 — clients must trust it or skip verification
#   letsencrypt   Caddy issues + renews a public certificate via ACME;
#                 --domain (public DNS pointing at the VM) and --email
#                 (the ACME account contact) are both required
#
# With no --host the script deploys the machine it runs on (it is its own
# cloud-init: git clone && scripts/deploy_vm.sh ...). SSH as root (or run
# as root locally) — apt, /etc/systemd/system and systemctl need it. The
# SSH host key is accepted on first connect (accept-new) and only
# non-interactive auth is attempted.
#
# Re-running is safe: every step is idempotent; redeploys re-sync, rebuild
# (or re-pull), and restart the service. GitHub Actions drives this same
# script over SSH from .github/workflows/deploy.yml (dormant until a
# remote exists — see its header).
#
# Flags:
#   --host USER@HOST   SSH target (no --host = deploy locally)
#   --ssh-key PATH     SSH identity file
#   --app-dir DIR      app dir on the VM (default /opt/llm-gateway)
#   --image REF        run prebuilt image REF instead of building
#   --tls MODE         none | self-signed | letsencrypt (default none)
#   --domain NAME      TLS site address (default: --host address, else
#                      localhost)
#   --email ADDR       ACME account email (letsencrypt: required)
#   --dry-run          print the plan + rendered files, change nothing
#   -h | --help        this help
set -euo pipefail

die() { echo "deploy_vm: $*" >&2; exit 1; }
log() { echo "[deploy] $*"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  # The header above is the help text — one source of truth.
  sed -n '2,/^set -euo/p' "$0" | sed -e 's/^#\{1,\} \{0,1\}//' | sed '$d'
}

# ---------------------------------------------------------------------- args
HOST="" SSH_KEY="" APP_DIR="/opt/llm-gateway" IMAGE="" TLS="none"
DOMAIN="" EMAIL="" DRY_RUN=false

need_value() { [[ $# -ge 2 ]] || die "$1 requires a value (see --help)"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)    need_value "$@"; HOST="$2"; shift 2 ;;
    --ssh-key) need_value "$@"; SSH_KEY="$2"; shift 2 ;;
    --app-dir) need_value "$@"; APP_DIR="$2"; shift 2 ;;
    --image)   need_value "$@"; IMAGE="$2"; shift 2 ;;
    --tls)     need_value "$@"; TLS="$2"; shift 2 ;;
    --domain)  need_value "$@"; DOMAIN="$2"; shift 2 ;;
    --email)   need_value "$@"; EMAIL="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown flag: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- validation
case "$TLS" in
  none|self-signed|letsencrypt) ;;
  *) die "unknown --tls mode '$TLS' — valid: none | self-signed | letsencrypt" ;;
esac

# Empty values (workflow env plumbing passes "" for unset variables) mean
# "use the default" for --image/--domain/--email; --host empty means local.
[[ "$HOST" =~ ^(([A-Za-z0-9._-]+@)?[A-Za-z0-9._:-]+)?$ ]] \
  || die "--host must be USER@HOST or HOST (got: '$HOST')"
[[ "$APP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
  || die "--app-dir must be an absolute path without spaces or quotes (got: '$APP_DIR')"
[[ "$IMAGE" =~ ^[A-Za-z0-9._:/@-]*$ ]] \
  || die "--image must be an image reference (got: '$IMAGE')"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]*$ ]] \
  || die "--domain must be a hostname or IP (got: '$DOMAIN')"
[[ "$EMAIL" =~ ^([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)?$ ]] \
  || die "--email must be an email address (got: '$EMAIL')"
if [[ -n "$SSH_KEY" ]]; then
  [[ -f "$SSH_KEY" ]] || die "--ssh-key not found: $SSH_KEY"
fi

if [[ "$TLS" == letsencrypt ]]; then
  [[ -n "$DOMAIN" ]] || die "--tls letsencrypt requires --domain (a public DNS name pointing at the VM)"
  [[ -n "$EMAIL" ]] || die "--tls letsencrypt requires --email (the ACME account contact)"
fi
if [[ -z "$DOMAIN" ]]; then
  if [[ -n "$HOST" ]]; then DOMAIN="${HOST##*@}"; else DOMAIN="localhost"; fi
fi

for f in Dockerfile config.yaml deploy/docker-compose.yml \
         deploy/docker-compose.tls.yml deploy/Caddyfile.template \
         deploy/gateway.service.template; do
  [[ -f "$REPO_ROOT/$f" ]] || die "missing repo artifact: $f"
done
if [[ -n "$HOST" ]]; then
  command -v rsync >/dev/null 2>&1 \
    || die "rsync is required locally for --host deploys (apt/brew install rsync)"
fi

# ------------------------------------------------------------------ derived
COMPOSE_FILES="-f deploy/docker-compose.yml"
if [[ "$TLS" != none ]]; then
  COMPOSE_FILES="$COMPOSE_FILES -f deploy/docker-compose.tls.yml"
fi
GATEWAY_HTTP_PORT="8000"
if [[ "$TLS" != none ]]; then
  GATEWAY_HTTP_PORT="127.0.0.1:8000"   # Caddy is the only public entry point
fi

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
if [[ -n "$SSH_KEY" ]]; then SSH_OPTS+=(-i "$SSH_KEY"); fi

# ------------------------------------------------------------------ helpers
# Run one command on the VM (SSH when --host was given, locally otherwise).
# Arguments are validated to be free of quotes/whitespace above, so the
# command strings interpolate safely into the remote shell.
run() {
  if $DRY_RUN; then echo "[dry-run] $*"; return 0; fi
  if [[ -n "$HOST" ]]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "$*"
  else
    bash -c "$*"
  fi
}

check_root() {
  if $DRY_RUN; then return 0; fi
  if [[ "$(run 'id -u')" != "0" ]]; then
    die "must run as root on the VM (ssh root@host, or sudo scripts/deploy_vm.sh locally) — apt, /etc/systemd/system and systemctl need it"
  fi
}

bootstrap() {
  if $DRY_RUN; then
    echo "[dry-run] VM bootstrap (each step already skipped when present): apt-get update; install ca-certificates curl rsync; Docker CE + compose plugin from download.docker.com; systemctl enable --now docker; mkdir -p $APP_DIR"
    return 0
  fi
  if run "command -v docker >/dev/null 2>&1"; then
    log "bootstrap: docker already installed — skipping the Docker apt bootstrap"
  else
    log "bootstrap: installing Docker (official apt repo)"
    local distro
    distro="$(run '. /etc/os-release && echo "$ID"')"
    case "$distro" in
      ubuntu|debian) ;;
      *) log "bootstrap: WARNING - untested distro '$distro' (targets Ubuntu 22.04/24.04; Debian works)" ;;
    esac
    run "apt-get update -qq"
    run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl rsync"
    run "install -m 0755 -d /etc/apt/keyrings"
    run "curl -fsSL https://download.docker.com/linux/$distro/gpg -o /etc/apt/keyrings/docker.asc"
    run "chmod a+r /etc/apt/keyrings/docker.asc"
    run "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$distro \$(. /etc/os-release && echo \$VERSION_CODENAME) stable\" > /etc/apt/sources.list.d/docker.list"
    run "apt-get update -qq"
    run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
    run "systemctl enable --now docker"
  fi
  # rsync needs its remote end; curl is the health-check client.
  run "command -v rsync >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync; }"
  run "command -v curl  >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl; }"
  run "mkdir -p '$APP_DIR'"
}

RSYNC_EXCLUDES=(--exclude .git/ --exclude .venv/ --exclude .claude/
                --exclude .pytest_cache/ --exclude __pycache__/
                --exclude deploy/certs/ --exclude .env)

sync_repo() {
  if [[ -z "$HOST" && "$APP_DIR" == "$REPO_ROOT" ]]; then
    log "sync: deploying the checkout in place — nothing to copy"
    return 0
  fi
  local -a cmd=(rsync -az "${RSYNC_EXCLUDES[@]}")
  if [[ -n "$HOST" ]]; then
    cmd+=(-e "ssh ${SSH_OPTS[*]}" "$REPO_ROOT/" "$HOST:$APP_DIR/")
  else
    cmd+=("$REPO_ROOT/" "$APP_DIR/")
  fi
  if $DRY_RUN; then echo "[dry-run] ${cmd[*]}"; return 0; fi
  "${cmd[@]}"
}

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

render_files() {
  local tls_line="# certificates are issued and renewed automatically (ACME / Let's Encrypt)"
  if [[ "$TLS" == self-signed ]]; then
    tls_line="tls internal"
  fi
  local email="${EMAIL:-admin@$DOMAIN}"

  {
    echo "# Rendered by scripts/deploy_vm.sh — re-run it to change anything here."
    echo "# Read by llm-gateway.service (EnvironmentFile) and by every manual"
    echo "# docker compose run. GATEWAY_HTTP_PORT is the gateway's published"
    echo "# port: in TLS mode it is pinned to the VM loopback so Caddy is the"
    echo "# only public entry point; GATEWAY_IMAGE runs a prebuilt image."
    echo "GATEWAY_HTTP_PORT=$GATEWAY_HTTP_PORT"
    if [[ -n "$IMAGE" ]]; then
      echo "GATEWAY_IMAGE=$IMAGE"
    fi
  } > "$RENDER_DIR/vm.env"

  if [[ "$TLS" != none ]]; then
    sed -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@EMAIL@|$email|g" \
        -e "s|@TLS@|$tls_line|g" \
        "$REPO_ROOT/deploy/Caddyfile.template" > "$RENDER_DIR/Caddyfile"
  fi

  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@COMPOSE_FILES@|$COMPOSE_FILES|g" \
      "$REPO_ROOT/deploy/gateway.service.template" \
      > "$RENDER_DIR/llm-gateway.service"
}

print_file() {
  echo "----- deploy/$1 -----"
  cat "$RENDER_DIR/$1"
  echo "----- end deploy/$1 -----"
}

push_rendered() {
  local -a cmd=(rsync -az)
  if [[ -n "$HOST" ]]; then
    cmd+=(-e "ssh ${SSH_OPTS[*]}" "$RENDER_DIR/" "$HOST:$APP_DIR/deploy/")
  else
    cmd+=("$RENDER_DIR/" "$APP_DIR/deploy/")
  fi
  "${cmd[@]}"
}

deliver_image() {
  local envstr="GATEWAY_HTTP_PORT='$GATEWAY_HTTP_PORT'"
  if [[ -n "$IMAGE" ]]; then
    envstr="$envstr GATEWAY_IMAGE='$IMAGE'"
    log "image: pulling prebuilt $IMAGE"
    run "cd '$APP_DIR' && $envstr docker compose $COMPOSE_FILES pull gateway"
  else
    log "image: building on the VM"
    run "cd '$APP_DIR' && $envstr docker compose $COMPOSE_FILES build --pull"
  fi
}

install_unit() {
  log "service: installing llm-gateway.service"
  run "install -m 0644 '$APP_DIR/deploy/llm-gateway.service' /etc/systemd/system/llm-gateway.service"
  run "systemctl daemon-reload"
  run "systemctl enable llm-gateway.service"
  log "service: (re)starting the stack via systemd (up -d --wait)"
  run "systemctl restart llm-gateway.service"
}

wait_ready() {
  local -a curl_args=(-fsS --max-time 5)
  local url="http://127.0.0.1:8000/readyz"
  if [[ "$TLS" != none ]]; then
    url="https://$DOMAIN/readyz"
    if [[ "$TLS" == self-signed ]]; then
      curl_args+=(-k)
    fi
    if [[ "$DOMAIN" != localhost ]]; then
      # Verify through the proxy on this VM, whatever DNS says from here.
      curl_args+=(--connect-to "$DOMAIN:443:127.0.0.1:443")
    fi
  fi
  if $DRY_RUN; then
    echo "[dry-run] curl ${curl_args[*]} '$url' — retried every 2s for up to 120s"
    return 0
  fi
  local i
  for i in $(seq 1 60); do
    if run "curl ${curl_args[*]} '$url' >/dev/null 2>&1"; then
      log "verify: gateway ready — $url"
      return 0
    fi
    sleep 2
  done
  die "gateway did not report ready within 120s — inspect with: systemctl status llm-gateway && journalctl -u llm-gateway -n 100"
}

summary() {
  local public="${HOST##*@}"
  if [[ -z "$public" ]]; then public="localhost"; fi
  local url="http://$public:8000"
  if [[ "$TLS" != none ]]; then
    url="https://$DOMAIN"
  fi
  log "=== deploy complete ==="
  echo "  gateway:      $url/v1 (docs: $url/docs, admin UI: $url/admin/ui)"
  echo "  service:      systemctl status llm-gateway (logs: journalctl -u llm-gateway -f)"
  echo "  containers:   cd '$APP_DIR' && docker compose $COMPOSE_FILES ps"
  if [[ "$TLS" == self-signed ]]; then
    echo "  certificate:  self-signed — clients must trust it or skip verification (curl -k, verify=False)"
  fi
  if [[ "$TLS" == letsencrypt ]]; then
    echo "  certificate:  Let's Encrypt, issued and renewed automatically by Caddy (ACME account: $EMAIL)"
  fi
}

# ---------------------------------------------------------------------- main
main() {
  local tls_label="none — plain HTTP published on :8000"
  if [[ "$TLS" != none ]]; then
    tls_label="$TLS — https://$DOMAIN via Caddy on :443"
  fi
  log "=== deploy plan ==="
  log "host:      ${HOST:-<local — deploying this machine>}"
  log "app dir:   $APP_DIR"
  log "image:     ${IMAGE:-build on the VM}"
  log "tls:       $tls_label"
  log "compose:   docker compose $COMPOSE_FILES"
  if $DRY_RUN; then
    log "=== DRY-RUN: no changes will be made (re-run without --dry-run to apply) ==="
  fi

  check_root
  bootstrap
  sync_repo
  render_files

  if $DRY_RUN; then
    deliver_image
    install_unit
    wait_ready
    print_file vm.env
    if [[ "$TLS" != none ]]; then
      print_file Caddyfile
    fi
    print_file llm-gateway.service
    echo "=== dry run complete ==="
    exit 0
  fi

  push_rendered
  deliver_image
  install_unit
  wait_ready
  summary
}

main "$@"
