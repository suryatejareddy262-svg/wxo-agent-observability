#!/usr/bin/env bash
# =============================================================================
# forwarder/setup_db.sh — One-Click Setup for WXO Agent Mapping Database
# =============================================================================
#
# Supports:
#   1. AWS DynamoDB (Default for AWS)
#   2. PostgreSQL
#
# Usage:
#   # For DynamoDB:
#   ./src/forwarder/setup_db.sh --dynamodb
#
#   # For PostgreSQL:
#   ./src/forwarder/setup_db.sh --postgres "postgresql://user:password@localhost:5432/wxo"
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source config if available
if [ -f "${REPO_ROOT}/config.env" ]; then
    source "${REPO_ROOT}/config.env"
elif [ -f "${REPO_ROOT}/config/config.env" ]; then
    source "${REPO_ROOT}/config/config.env"
else
    if [ -f "${REPO_ROOT}/config/shared.env" ]; then
        source "${REPO_ROOT}/config/shared.env"
    fi
    if [ -f "${REPO_ROOT}/config/forwarder.env" ]; then
        source "${REPO_ROOT}/config/forwarder.env"
    fi
fi

MODE="${1:---dynamodb}"

if [ "${MODE}" = "--dynamodb" ]; then
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║       WXO Agent Mapping — AWS DynamoDB Setup                     ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    TABLE_NAME="${DYNAMODB_TABLE_NAME:-wxo_agent_mappings}"
    AWS_REGION="${AWS_REGION:-us-east-1}"

    echo "  Creating table '${TABLE_NAME}' in region '${AWS_REGION}'..."
    python3 "${SCRIPT_DIR}/manage_db.py" --dynamodb create-table --table-name "${TABLE_NAME}" --region "${AWS_REGION}"

    echo "  Seeding initial agent mappings from config..."
    python3 "${SCRIPT_DIR}/manage_db.py" --dynamodb seed --table-name "${TABLE_NAME}" --region "${AWS_REGION}"

    echo ""
    echo "  Current mappings in DynamoDB:"
    python3 "${SCRIPT_DIR}/manage_db.py" --dynamodb list --table-name "${TABLE_NAME}" --region "${AWS_REGION}"

    echo ""
    echo "  ✓ DynamoDB setup complete. Set DYNAMODB_TABLE_NAME=${TABLE_NAME} on your forwarder."

elif [ "${MODE}" = "--postgres" ]; then
    DB_URL="${2:-${POSTGRES_URL:-${DATABASE_URL:-}}}"
    if [ -z "${DB_URL}" ]; then
        echo "  ✗ PostgreSQL connection string not provided."
        echo "    Usage: $0 --postgres 'postgresql://user:pass@host:5432/db'"
        exit 1
    fi

    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║       WXO Agent Mapping — PostgreSQL Setup                       ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"

    echo "  Creating table 'wxo_agent_mappings'..."
    python3 "${SCRIPT_DIR}/manage_db.py" --postgres --db-url "${DB_URL}" create-table

    echo "  Seeding initial agent mappings from config..."
    python3 "${SCRIPT_DIR}/manage_db.py" --postgres --db-url "${DB_URL}" seed

    echo ""
    echo "  Current mappings in PostgreSQL:"
    python3 "${SCRIPT_DIR}/manage_db.py" --postgres --db-url "${DB_URL}" list

    echo ""
    echo "  ✓ PostgreSQL setup complete. Set POSTGRES_URL='${DB_URL}' on your forwarder."
else
    echo "Unknown option: ${MODE}"
    echo "Usage: $0 [--dynamodb | --postgres <url>]"
    exit 1
fi
