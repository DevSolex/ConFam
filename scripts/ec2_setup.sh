#!/usr/bin/env bash
# =============================================================================
# scripts/ec2_setup.sh
#
# Run once on a fresh Ubuntu 24.04 EC2 instance to install Docker,
# clone the repo, and prepare the environment.
#
# Usage (as ubuntu user after SSH):
#   curl -fsSL https://raw.githubusercontent.com/DevSolex/ConFam/main/scripts/ec2_setup.sh | bash
# OR copy the file and run:
#   bash ec2_setup.sh
# =============================================================================

set -euo pipefail

echo "=== [1/5] System update ==="
sudo apt-get update -q
sudo apt-get upgrade -yq

echo "=== [2/5] Install Docker ==="
sudo apt-get install -yq ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -q
sudo apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
# Allow ubuntu user to run docker without sudo
sudo usermod -aG docker ubuntu
echo "Docker installed: $(docker --version)"

echo "=== [3/5] Clone repository ==="
cd /home/ubuntu
git clone https://github.com/DevSolex/ConFam.git
cd ConFam
echo "Repository cloned."

echo "=== [4/5] Create .env ==="
# Copy the example — you MUST fill in real values before starting the stack
cp .env.example .env
chmod 600 .env
echo ""
echo "IMPORTANT: Edit /home/ubuntu/ConFam/.env with real values before running the stack."
echo "  - Set PUBLIC_HOSTNAME to your sslip.io hostname (see DEPLOYMENT.md)"
echo "  - Set all PAYSTACK_*, WHATSAPP_*, and database passwords"
echo ""

echo "=== [5/5] Open firewall ports (ufw) ==="
# Allow SSH, HTTP (for Let's Encrypt), HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. newgrp docker  (or log out and back in for docker group to take effect)"
echo "  2. nano /home/ubuntu/ConFam/.env  (fill in real values)"
echo "  3. docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
echo "  4. See DEPLOYMENT.md for the full checklist."
