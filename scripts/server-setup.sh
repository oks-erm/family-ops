#!/usr/bin/env bash
# Hetzner Ubuntu 24.04 LTS — initial server setup
#
# Usage (run as root on the new server):
#   bash server-setup.sh "<your-ssh-public-key>"
#
# What this script does:
#   1. Updates & upgrades system packages
#   2. Installs Docker CE + Compose plugin
#   3. Creates a non-root "deploy" user with Docker access
#   4. Installs your SSH public key for the deploy user
#   5. Configures UFW: allow 22/80/443, deny everything else
#   6. Hardens SSH: disables password auth and root login
#   7. Creates /opt/family-copilot owned by the deploy user

set -euo pipefail

SSH_PUBLIC_KEY="${1:?Usage: $0 \"<ssh-public-key>\"}"
DEPLOY_USER="deploy"
APP_DIR="/opt/family-copilot"

# ── 1. System update ──────────────────────────────────────────────────────────
echo "==> Updating system packages..."
apt-get update -q
apt-get upgrade -y -q

# ── 2. Install Docker CE ──────────────────────────────────────────────────────
echo "==> Installing Docker..."
apt-get install -y -q ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -q
apt-get install -y -q \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

# ── 3. Create deploy user ─────────────────────────────────────────────────────
echo "==> Creating deploy user..."
if ! id "$DEPLOY_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

# ── 4. Install SSH key for deploy user ────────────────────────────────────────
echo "==> Configuring SSH key..."
DEPLOY_HOME="/home/$DEPLOY_USER"
mkdir -p "$DEPLOY_HOME/.ssh"
echo "$SSH_PUBLIC_KEY" > "$DEPLOY_HOME/.ssh/authorized_keys"
chmod 700 "$DEPLOY_HOME/.ssh"
chmod 600 "$DEPLOY_HOME/.ssh/authorized_keys"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_HOME/.ssh"

# ── 5. Firewall ───────────────────────────────────────────────────────────────
echo "==> Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment 'SSH'
ufw allow 80/tcp   comment 'HTTP (Traefik → HTTPS redirect)'
ufw allow 443/tcp  comment 'HTTPS'
ufw --force enable

# ── 6. Harden SSH ─────────────────────────────────────────────────────────────
echo "==> Hardening SSH configuration..."
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh

# ── 7. App directory ──────────────────────────────────────────────────────────
echo "==> Creating app directory $APP_DIR..."
mkdir -p "$APP_DIR"
chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

echo ""
echo "========================================"
echo " Server setup complete!"
echo "========================================"
echo " Deploy user : $DEPLOY_USER"
echo " App dir     : $APP_DIR"
echo " Firewall    : 22 (SSH), 80 (HTTP), 443 (HTTPS)"
echo ""
echo " Next steps:"
echo "   1. Point your domain DNS A record to this server's IP"
echo "   2. Add GitHub secrets (see .env.production.example)"
echo "   3. Push to main to trigger the first deploy"
echo "========================================"
