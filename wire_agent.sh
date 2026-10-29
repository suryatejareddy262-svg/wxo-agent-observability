#!/usr/bin/env bash
# =============================================================================
# wire_agent.sh  —  Unified WXO Agent Analytics Tracing Setup
#
# Covers all three cloud platforms from a single entrypoint.
#
# Usage:
#   ./wire_agent.sh --aws         [--harness|--runtime]              [--dry-run]
#   ./wire_agent.sh --azure       [--prompt|--hosted] [--revert]     [--dry-run] [--publish-only]
#   ./wire_agent.sh --vertex      [--reasoning-engine|--selfhosted]  [--dry-run]
#   ./wire_agent.sh --salesforce  [--apex|--session <id>|--poller]   [--dry-run]
#   ./wire_agent.sh --forwarder   [--deploy [--claude]]              [--dry-run]
#
# Agent input (ALL platforms):
#   WXO_AGENT_MAP in config/<platform>.env — the only supported input.
#   One entry = one agent.  Many entries = wire all in one run.
#   Each entry carries the AGENT_REF (or agent key) → WXO identity mapping.
#   See config/aws.env, config/azure.env, config/vertex.env, config/salesforce.env, config/forwarder.env
#   for the per-platform JSON schema.
#
# AWS flows (AGENT_TYPE in config/aws.env):
#   --harness  → Flow A: env vars only, no Docker, no image change
#   --runtime  → Flow B: sitecustomize.py image patch
#
# Azure flows:
#   --prompt   → Flow A: Azure Function App polls Log Analytics every 30s
#   --hosted   → Flow B: sitecustomize.py bundled into agent version (default)
#
# Vertex flows:
#   --selfhosted       → sitecustomize.py injected into container (default)
#   --reasoning-engine → WXO tracing embedded in Reasoning Engine query()
#
# Forwarder flows (FORWARDER_TARGET in config.env: aws | azure | docker):
#   (no flag)           → Run local OTLP receiver daemon (port 4318)
#   --deploy            → Create / update forwarder on target (AWS Lambda / Azure / Docker)
#   --deploy --claude   → Deploy forwarder and configure Claude (Anthropic) telemetry destination
#
# Salesforce flows:
#   --apex        → In-org forwarder (default): deploys WxoSessionPoller Queueable stack +
#                   unified REST endpoint (/services/apexrest/wxo/trace). Polls Data 360
#                   with reduced 1-min timer, enriches traces based on session output,
#                   and pushes to WXO. Zero agent or topic patching required.
#   --session <id>→ On-demand trigger: fetches, enriches, and pushes a single session immediately.
#   --poller      → External Python daemon: polls Data 360 and forwards OTLP traces to WXO.
#
# Pre-requisites:
#   AWS:        AWS CLI authenticated; Docker (--runtime only); ssm_params.sh run once (aws/ssm_params.sh)
#   Azure:      az CLI authenticated; func CLI (--prompt); azure-ai-projects (--hosted)
#   Vertex:     gcloud authenticated; Docker (--selfhosted)
#   Salesforce: Connected App with API access; SF_USERNAME/PASSWORD/CLIENT_ID set
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"
SRC_DIR="${SCRIPT_DIR}/src"

# ── Parse CLI arguments ───────────────────────────────────────────────────────
PLATFORM=""
DRY_RUN=0
SHOW_STATUS=0
SHOW_SECTION=""
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --aws)         PLATFORM="aws" ;;
        --azure)       PLATFORM="azure" ;;
        --vertex)      PLATFORM="vertex" ;;
        --salesforce)  PLATFORM="salesforce" ;;
        --forwarder|--otlp) PLATFORM="forwarder" ;;
        --platform)    shift; PLATFORM="${1:-}" ;;
        --platform=*)  PLATFORM="${1#--platform=}" ;;
        --status|--list) SHOW_STATUS=1 ;;
        --show)        shift; SHOW_SECTION="${1:-forwarder}" ;;
        --show=*)      SHOW_SECTION="${1#--show=}" ;;
        --dry-run) DRY_RUN=1; PASSTHROUGH_ARGS+=("--dry-run") ;;
        --help|-h)
            sed -n '2,/^set -/p' "$0" | grep '^#' | sed 's/^# \{0,2\}//'
            exit 0
            ;;
        *) PASSTHROUGH_ARGS+=("$1") ;;
    esac
    shift
