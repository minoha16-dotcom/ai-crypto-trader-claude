#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with: sudo bash deploy/oracle-cloud/install.sh" >&2
  exit 1
fi

if [[ ! -f /etc/os-release ]] || ! grep -q 'Ubuntu' /etc/os-release; then
  echo "This installer currently supports Ubuntu Server on OCI Ampere A1." >&2
  exit 1
fi

APP_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="/opt/ai-crypto-trader"
DATA_DIR="/var/lib/ai-crypto-trader"
ENV_FILE="/etc/ai-crypto-trader.env"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git openssl python3 python3-venv python3-pip rsync

if ! id trader >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin trader
fi

install -d -o root -g trader -m 0750 "${APP_DIR}"
rsync -a --delete --exclude='.git' --exclude='.venv' "${APP_SOURCE}/" "${APP_DIR}/"
chown -R root:trader "${APP_DIR}"
chmod -R g+rX,o-rwx "${APP_DIR}"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

install -d -o trader -g trader -m 0750 "${DATA_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  DASHBOARD_PASSWORD="$(openssl rand -hex 32)"
  cat > "${ENV_FILE}" <<EOF
APP_NAME=AI Crypto Trader V2
DATABASE_URL=sqlite:////var/lib/ai-crypto-trader/trader.db
DASHBOARD_AUTH_REQUIRED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
PYTHONUNBUFFERED=1
EOF
  chmod 0600 "${ENV_FILE}"
  chown root:root "${ENV_FILE}"
  echo "Created dashboard credentials in ${ENV_FILE} (password not displayed)."
else
  echo "Keeping existing environment file and dashboard password."
fi

install -m 0644 "${APP_DIR}/deploy/oracle-cloud/ai-crypto-trader.service" \
  /etc/systemd/system/ai-crypto-trader.service
systemctl daemon-reload
systemctl enable --now ai-crypto-trader
systemctl --no-pager --full status ai-crypto-trader || true

DOMAIN="${DOMAIN:-}"
if [[ -z "${DOMAIN}" && -t 0 ]]; then
  read -r -p "DNS hostname for HTTPS (leave blank to configure later): " DOMAIN
fi

if [[ -n "${DOMAIN}" ]]; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
  install -d -m 0755 /etc/apt/keyrings
  rm -f /etc/apt/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /etc/apt/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /etc/apt/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
  cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
  systemctl enable --now caddy
  systemctl reload caddy
  echo "Caddy configured for https://${DOMAIN}"
else
  echo "No domain supplied. App is bound to localhost only; use an SSH tunnel until HTTPS is configured."
fi

echo
echo "Installation complete."
echo "Dashboard username: admin"
echo "Read the generated password securely with: sudo less ${ENV_FILE}"
echo "Health check on the VM: curl http://127.0.0.1:8000/api/health"
echo "Service logs: sudo journalctl -u ai-crypto-trader -f"
