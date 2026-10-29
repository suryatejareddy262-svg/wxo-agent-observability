#!/usr/bin/env bash
# ============================================================================
# ssm_params.sh
#
# Stores WXO credentials in AWS SSM Parameter Store (SecureString).
# Run this once per tenant when onboarding a new WXO tenant to the account.
# The Lambda reads these at runtime — no hardcoded secrets anywhere.
#
# SSM layout
# ----------
#   /wxo-agent/tenants/<tenant_id>/api_key    ← per-tenant API key
#   /wxo-agent/tenants/<tenant_id>/token_url  ← per-tenant IAM endpoint
#
# Fallback (single-tenant legacy):
#   /wxo-agent/API_KEY    — used when no tenant-specific entry exists
#   /wxo-agent/TOKEN_URL
#
# Why per-tenant keys?
#   Each WXO tenant has its own API key.  Storing a single account-wide
#   API_KEY would prevent different agents from authenticating as their
#   respective tenants.  Keying by tenant_id in SSM gives the Lambda full
#   isolation: it looks up /wxo-agent/tenants/<WXO_TENANT_ID>/api_key at
#   runtime using the WXO_TENANT_ID env var stamped on the agent/harness.
#
# WXO_TENANT_ID and WXO_OTEL_EXPORT_URL are NOT stored in SSM — they are
# injected as env vars directly onto each agent/harness/runtime by the
# wiring scripts (--environment-variables), so each agent can route to a
# different tenant without SSM contention.
#
# Usage:
#   chmod +x ssm_params.sh
#   ./ssm_params.sh
#
#   To register a second tenant, set WXO_TENANT_ID / API_KEY / TOKEN_URL
#   in config/aws.env to the new tenant's values and re-run.
#
# Requires:
#   aws CLI configured with sufficient IAM permissions:
#     ssm:PutParameter
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Load shared creds (TOKEN_URL) and per-agent tenant vars from config
if [ -z "${WXO_DEPLOYMENT_MODE:-}" ]; then
    if [ -f "${SCRIPT_DIR}/../config.env" ]; then
        eval "$(python3 "${SCRIPT_DIR}/lib/parse_env_section.py" "${SCRIPT_DIR}/../config.env" "aws" "sh")"
    elif [ -f "${SCRIPT_DIR}/../config/config.env" ]; then
        eval "$(python3 "${SCRIPT_DIR}/lib/parse_env_section.py" "${SCRIPT_DIR}/../config/config.env" "aws" "sh")"
    elif [ -f "${SCRIPT_DIR}/../config/aws.env" ]; then
        set -a
        [ -f "${SCRIPT_DIR}/../config/shared.env" ] && source "${SCRIPT_DIR}/../config/shared.env"
        source "${SCRIPT_DIR}/../config/aws.env"
        set +a
    fi
fi

# ── CPD mode: store the CPD login instead of tenant API keys ──────────────────
if [ "${WXO_DEPLOYMENT_MODE:-cloud}" = "cpd" ]; then
    echo "Storing WXO CPD credentials in AWS SSM Parameter Store..."
    echo "Region       : ${AWS_REGION}"
    echo "CPD host     : ${CPD_HOST_URL}"
    echo "CPD instance : ${CPD_INSTANCE_ID}"
    echo ""
    _put_cpd() {
        aws ssm put-parameter --region "${AWS_REGION}" --name "$1" --value "$2" \
            --type "$3" --overwrite --query "Version" --output text > /dev/null
        echo "  ✓ $1"
    }
    _put_cpd "/wxo-agent/cpd/host_url"    "${CPD_HOST_URL}"    "String"
    _put_cpd "/wxo-agent/cpd/username"    "${CPD_USERNAME}"    "SecureString"
    _put_cpd "/wxo-agent/cpd/password"    "${CPD_PASSWORD}"    "SecureString"
    _put_cpd "/wxo-agent/cpd/instance_id" "${CPD_INSTANCE_ID}" "String"
    _put_cpd "/wxo-agent/cpd/namespace"   "${CPD_NAMESPACE}"   "String"
    echo ""
    echo "CPD credentials stored under /wxo-agent/cpd/."
    exit 0
fi

echo "Storing WXO tenant credentials in AWS SSM Parameter Store..."
echo "Region    : ${AWS_REGION}"
echo "Tenant ID : ${WXO_TENANT_ID}"
echo ""

# Helper — puts a SecureString parameter (overwrites if exists)
put_param() {
    local name="$1"
    local value="$2"
    local description="$3"

    aws ssm put-parameter \
        --region "${AWS_REGION}" \
        --name "${name}" \
        --value "${value}" \
        --type "SecureString" \
        --description "${description}" \
        --overwrite \
        --query "Version" \
        --output text

    echo "  ✓ ${name}"
}

# ============================================================================
# Per-tenant credentials — keyed by WXO_TENANT_ID so the Lambda can look
# up the correct API key for each tenant at runtime.
# ============================================================================

put_param \
    "/wxo-agent/tenants/${WXO_TENANT_ID}/api_key" \
    "${API_KEY}" \
    "WXO API key for tenant ${WXO_TENANT_ID}"

put_param \
    "/wxo-agent/tenants/${WXO_TENANT_ID}/token_url" \
    "${TOKEN_URL}" \
    "IAM token endpoint for tenant ${WXO_TENANT_ID}"

# ============================================================================
# Also write the legacy flat keys so existing Lambda deployments continue
# to work during a rolling upgrade.  The Lambda prefers the per-tenant
# path and falls back to these only when no tenant-specific entry exists.
# ============================================================================

put_param "/wxo-agent/API_KEY"   "${API_KEY}"   "WXO API key (legacy flat — prefer per-tenant path)"
put_param "/wxo-agent/TOKEN_URL" "${TOKEN_URL}" "IAM endpoint (legacy flat — prefer per-tenant path)"

echo ""
echo "Tenant credentials stored."
echo ""
echo "  Per-tenant path : /wxo-agent/tenants/${WXO_TENANT_ID}/"
echo "    api_key        = (SecureString)"
echo "    token_url      = (SecureString)"
echo ""
echo "  Legacy flat keys also updated for backward compatibility."
echo ""
echo "  To add a second tenant: update WXO_TENANT_ID and API_KEY in"
echo "  config/aws.env to the new tenant's values, then re-run this script."
echo ""
echo "Verify with:"
echo "  aws ssm get-parameters-by-path --path /wxo-agent/ --with-decryption --recursive \\"
echo "    --region ${AWS_REGION} --query 'Parameters[*].{Name:Name,Version:Version}' --output table"