done

# ── Load config ───────────────────────────────────────────────────────────────
# Supports:
#   Option A (Unified): single config.env containing all platforms + toggle
#   Option B (Modular): config/shared.env + config/<platform>.env
_unified_cfg=""
if [ -f "${SCRIPT_DIR}/config.env" ]; then
    _unified_cfg="${SCRIPT_DIR}/config.env"
elif [ -f "${CONFIG_DIR}/config.env" ]; then
    _unified_cfg="${CONFIG_DIR}/config.env"
elif [ -f "${SCRIPT_DIR}/.env" ]; then
    _unified_cfg="${SCRIPT_DIR}/.env"
fi

# Helper commands (work in both Unified config.env and Modular config/*.env modes)
if [ "${SHOW_STATUS}" -eq 1 ]; then
    _cfg_path="${_unified_cfg:-${CONFIG_DIR}}"
    python3 "${SRC_DIR}/lib/parse_env_section.py" "${_cfg_path}" "--status"
    exit 0
fi
if [ -n "${SHOW_SECTION}" ]; then
    _cfg_path="${_unified_cfg:-${CONFIG_DIR}}"
    python3 "${SRC_DIR}/lib/parse_env_section.py" "${_cfg_path}" "--show" "${SHOW_SECTION}"
    exit 0
fi

if [ -n "${_unified_cfg}" ]; then

    # If platform not passed via CLI flag, check if TARGET_PLATFORM is in environment or prompt interactively
    if [ -z "${PLATFORM}" ]; then
        PLATFORM="${TARGET_PLATFORM:-}"
    fi

    if [ -z "${PLATFORM}" ]; then
        if [ -t 0 ]; then
            echo ""
            echo "╔══════════════════════════════════════════════════════════════════╗"
            echo "║        WXO Unified Agent Analytics Tracing Setup                ║"
            echo "╚══════════════════════════════════════════════════════════════════╝"
            echo ""
            echo "  Select a platform section from config.env to run:"
            echo ""
            echo "    1) [forwarder]   OTLP Trace Forwarder (Local / Docker / Lambda / Azure)"
            echo "    2) [aws]         AWS AgentCore / Bedrock Agents"
            echo "    3) [azure]       Azure AI Foundry Agents"
            echo "    4) [vertex]      Google Cloud Vertex AI / Reasoning Engine"
            echo "    5) [salesforce]  Salesforce Agentforce / Data 360"
            echo "    s) Status        View configuration readiness table"
            echo "    q) Quit"
            echo ""
            read -r -p "  Enter choice [1-5]: " _choice
            case "${_choice}" in
                1) PLATFORM="forwarder" ;;
                2) PLATFORM="aws" ;;
                3) PLATFORM="azure" ;;
                4) PLATFORM="vertex" ;;
                5) PLATFORM="salesforce" ;;
                s|S) python3 "${SRC_DIR}/lib/parse_env_section.py" "${_unified_cfg}" "--status"; exit 0 ;;
                *) echo "  Exiting."; exit 0 ;;
            esac
            echo ""
        else
            echo "  ✗ Platform not specified."
            echo "    Specify the platform to run:"
            echo "      ./wire_agent.sh --forwarder"
            echo "      ./wire_agent.sh --aws"
            echo "      ./wire_agent.sh --azure"
            echo "      ./wire_agent.sh --vertex"
            echo "      ./wire_agent.sh --salesforce"
            echo "      ./wire_agent.sh --status"
            echo ""
            echo "  Run $0 --help for usage."
            exit 1
        fi
    fi

    # Layered config, lowest → highest precedence:
    #   config/shared.env  <  config/<platform>.env  <  config.env [<platform>]
    # A higher layer only wins when its value is real (not empty / "<placeholder>"),
    # so credentials may live in forwarder.env or shared.env instead of config.env.
    _cfg_desc="${_unified_cfg} [${PLATFORM}]"
    eval "$(python3 "${SRC_DIR}/lib/parse_env_section.py" "${_unified_cfg}" "${PLATFORM}" "sh")"
    set -a
    if [ -f "${CONFIG_DIR}/shared.env" ]; then
        source "${CONFIG_DIR}/shared.env"
        _cfg_desc="${_cfg_desc}, config/shared.env"
    fi
    if [ -f "${CONFIG_DIR}/${PLATFORM}.env" ]; then
        source "${CONFIG_DIR}/${PLATFORM}.env"
        _cfg_desc="${_cfg_desc}, config/${PLATFORM}.env"
    fi
    set +a
