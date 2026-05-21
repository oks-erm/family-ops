#!/usr/bin/env bash
# Retries Oracle A1 instance creation until capacity is available.
#
# Prerequisites:  oci CLI installed and configured (see instructions below)
#
# Usage:
#   export COMPARTMENT_OCID=ocid1.tenancy.oc1..xxxxxxxx
#   bash scripts/oci-retry-create.sh

set -euo pipefail

COMPARTMENT_OCID="${COMPARTMENT_OCID:?ERROR: please run: export COMPARTMENT_OCID=ocid1.tenancy...}"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/family_copilot_deploy.pub}"
DISPLAY_NAME="family-copilot"
SHAPE="VM.Standard.A1.Flex"
OCPUS=4
MEMORY_GB=24
RETRY_INTERVAL=60  # seconds between attempts

# ── Discover availability domain ──────────────────────────────────────────────
echo "==> Discovering availability domain..."
AD=$(oci iam availability-domain list \
  --compartment-id "$COMPARTMENT_OCID" \
  --query 'data[0].name' \
  --raw-output)
echo "    $AD"

# ── Discover latest Ubuntu 24.04 Arm64 image ─────────────────────────────────
echo "==> Finding latest Ubuntu 24.04 Arm64 image..."
IMAGE_OCID=$(oci compute image list \
  --compartment-id "$COMPARTMENT_OCID" \
  --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" \
  --sort-by TIMECREATED \
  --sort-order DESC \
  --all \
  --query 'data[?contains("display-name", `aarch64`)] | [0].id' \
  --raw-output 2>/dev/null || true)

if [ -z "$IMAGE_OCID" ] || [ "$IMAGE_OCID" = "null" ]; then
  IMAGE_OCID=$(oci compute image list \
    --compartment-id "$COMPARTMENT_OCID" \
    --operating-system "Canonical Ubuntu" \
    --operating-system-version "24.04" \
    --sort-by TIMECREATED \
    --sort-order DESC \
    --limit 1 \
    --query 'data[0].id' \
    --raw-output)
fi
echo "    $IMAGE_OCID"

# ── Discover subnet ───────────────────────────────────────────────────────────
echo "==> Finding subnet..."
SUBNET_OCID=$(oci network subnet list \
  --compartment-id "$COMPARTMENT_OCID" \
  --limit 1 \
  --query 'data[0].id' \
  --raw-output)

if [ -z "$SUBNET_OCID" ] || [ "$SUBNET_OCID" = "null" ]; then
  echo ""
  echo "ERROR: No subnet found. The VCN from the wizard may not have been created."
  echo "Please create a VCN first:"
  echo "  Oracle Console → Networking → Virtual Cloud Networks → Create VCN"
  echo "  Use 'Create VCN with Internet Connectivity' option."
  exit 1
fi
echo "    $SUBNET_OCID"

# ── Retry loop ────────────────────────────────────────────────────────────────
echo ""
echo "==> All good. Starting retry loop (every ${RETRY_INTERVAL}s). Press Ctrl+C to stop."
echo ""

ATTEMPT=0
while true; do
  ATTEMPT=$((ATTEMPT + 1))

  echo -n "[$(date '+%H:%M:%S')] Attempt #$ATTEMPT ... "

  if INSTANCE_OCID=$(oci compute instance launch \
    --availability-domain "$AD" \
    --compartment-id "$COMPARTMENT_OCID" \
    --shape "$SHAPE" \
    --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}" \
    --image-id "$IMAGE_OCID" \
    --subnet-id "$SUBNET_OCID" \
    --assign-public-ip true \
    --ssh-authorized-keys-file "$SSH_KEY_FILE" \
    --display-name "$DISPLAY_NAME" \
    --query 'data.id' \
    --raw-output 2>/dev/null); then

    echo "SUCCESS!"
    echo ""
    echo "Instance OCID: $INSTANCE_OCID"
    echo ""
    echo "==> Waiting for instance to reach RUNNING state (up to 5 min)..."
    oci compute instance get \
      --instance-id "$INSTANCE_OCID" \
      --wait-for-state RUNNING \
      --max-wait-seconds 300 \
      --wait-interval-seconds 15 2>/dev/null || true

    echo ""
    echo "==> Fetching public IP..."
    sleep 10
    PUBLIC_IP=$(oci compute instance list-vnics \
      --instance-id "$INSTANCE_OCID" \
      --query 'data[0]."public-ip"' \
      --raw-output 2>/dev/null || echo "(check Oracle console)")

    echo ""
    echo "================================================"
    echo " Instance is RUNNING!"
    echo " Public IP : $PUBLIC_IP"
    echo " SSH       : ssh -i $SSH_KEY_FILE ubuntu@$PUBLIC_IP"
    echo "================================================"
    exit 0

  else
    echo "out of capacity. Retrying in ${RETRY_INTERVAL}s..."
    sleep "$RETRY_INTERVAL"
  fi
done