else
    if [ -z "${PLATFORM}" ]; then
        echo "  ✗ Platform not specified. Use --aws, --azure, --vertex, --salesforce, or --forwarder."
        echo "    Or create a unified config.env (cp config.env.example config.env)."
        echo ""
        echo "  Run $0 --help for usage."
        exit 1
    fi

    _platform_cfg="${CONFIG_DIR}/${PLATFORM}.env"

    if [ ! -f "${_platform_cfg}" ]; then
        echo "  ✗ Configuration not found. Looked for:"
        echo "    - config.env (Unified config — see config.env.example)"
        echo "    - config/${PLATFORM}.env (Platform config)"
        echo ""
        echo "    Create config/${PLATFORM}.env and fill in your credentials."
        exit 1
    fi

    set -a
    if [ -f "${CONFIG_DIR}/shared.env" ]; then
        source "${CONFIG_DIR}/shared.env"
    fi
    source "${_platform_cfg}"
    set +a
    _cfg_desc="config/${PLATFORM}.env"
fi

# ── Deployment mode + credential validation ──────────────────────────────────
# WXO_DEPLOYMENT_MODE selects how the bearer token is obtained:
#   cloud (default)  API_KEY + TOKEN_URL (IBM IAM), WXO_TENANT_ID, WXO_OTEL_EXPORT_URL
#   cpd              CPD_HOST_URL + CPD_USERNAME + CPD_PASSWORD (POST /icp4d-api/v1/authorize),
#                    CPD_INSTANCE_ID + CPD_NAMESPACE (builds the trace URL)
source "${SRC_DIR}/lib/common.sh"
wxo_resolve_mode || exit 1

if ! wxo_validate_mode "${_cfg_desc}"; then
    echo ""
    echo "  Edit your configuration and re-run (see config.env.example for ${WXO_DEPLOYMENT_MODE} mode)."
    exit 1
fi

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║        WXO Unified Agent Analytics Tracing Setup                ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
printf "  Platform    : %s\n" "${PLATFORM}"
printf "  Mode        : %s\n" "${WXO_DEPLOYMENT_MODE}"
printf "  Environment : %s\n" "${ENVIRONMENT_NAME}"
if [ "${WXO_DEPLOYMENT_MODE}" = "cpd" ]; then
    printf "  CPD host    : %s\n" "${CPD_HOST_URL}"
    printf "  CPD instance: %s (namespace %s)\n" "${CPD_INSTANCE_ID}" "${CPD_NAMESPACE}"
else
    printf "  WXO tenant  : %s\n" "${WXO_TENANT_ID}"
fi
printf "  WXO endpoint: %s\n" "${WXO_OTEL_EXPORT_URL:0:60}..."
echo ""

# ── Dry-run notice ─────────────────────────────────────────────────────────────
if [ "${DRY_RUN}" -eq 1 ]; then
    echo "  --dry-run mode: no changes will be made."
    echo ""
fi

# ── Dispatch ──────────────────────────────────────────────────────────────────
# All env vars are already exported (set -a above), so each child process
# inherits them without re-sourcing config.env.
case "${PLATFORM}" in
    aws)
        exec "${SRC_DIR}/aws/wire_agent.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        ;;
    azure)
        exec python3 "${SRC_DIR}/azure/wire_agent.py" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        ;;
    vertex)
        exec "${SRC_DIR}/vertex/wire_agent.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        ;;
    salesforce)
        exec python3 "${SRC_DIR}/salesforce/wire_agent.py" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        ;;
    forwarder)
        # --deploy / --sync / --target are handled by deploy.py (create infra + push config).
        _action=""; _target=""; _reg=0; _cm=""; _rest=()
        _args=("${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}")
        _i=0
        while [ "${_i}" -lt "${#_args[@]}" ]; do
            case "${_args[${_i}]}" in
                --deploy) _action="deploy" ;;
                --sync)   _action="sync" ;;
                --claude|--anthropic|--register-anthropic) _reg=1 ;;
                --unregister-anthropic) _action="unregister-anthropic" ;;
                --content-mode) _i=$((_i + 1)); _cm="${_args[${_i}]:-}" ;;
                --content-mode=*) _cm="${_args[${_i}]#--content-mode=}" ;;
                --metadata-only) _cm="metadata_only" ;;
                --full-content)  _cm="content" ;;
                --target) _i=$((_i + 1)); _target="${_args[${_i}]:-}" ;;
                --target=*) _target="${_args[${_i}]#--target=}" ;;
                *) _rest+=("${_args[${_i}]}") ;;
            esac
            _i=$((_i + 1))
        done
        # --register-anthropic alone = register only; combined with --deploy/--sync = do both.
        _reg_flag=()
        if [ "${_reg}" -eq 1 ]; then
            if [ -z "${_action}" ]; then _action="register-anthropic"; else _reg_flag=("--register-anthropic"); fi
        fi
        if [ -n "${_action}" ]; then
            _dx=("${_reg_flag[@]+"${_reg_flag[@]}"}")
            [ "${DRY_RUN}" -eq 1 ] && _dx+=("--dry-run")
            [ -n "${_target}" ] && _dx+=("--target" "${_target}")
            [ -n "${_cm}" ] && _dx+=("--content-mode" "${_cm}")
            exec python3 "${SRC_DIR}/forwarder/deploy.py" "${_action}" "${_dx[@]+"${_dx[@]}"}"
        fi
        if [ -n "${_target}" ]; then
            echo "  ✗ --target only applies with --deploy or --sync."
            exit 1
        fi

        # Local receiver: tell the user where to point their agent's OTel exporter.
        _port="${OTLP_PORT:-4318}"
        echo "  Forwarder OTLP endpoint : http://0.0.0.0:${_port}/v1/traces  (local daemon)"
        echo "  Point your agent's OTel exporter here:"
        echo "    export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=\"http://localhost:${_port}/v1/traces\""

        # Print the agent map so the user can confirm key → UUID bindings
        if [ -n "${WXO_AGENT_MAP:-}" ]; then
            echo ""
            echo "  WXO_AGENT_MAP entries (platform key → wxo_agent_id):"
            echo "${WXO_AGENT_MAP}" | python3 -c "
import sys, json
try:
    m = json.load(sys.stdin)
    for k, v in m.items():
        if isinstance(v, str):
            aid = v
        elif isinstance(v, dict):
            aid = v.get('wxo_agent_id') or v.get('agent_id') or '?'
        else:
            aid = '?'
        print(f'    {k!r:40s} → {aid}')
except Exception as e:
    print(f'    (could not parse WXO_AGENT_MAP: {e})')
"
        fi
        echo ""
        exec python3 "${SRC_DIR}/forwarder/wire_agent.py" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        ;;
esac
