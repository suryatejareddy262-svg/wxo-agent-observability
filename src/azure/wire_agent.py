"""
wire_agent.py  —  Wire an Azure Foundry agent with WXO tracing
===============================================================

Two flows — set with --prompt or --hosted:

  --prompt  (Flow A — kind:prompt agents)
  ──────────────────────────────────────────────────────────────────────
  Prompt agents emit telemetry automatically to Application Insights.
  This flow deploys a single Azure Function App that polls the
  AppGenAIContent table in Log Analytics every 30 s and forwards spans
  to WXO Agent Analytics.

  No changes to the agent itself.  One Function App covers ALL prompt
  agents in the workspace simultaneously.

  Steps:
    1. Create / verify Storage Account
    2. Create / verify Linux Consumption Function App
    3. Assign Managed Identity + grant Monitoring Reader on Log Analytics
    4. Merge WXO_AGENT_MAP into Function App settings
    5. Publish function_app.py code via `func` CLI

  --hosted  (Flow B — kind:hosted agents)  [default]
  ──────────────────────────────────────────────────────────────────────
  Hosted agents run user-supplied Python code.  Foundry manages the
  container internally (no ACR URI to layer on top of).

  This flow creates a NEW agent version from source by:
    1. Fetching the original agent source from GitHub
    2. Injecting sitecustomize.py into the zip  (Python auto-imports it
       at startup — before main.py)
    3. Calling create_version_from_code with WXO env vars injected

  No changes to main.py, requirements.txt, or any agent file.
  sitecustomize.py is the only addition — and only in the new version.

  --revert: create a new clean version without sitecustomize.py / WXO vars.

Usage
-----
    cd wxo-azure-agent-tracing

    # Flow A — kind:prompt (deploy / update the Function App)
    python3 wire_agent.py --prompt
    python3 wire_agent.py --prompt --publish-only   # skip resource creation
    python3 wire_agent.py --prompt --dry-run

    # Flow B — kind:hosted (wire a specific hosted agent)
    python3 wire_agent.py --hosted                             # uses AZURE_AGENT_NAME from config.env
    python3 wire_agent.py --hosted --agent <name>
    python3 wire_agent.py --hosted --agent <name> --revert
    python3 wire_agent.py --hosted --dry-run

    # --hosted is the default when neither flag is given (backward-compat)
    python3 wire_agent.py
    python3 wire_agent.py --agent <name> --revert
"""

import argparse
import io
import json
import os
import subprocess
import sys
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# =============================================================================
# ── Configuration ─────────────────────────────────────────────────────────────
# =============================================================================

SUBSCRIPTION     = os.environ.get("AZURE_SUBSCRIPTION_ID", "<your-azure-subscription-id>")
RESOURCE_GROUP   = os.environ.get("AZURE_RESOURCE_GROUP",  "<your-resource-group>")
ACCOUNT          = os.environ.get("AZURE_ACCOUNT",         "<your-ai-foundry-account>")
PROJECT          = os.environ.get("AZURE_PROJECT",         "<your-ai-foundry-project>")
PROJECT_ENDPOINT = f"https://{ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT}"

PATCH_DIR        = Path(__file__).parent.parent / "shared"   # canonical sitecustomize.py location
API_VERSION      = "v1"

# ── Agent source configuration (kind:hosted / --hosted) ──────────────────────
# Source resolution for step2_build_zip (first match wins):
#
#   1. AGENT_SOURCE_DIR (env)             — any local directory; ALL files zipped
#   2. azure/agents/<foundry-agent-name>/ — per-agent directory in this repo
#   3. azure/agent/                       — legacy single-agent fallback
#   4. AGENT_SOURCE_URL (env)             — remote URL; ALL files fetched
#                                           (GitHub raw URL: all files via Contents API;
#                                            other URL: main.py + requirements.txt only)
#
# AGENT_SOURCE_DIR  — absolute or repo-relative path to a local agent directory.
#                     ALL files in that directory are zipped (no configuration needed).
#                     Use this when your agent lives outside azure/agents/.
#                     Set in config/azure.env:
#                       export AGENT_SOURCE_DIR="/path/to/my-agent"
#
# AGENT_SOURCE_URL  — REQUIRED when no local directory is available (options 1–3 miss).
#                     • GitHub raw URL  → file list fetched via GitHub Contents API;
#                                         ALL files in that directory are downloaded.
#                     • Any other URL   → main.py + requirements.txt fetched directly;
#                                         use AGENT_SOURCE_DIR for agents with extra files.
#                     No default — if unset and no local directory is found, the tool
#                     exits with an error listing all three options to resolve it.
#                     Set in config/azure.env:
#                       export AGENT_SOURCE_URL="https://raw.githubusercontent.com/<org>/<repo>/<branch>/path"
_AGENT_SOURCE_DIR  = os.environ.get("AGENT_SOURCE_DIR", "").strip()
_AGENT_SOURCE_BASE = os.environ.get("AGENT_SOURCE_URL", "").rstrip("/")
# Fallback file list used when the remote URL is NOT a GitHub raw URL and
# directory listing is therefore not possible.
_REMOTE_FALLBACK_FILES = ["main.py", "requirements.txt"]

# WXO credentials — read from env vars (populated from config/azure.env).
#
# WXO_TENANT_ID and WXO_OTEL_EXPORT_URL come from config/azure.env (NOT
# shared.env) so that different Azure agents can be wired to different WXO
# tenants on the same Azure subscription.
# API_KEY and TOKEN_URL are account-wide and come from shared.env.
#
# WXO_DEPLOYMENT_MODE selects how traces are authenticated:
#   cloud (default) — API_KEY + TOKEN_URL + WXO_TENANT_ID + WXO_OTEL_EXPORT_URL
#   cpd             — CPD_HOST_URL + CPD_USERNAME + CPD_PASSWORD + CPD_INSTANCE_ID
#                     + CPD_NAMESPACE (export URL derived; no tenant / API key)
_WXO_MODE            = "cpd" if os.environ.get("WXO_DEPLOYMENT_MODE", "cloud").strip().lower() == "cpd" else "cloud"
_CPD_HOST_URL        = os.environ.get("CPD_HOST_URL", "").rstrip("/")
_CPD_USERNAME        = os.environ.get("CPD_USERNAME", "")
_CPD_PASSWORD        = os.environ.get("CPD_PASSWORD", "")
_CPD_INSTANCE_ID     = os.environ.get("CPD_INSTANCE_ID", "")
_CPD_NAMESPACE       = os.environ.get("CPD_NAMESPACE", "")
_CPD_VERIFY_TLS      = os.environ.get("CPD_VERIFY_TLS", "false")
if _WXO_MODE == "cpd":
    _WXO_TENANT_ID       = ""
    _WXO_API_KEY         = ""
    _WXO_TOKEN_URL       = ""
    _WXO_OTEL_EXPORT_URL = (os.environ.get("CPD_EXPORT_URL") or os.environ.get("WXO_OTEL_EXPORT_URL")
                            or (f"{_CPD_HOST_URL}/{_CPD_NAMESPACE}/instances/{_CPD_INSTANCE_ID}/v1/agentops-v3/traces"
                                if _CPD_HOST_URL and _CPD_NAMESPACE and _CPD_INSTANCE_ID else ""))
else:
    _WXO_TENANT_ID       = os.environ.get("WXO_TENANT_ID",     "<your-wxo-tenant-id>")
    _WXO_API_KEY         = os.environ.get("API_KEY",           "<your-wxo-api-key>")
    _WXO_TOKEN_URL       = os.environ.get("TOKEN_URL",         "https://account-iam.platform.saas.ibm.com/api/2.0/apikeys/token")
    _WXO_OTEL_EXPORT_URL = os.environ.get("WXO_OTEL_EXPORT_URL", "https://api.watson-orchestrate.ibm.com/instances/<your-wxo-instance-id>/v1/orchestrate/inject/traces")
_WXO_ENVIRONMENT     = os.environ.get("ENVIRONMENT_NAME",  "draft")


def _wxo_auth_settings(overrides=None) -> dict:
    """Mode-specific WXO auth settings to inject into the Function App / hosted agent.

    cloud → {WXO_OTEL_EXPORT_URL, WXO_API_KEY, WXO_TOKEN_URL, WXO_TENANT_ID}
    cpd   → {WXO_DEPLOYMENT_MODE, WXO_OTEL_EXPORT_URL, CPD_*}  (no tenant / API key)
    """
    o = overrides or {}
    if _WXO_MODE == "cpd":
        return {
            "WXO_DEPLOYMENT_MODE": "cpd",
            "WXO_OTEL_EXPORT_URL": o.get("wxo_otel_export_url") or _WXO_OTEL_EXPORT_URL,
            "CPD_HOST_URL":        _CPD_HOST_URL,
            "CPD_USERNAME":        _CPD_USERNAME,
            "CPD_PASSWORD":        _CPD_PASSWORD,
            "CPD_INSTANCE_ID":     _CPD_INSTANCE_ID,
            "CPD_NAMESPACE":       _CPD_NAMESPACE,
            "CPD_VERIFY_TLS":      _CPD_VERIFY_TLS,
        }
    return {
        "WXO_OTEL_EXPORT_URL": o.get("wxo_otel_export_url") or _WXO_OTEL_EXPORT_URL,
        "WXO_API_KEY":         o.get("wxo_api_key")         or _WXO_API_KEY,
        "WXO_TOKEN_URL":       o.get("wxo_token_url")       or _WXO_TOKEN_URL,
        "WXO_TENANT_ID":       o.get("wxo_tenant_id")       or _WXO_TENANT_ID,
    }

# ── Flow A — kind:prompt Function App config ──────────────────────────────────
AZURE_LOCATION              = os.environ.get("AZURE_LOCATION",               "eastus")
LOG_ANALYTICS_WORKSPACE_ID  = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID",   "")
LOG_ANALYTICS_WORKSPACE_NAME= os.environ.get("LOG_ANALYTICS_WORKSPACE_NAME", "")
FUNCTION_APP_NAME           = os.environ.get("FUNCTION_APP_NAME",            "wxo-foundry-prompt-tracer")
STORAGE_ACCOUNT_NAME        = os.environ.get("STORAGE_ACCOUNT_NAME",         "")
# Flow A (--prompt) reads WXO_AGENT_MAP_PROMPT; Flow B (--hosted) reads WXO_AGENT_MAP.
# _prompt_step4_app_settings merges WXO_AGENT_MAP_PROMPT into the Function App's
# WXO_AGENT_MAP setting so the poller can resolve every prompt agent to a WXO identity.
WXO_AGENT_MAP_RAW           = os.environ.get("WXO_AGENT_MAP_PROMPT") or os.environ.get("WXO_AGENT_MAP", "{}")

# =============================================================================
# ── Shared helpers ─────────────────────────────────────────────────────────────
# =============================================================================

def _ok(msg):   print(f"  ✓  {msg}")
def _info(msg): print(f"  ·  {msg}")
def _err(msg):  print(f"  ✗  {msg}")
def _head(msg): print(f"\n{'═'*60}\n  {msg}\n{'═'*60}")


# ── Flow B — kind:hosted agent config ─────────────────────────────────────────
# Built from WXO_AGENT_MAP (required).
# WXO_AGENT_MAP entries for hosted agents use the same per-agent fields as the
# prompt map: agent_id, display_name, tenant_id, otel_export_url, environment.
# The Foundry agent name is the map key (must match exactly in Azure AI Foundry).
def _build_agents_table() -> dict[str, dict]:
    """Build the agents table from WXO_AGENT_MAP (required).

    Raises SystemExit if WXO_AGENT_MAP is not set or is empty.

    Map schema (per entry):
      Simple form  — "foundry-agent-name": "wxo-uuid"
      Full form    — "foundry-agent-name": {
                       "agent_id":        "<wxo-uuid>",        # required
                       "tenant_id":       "<wxo-tenant>",      # optional, overrides global
                       "otel_export_url": "<url>",             # optional, overrides global
                       "wxo_agent_name":  "<snake_name>",      # optional
                       "api_key":         "<key>",             # optional
                       "token_url":       "<url>",             # optional
                       "environment":     "draft|live",        # optional
                     }
    """
    raw = os.environ.get("WXO_AGENT_MAP", "").strip()
    if not raw or raw in ("''", '""', "{}"):
        _err("WXO_AGENT_MAP is not set in config/azure.env")
        print()
        print("  Set WXO_AGENT_MAP to a JSON object with one or more agents:")
        print('    WXO_AGENT_MAP=\'{"my-agent": {"agent_id":"<uuid>", "tenant_id":"<tid>", "otel_export_url":"<url>"}}\'')
        print()
        print("  See config/azure.env.example for the full schema.")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except Exception as exc:
        _err(f"WXO_AGENT_MAP is not valid JSON: {exc}")
        sys.exit(1)

    if not data:
        _err("WXO_AGENT_MAP is an empty object — add at least one agent entry.")
        sys.exit(1)

    agents: dict[str, dict] = {}
    for foundry_name, v in data.items():
        if isinstance(v, str):
            # Minimal form: "foundry-agent-name": "wxo-uuid"
            # tenant/url/key all fall back to globals in the platform config
            agents[foundry_name] = {
                "wxo_agent_id":        v,
                "wxo_agent_name":      foundry_name.replace("-", "_"),
                "wxo_tenant_id":       _WXO_TENANT_ID,
                "wxo_otel_export_url": _WXO_OTEL_EXPORT_URL,
                "environment":         _WXO_ENVIRONMENT,
            }
        else:
            # Only agent_id is required per entry — everything else falls back to globals
            aid = v.get("wxo_agent_id") or v.get("agent_id", "")
            if not aid:
                _err(f"WXO_AGENT_MAP entry '{foundry_name}' missing required field: wxo_agent_id")
                sys.exit(1)
            agents[foundry_name] = {
                "wxo_agent_id":        aid,
                "wxo_agent_name":      v.get("wxo_agent_name") or v.get("wxo_display_name") or foundry_name.replace("-", "_"),
                "wxo_tenant_id":       v.get("tenant_id")       or _WXO_TENANT_ID,
                "wxo_otel_export_url": v.get("otel_export_url") or _WXO_OTEL_EXPORT_URL,
                "wxo_api_key":         v.get("api_key",   ""),
                "wxo_token_url":       v.get("token_url", ""),
                "environment":         v.get("environment", _WXO_ENVIRONMENT),
            }
    return agents


AGENTS: dict[str, dict] = _build_agents_table()
DISABLE_CONSOLE  = os.environ.get("OTEL_DISABLE_CONSOLE", "false")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=False)
    if check and result.returncode != 0:
        _err(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        sys.exit(1)
    return result


def _az(args: list[str], capture: bool = False) -> str:
    """Run an `az` CLI command.  Returns stdout if capture=True."""
    cmd = ["az"] + args
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True,
                            capture_output=capture,
                            check=False)
    if result.returncode != 0 and not capture:
        _err(f"az command failed: {' '.join(cmd)}")
        sys.exit(1)
    return result.stdout.strip() if capture else ""


def _az_token(resource: str) -> str:
    r = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _rest(method: str, url: str, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:    err_body = json.loads(raw)
        except: err_body = {"raw": raw.decode()[:400]}
        return e.code, err_body


def _ai(method: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    return _rest(method, f"{PROJECT_ENDPOINT}{path}", body, token)


# =============================================================================
# ══════════════════════════════════════════════════════════════════════════════
#   FLOW A  —  kind:prompt  (Azure Function App polling Log Analytics)
# ══════════════════════════════════════════════════════════════════════════════
# =============================================================================

def _prompt_validate_config() -> None:
    """Abort with helpful message if required Flow A vars are missing."""
    required = {
        "AZURE_SUBSCRIPTION_ID":        SUBSCRIPTION,
        "AZURE_RESOURCE_GROUP":         RESOURCE_GROUP,
        "AZURE_LOCATION":               AZURE_LOCATION,
        "STORAGE_ACCOUNT_NAME":         STORAGE_ACCOUNT_NAME,
        "FUNCTION_APP_NAME":            FUNCTION_APP_NAME,
        "LOG_ANALYTICS_WORKSPACE_ID":   LOG_ANALYTICS_WORKSPACE_ID,
        "LOG_ANALYTICS_WORKSPACE_NAME": LOG_ANALYTICS_WORKSPACE_NAME,
        "ENVIRONMENT_NAME":             _WXO_ENVIRONMENT,
    }
    if _WXO_MODE == "cpd":
        required.update({
            "CPD_HOST_URL":    _CPD_HOST_URL,
            "CPD_USERNAME":    _CPD_USERNAME,
            "CPD_PASSWORD":    _CPD_PASSWORD,
            "CPD_INSTANCE_ID": _CPD_INSTANCE_ID,
            "CPD_NAMESPACE":   _CPD_NAMESPACE,
        })
    else:
        required.update({
            "WXO_TENANT_ID":       _WXO_TENANT_ID,
            "API_KEY":             _WXO_API_KEY,
            "TOKEN_URL":           _WXO_TOKEN_URL,
            "WXO_OTEL_EXPORT_URL": _WXO_OTEL_EXPORT_URL,
        })
    missing = [k for k, v in required.items() if not v or v.startswith("<")]
    if missing:
        for m in missing:
            _err(f"{m} is not set in config.env")
        print("\n  Edit config.env and re-run.")
        sys.exit(1)


def _prompt_step1_storage() -> None:
    _head("Step 1/5 — Storage account")
    exists = _az([
        "storage", "account", "show",
        "--name", STORAGE_ACCOUNT_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "name", "-o", "tsv",
    ], capture=True)
    if exists:
        _ok(f"Storage account already exists: {STORAGE_ACCOUNT_NAME}")
    else:
        _az([
            "storage", "account", "create",
            "--name", STORAGE_ACCOUNT_NAME,
            "--resource-group", RESOURCE_GROUP,
            "--location", AZURE_LOCATION,
            "--sku", "Standard_LRS",
            "--output", "none",
        ])
        _ok(f"Storage account created: {STORAGE_ACCOUNT_NAME}")


def _prompt_step2_function_app() -> None:
    _head("Step 2/5 — Function App")
    exists = _az([
        "functionapp", "show",
        "--name", FUNCTION_APP_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "name", "-o", "tsv",
    ], capture=True)
    if exists:
        _ok(f"Function App already exists: {FUNCTION_APP_NAME}")
    else:
        _az([
            "functionapp", "create",
            "--name", FUNCTION_APP_NAME,
            "--resource-group", RESOURCE_GROUP,
            "--storage-account", STORAGE_ACCOUNT_NAME,
            "--consumption-plan-location", AZURE_LOCATION,
            "--runtime", "python",
            "--runtime-version", "3.11",
            "--functions-version", "4",
            "--os-type", "Linux",
            "--assign-identity",
            "--output", "none",
        ])
        _ok(f"Function App created: {FUNCTION_APP_NAME}")


def _prompt_step3_role_assignment() -> None:
    _head("Step 3/5 — Managed Identity + Monitoring Reader role")

    principal = _az([
        "functionapp", "identity", "show",
        "--name", FUNCTION_APP_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "principalId", "-o", "tsv",
    ], capture=True)

    if not principal:
        _info("Assigning system identity to Function App...")
        _az([
            "functionapp", "identity", "assign",
            "--name", FUNCTION_APP_NAME,
            "--resource-group", RESOURCE_GROUP,
            "--output", "none",
        ])
        principal = _az([
            "functionapp", "identity", "show",
            "--name", FUNCTION_APP_NAME,
            "--resource-group", RESOURCE_GROUP,
            "--query", "principalId", "-o", "tsv",
        ], capture=True)

    _ok(f"Managed identity principal: {principal}")

    workspace_scope = _az([
        "monitor", "log-analytics", "workspace", "show",
        "--workspace-name", LOG_ANALYTICS_WORKSPACE_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "id", "-o", "tsv",
    ], capture=True)

    role_exists = _az([
        "role", "assignment", "list",
        "--assignee", principal,
        "--role", "Monitoring Reader",
        "--scope", workspace_scope,
        "--query", "[0].id", "-o", "tsv",
    ], capture=True)

    if role_exists:
        _ok("Monitoring Reader role already assigned")
    else:
        _az([
            "role", "assignment", "create",
            "--assignee", principal,
            "--role", "Monitoring Reader",
            "--scope", workspace_scope,
            "--output", "none",
        ])
        _ok("Monitoring Reader role assigned on Log Analytics workspace")


def _prompt_step4_app_settings() -> None:
    _head("Step 4/5 — Function App settings (merge WXO_AGENT_MAP)")


    existing_raw = _az([
        "functionapp", "config", "appsettings", "list",
        "--name", FUNCTION_APP_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "[?name=='WXO_AGENT_MAP'].value",
        "-o", "tsv",
    ], capture=True)

    # Merge existing map in Azure with the local WXO_AGENT_MAP from config.env
    merged: dict = {}
    for raw in (existing_raw, WXO_AGENT_MAP_RAW):
        if raw and raw.strip():
            try:
                merged.update(json.loads(raw))
            except json.JSONDecodeError:
                pass
    final_map = json.dumps(merged)

    # Auto-fetch App Insights connection string so the Function App can log to
    # App Insights.  Without this setting the timer fires silently — no logs,
    # no pushes — and the problem is invisible.
    # `show` requires --app (component name); use `list` instead and take the
    # first component in the resource group.
    appinsights_conn = _az([
        "monitor", "app-insights", "component", "list",
        "--resource-group", RESOURCE_GROUP,
        "--query", "[0].connectionString",
        "-o", "tsv",
    ], capture=True)
    if not appinsights_conn:
        _info("APPLICATIONINSIGHTS_CONNECTION_STRING not found — Function App logs will go to stdout only")
    else:
        _ok(f"App Insights connection string fetched (key={appinsights_conn[:30]}...)")

    settings = [
        f"LOG_ANALYTICS_WORKSPACE_ID={LOG_ANALYTICS_WORKSPACE_ID}",
        *[f"{k}={v}" for k, v in _wxo_auth_settings().items()],
        f"WXO_ENVIRONMENT_NAME={_WXO_ENVIRONMENT}",
        f"WXO_AGENT_MAP={final_map}",
        f"WXO_AGENT_ID={os.environ.get('WXO_AGENT_ID', '')}",
        f"WXO_AGENT_DISPLAY_NAME={os.environ.get('WXO_AGENT_DISPLAY_NAME', '')}",
    ]
    if appinsights_conn:
        settings.append(f"APPLICATIONINSIGHTS_CONNECTION_STRING={appinsights_conn}")

    _az([
        "functionapp", "config", "appsettings", "set",
        "--name", FUNCTION_APP_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--settings", *settings,
        "--output", "none",
    ])
    _ok(f"App settings configured (merged WXO_AGENT_MAP: {final_map})")
    _upload_agent_map_blob(final_map)


def _upload_agent_map_blob(final_map: str) -> None:
    """
    Publish the merged agent map to blob storage (azure-webjobs-hosts/wxo-cursor/agent_map.json).
    function_app.py re-reads it on every poll, so Function App instances that started before
    this deploy still route traces by the CURRENT map instead of the one in their frozen env.
    The storage key is passed via env vars so it never appears in the printed command.
    """
    keys = _az([
        "storage", "account", "keys", "list",
        "--account-name", STORAGE_ACCOUNT_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--query", "[0].value", "-o", "tsv",
    ], capture=True)
    if not keys:
        _err(f"Could not read a key for storage account {STORAGE_ACCOUNT_NAME} — agent map blob NOT updated")
        sys.exit(1)

    blob_name = "wxo-cursor/agent_map.json"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(final_map)
        tmp_path = fh.name
    try:
        env = {**os.environ, "AZURE_STORAGE_ACCOUNT": STORAGE_ACCOUNT_NAME, "AZURE_STORAGE_KEY": keys}
        for args in (
            ["az", "storage", "container", "create", "--name", "azure-webjobs-hosts", "--output", "none"],
            ["az", "storage", "blob", "upload", "--container-name", "azure-webjobs-hosts",
             "--name", blob_name, "--file", tmp_path, "--overwrite", "true", "--output", "none"],
        ):
            result = subprocess.run(args, text=True, capture_output=True, env=env)
            if result.returncode != 0:
                _err(f"agent map blob update failed: {result.stderr.strip()[:500]}")
                sys.exit(1)
    finally:
        os.unlink(tmp_path)
    _ok(f"Agent map published to blob azure-webjobs-hosts/{blob_name}")


def _prompt_step5_publish() -> None:
    _head("Step 5/5 — Publish function code")
    # Must run from the azure/ directory where function_app.py + host.json live,
    # not from shared/ (PATCH_DIR) which contains only sitecustomize.py.
    azure_dir = Path(__file__).parent
    result = subprocess.run(
        ["func", "azure", "functionapp", "publish", FUNCTION_APP_NAME, "--python"],
        cwd=str(azure_dir),
        text=True,
    )
    if result.returncode != 0:
        _err(f"`func` publish failed (exit {result.returncode})")
        sys.exit(1)


def flow_prompt(publish_only: bool = False) -> None:
    """Flow A — deploy / update the kind:prompt Function App tracer."""
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Flow A — kind:prompt  (Azure Function App / Log Analytics) ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(f"  Subscription : {SUBSCRIPTION}")
    print(f"  Resource Grp : {RESOURCE_GROUP}")
    print(f"  Location     : {AZURE_LOCATION}")
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │  Config (from config.env)                                   │")
    print("  ├─────────────────────────────────────────────────────────────┤")
    print(f"  │  FUNCTION_APP_NAME   : {FUNCTION_APP_NAME:<37}│")
    print(f"  │  STORAGE_ACCOUNT     : {STORAGE_ACCOUNT_NAME:<37}│")
    print(f"  │  LOG_ANALYTICS_WS    : {LOG_ANALYTICS_WORKSPACE_ID:<37}│")
    print(f"  │  Traces ALL agents   : {'in this workspace automatically':<37}│")
    print("  └─────────────────────────────────────────────────────────────┘")
    print()

    _prompt_validate_config()

    # Set subscription
    subprocess.run(
        ["az", "account", "set", "--subscription", SUBSCRIPTION],
        check=True, capture_output=True,
    )

    if publish_only:
        # Skip infrastructure creation (storage, function app, role assignments)
        # but always re-push app settings so WXO_AGENT_MAP and credentials stay
        # in sync with config/azure.env — then republish the function code.
        _head("--publish-only: skipping infra creation, updating settings + code")
        _prompt_step4_app_settings()
    else:
        _prompt_step1_storage()
        _prompt_step2_function_app()
        _prompt_step3_role_assignment()
        _prompt_step4_app_settings()

    _prompt_step5_publish()

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Done — WXO kind:prompt tracing deployed.                   ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(f"  Function App : {FUNCTION_APP_NAME}")
    print(f"  Traces       : all kind:prompt agents in workspace {LOG_ANALYTICS_WORKSPACE_ID}")
    print( "  Poll interval: every 30 seconds")
    print()
    print("  Stream logs:")
    print(f"    func azure functionapp logstream {FUNCTION_APP_NAME}")
    print()
    print("  To redeploy code only (skip resource creation):")
    print("    python3 wire_agent.py --prompt --publish-only")
    print()


# =============================================================================
# ══════════════════════════════════════════════════════════════════════════════
#   FLOW B  —  kind:hosted  (sitecustomize.py bundled into agent version zip)
# ══════════════════════════════════════════════════════════════════════════════
# =============================================================================

def _poll_active_for(agent_name: str, version: str) -> None:
    token = _az_token("https://ai.azure.com/")
    _info("Polling provisioning status (max 10 min)...")
    for i in range(1, 61):
        time.sleep(10)
        status, body = _ai(
            "GET",
            f"/agents/{agent_name}/versions/{version}?api-version={API_VERSION}",
            token=token,
        )
        agent_status = body.get("status", "unknown") if status == 200 else f"http-{status}"
        print(f"  [{i:02d}] {agent_status}")
        if agent_status == "active":
            _ok(f"Version {version} is active.")
            return
        if agent_status == "failed":
            error = body.get("error", {})
            _err(f"Provisioning failed: {error.get('code')} — {error.get('message')}")
            sys.exit(1)
    _err("Timed out waiting for agent to become active.")
    sys.exit(1)


# ── Step 1: Fetch current agent metadata ──────────────────────────────────────

def step1_fetch_current(agent_name: str) -> dict:
    """Fetch the current live version from Foundry.

    Returns a dict with:
      version       current version string
      protos        list of protocol_versions dicts
      env_vars      existing environment_variables (preserved unchanged)
      deploy_type   "code"      → code_configuration (source zip)
                    "container" → container_configuration (Docker image)
      code_cfg      code_configuration dict  (deploy_type=="code")
      container_uri existing container URI   (deploy_type=="container")
    """
    _head("Step 1/3 — Fetch current agent metadata from Foundry")

    token  = _az_token("https://ai.azure.com/")
    status, body = _ai("GET", f"/agents/{agent_name}?api-version={API_VERSION}", token=token)
    if status != 200:
        _err(f"Agent '{agent_name}' not found ({status}): {json.dumps(body)[:200]}")
        sys.exit(1)

    latest  = body.get("versions", {}).get("latest", {})
    defn    = latest.get("definition", {})
    envs    = dict(defn.get("environment_variables", {}))
    protos  = defn.get("protocol_versions", [])
    ver     = str(latest.get("version", "?"))
    code_cfg     = defn.get("code_configuration")
    container_uri = (defn.get("container_configuration") or {}).get("container_uri", "")
    deploy_type  = "container" if container_uri else "code"

    _ok(f"Agent: {agent_name}  version: {ver}  deploy_type: {deploy_type}")
    _info(f"Protocols: {[p.get('protocol') for p in protos]}")
    _info(f"Existing env vars: {list(envs.keys())}")

    return {
        "version":       ver,
        "protos":        protos,
        "env_vars":      envs,
        "deploy_type":   deploy_type,
        "code_cfg":      code_cfg or {},
        "container_uri": container_uri,
    }


# ── Step 2a: Source-zip path (code_configuration agents) ──────────────────────

_LOCAL_AGENTS_DIR = Path(__file__).parent / "agents"
_DOCKERFILE_PATCH = Path(__file__).parent / "Dockerfile.patch"


def _zip_local_dir(src: Path, dest_dir: Path) -> None:
    """Copy all files from src into dest_dir, skipping sitecustomize.py."""
    import shutil
    files = [f for f in sorted(src.iterdir())
             if f.is_file() and f.name != "sitecustomize.py"]
    if not files:
        _err(f"Directory {src} contains no source files.")
        sys.exit(1)
    _info(f"  Source : {src}  ({len(files)} file(s))")
    for f in files:
        shutil.copy(f, dest_dir / f.name)
        _info(f"  ✓  {f.name}  ({f.stat().st_size} bytes)")


def _github_raw_to_contents_api(raw_url: str) -> str | None:
    """Convert a raw.githubusercontent.com URL to the GitHub Contents API URL.

    raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
    → api.github.com/repos/<owner>/<repo>/contents/<path>?ref=<branch>

    Returns None if the URL is not a GitHub raw URL.
    """
    import re
    m = re.match(
        r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)",
        raw_url,
    )
    if not m:
        return None
    owner, repo, branch, path = m.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"


def _list_github_files(api_url: str, ctx) -> list[dict]:
    """Call the GitHub Contents API and return the list of file entries.

    Each entry is a dict with at least 'name' and 'download_url'.
    Sends Authorization: Bearer <GITHUB_TOKEN> when the env var is set
    (avoids the 60 req/hour unauthenticated rate limit).
    Raises SystemExit on failure.
    """
    headers: dict[str, str] = {
        "User-Agent": "wxo-wire-agent/1.0",
        "Accept":     "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            entries = json.loads(resp.read())
        if not isinstance(entries, list):
            _err(f"GitHub Contents API returned unexpected response for: {api_url}")
            sys.exit(1)
        return [e for e in entries if e.get("type") == "file"]
    except urllib.error.HTTPError as exc:
        _err(f"GitHub Contents API error HTTP {exc.code}: {api_url}")
        if exc.code == 403:
            _err(f"  → Rate-limited by GitHub (60 req/h unauthenticated).")
            _err(f"  → Set GITHUB_TOKEN=<your-pat> in config/azure.env to raise the limit.")
            _err(f"  → Or use AGENT_SOURCE_DIR to skip the API call entirely.")
        sys.exit(1)
    except Exception as exc:
        _err(f"Failed to reach GitHub Contents API: {exc}")
        sys.exit(1)


def _fetch_agent_source(dest_dir: Path) -> None:
    """Download all agent source files from AGENT_SOURCE_URL.

    Behaviour depends on the URL type:
      • GitHub raw URL (raw.githubusercontent.com)
            → Uses the GitHub Contents API to list ALL files in the directory,
              then downloads each one. No hardcoded file list needed.
      • Any other HTTP(S) URL
            → Directory listing is not possible; downloads main.py and
              requirements.txt only. For agents with extra files, use
              AGENT_SOURCE_DIR or place files in azure/agents/<agent-name>/.

    Raises SystemExit if AGENT_SOURCE_URL is not set or on any HTTP/network failure.
    """
    import ssl as _ssl

    if not _AGENT_SOURCE_BASE:
        _err("AGENT_SOURCE_URL is not set and no local agent source directory was found.")
        _err("  Set one of the following in config/azure.env:")
        _err("    export AGENT_SOURCE_URL=\"https://raw.githubusercontent.com/<org>/<repo>/<branch>/path\"")
        _err("    export AGENT_SOURCE_DIR=\"/path/to/your/agent\"")
        _err("  Or place your agent files in:  azure/agents/<foundry-agent-name>/")
        sys.exit(1)

    ctx = _ssl.create_default_context()

    _info(f"  Remote source  : {_AGENT_SOURCE_BASE}")

    # ── GitHub raw URL: use Contents API to discover all files ───────────────
    api_url = _github_raw_to_contents_api(_AGENT_SOURCE_BASE)
    if api_url:
        _info(f"  ·  GitHub raw URL detected — listing files via Contents API")
        _info(f"  ·  Contents API : {api_url}")
        file_entries = _list_github_files(api_url, ctx)
        filenames = [e["name"] for e in file_entries]
        _info(f"  Files found    : {', '.join(filenames)}")
        for entry in file_entries:
            filename    = entry["name"]
            download_url = entry["download_url"]
            try:
                req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": "wxo-wire-agent/1.0"},
                )
                with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                    data = resp.read()
                (dest_dir / filename).write_bytes(data)
                _info(f"  ✓  {filename}  ({len(data)} bytes)")
            except urllib.error.HTTPError as exc:
                _err(f"HTTP {exc.code} downloading {filename} from {download_url}")
                sys.exit(1)
            except Exception as exc:
                _err(f"Failed to fetch {filename}: {exc}")
                sys.exit(1)
        return

    # ── Non-GitHub URL: no directory listing available ────────────────────────
    _info(f"  ·  Non-GitHub URL — fetching {', '.join(_REMOTE_FALLBACK_FILES)} directly")
    _info(f"  ·  Note: directory listing is not possible for non-GitHub URLs.")
    _info(f"  ·  For agents with tool files, use AGENT_SOURCE_DIR instead.")
    for filename in _REMOTE_FALLBACK_FILES:
        url = f"{_AGENT_SOURCE_BASE}/{filename}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wxo-wire-agent/1.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                data = resp.read()
            (dest_dir / filename).write_bytes(data)
            _info(f"  ✓  {filename}  ({len(data)} bytes)  ← {url}")
        except urllib.error.HTTPError as exc:
            _err(f"HTTP {exc.code} downloading {url}")
            _err(f"  → Verify AGENT_SOURCE_URL serves '{filename}' at: {url}")
            _err(f"  → To use local files instead: create azure/agents/<agent-name>/{filename}")
            _err(f"  → Or set AGENT_SOURCE_DIR=/path/to/agent in config/azure.env")
            sys.exit(1)
        except Exception as exc:
            _err(f"Failed to fetch {url}: {exc}")
            sys.exit(1)


def step2_build_zip(agent_name: str, code_cfg: dict, with_hook: bool = True) -> Path:
    """Build deployment zip for a code_configuration agent.

    Source resolution order (first match wins)
    -------------------------------------------
    1. AGENT_SOURCE_DIR env var      — any local directory; ALL files zipped
    2. azure/agents/<agent-name>/    — per-agent directory in this repo; ALL files zipped
    3. azure/agent/                  — legacy single-agent fallback; ALL files zipped
    4. AGENT_SOURCE_URL (GitHub raw) — Contents API lists ALL files; all downloaded
    5. AGENT_SOURCE_URL (other URL)  — fetches main.py + requirements.txt only
    No match → exits with an error (AGENT_SOURCE_URL required when no local dir exists)

    sitecustomize.py is always excluded from local source (injected separately).
    Set GITHUB_TOKEN env var to authenticate GitHub API calls and avoid rate-limiting.
    """
    label = "patched (+ sitecustomize.py)" if with_hook else "clean"
    _head(f"Step 2/3 — Build source zip [{label}]")

    import tempfile as _tmp, shutil as _sh, zipfile as _zf

    tmp_dir  = Path(_tmp.mkdtemp(prefix="wxo-agent-"))
    zip_path = Path(_tmp.gettempdir()) / f"{agent_name}-{'patched' if with_hook else 'clean'}.zip"

    try:
        # ── 1. AGENT_SOURCE_DIR — user-supplied local directory ───────────────
        if _AGENT_SOURCE_DIR:
            custom_dir = Path(_AGENT_SOURCE_DIR).expanduser().resolve()
            if not custom_dir.is_dir():
                _err(f"AGENT_SOURCE_DIR does not exist: {custom_dir}")
                sys.exit(1)
            _info(f"  Source : AGENT_SOURCE_DIR={custom_dir}")
            _zip_local_dir(custom_dir, tmp_dir)

        # ── 2. azure/agents/<agent-name>/ — per-agent repo directory ─────────
        elif (_LOCAL_AGENTS_DIR / agent_name).is_dir() and any(
                (_LOCAL_AGENTS_DIR / agent_name).iterdir()):
            _zip_local_dir(_LOCAL_AGENTS_DIR / agent_name, tmp_dir)

        # ── 3. azure/agent/ — legacy single-agent directory ──────────────────
        elif (Path(__file__).parent / "agent").is_dir() and any(
                (Path(__file__).parent / "agent").iterdir()):
            _zip_local_dir(Path(__file__).parent / "agent", tmp_dir)

        # ── 4 & 5. Remote fetch — AGENT_SOURCE_URL (required) ────────────────
        else:
            _info(f"  ✗  No local source found for '{agent_name}'")
            _info(f"  ·  Checked: AGENT_SOURCE_DIR (unset), "
                  f"{_LOCAL_AGENTS_DIR / agent_name}, "
                  f"{Path(__file__).parent / 'agent'}")
            _info(f"  ↓  Attempting remote fetch via AGENT_SOURCE_URL")
            _fetch_agent_source(tmp_dir)   # exits with error if AGENT_SOURCE_URL unset

        if with_hook:
            hook_src = PATCH_DIR / "sitecustomize.py"
            if not hook_src.exists():
                _err(f"sitecustomize.py not found at {hook_src}")
                sys.exit(1)
            _sh.copy(hook_src, tmp_dir / "sitecustomize.py")
            _info(f"Injected sitecustomize.py ({hook_src.stat().st_size} bytes)")

        with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            for f in sorted(tmp_dir.iterdir()):
                zf.write(f, f.name)
                _info(f"  + {f.name}  ({f.stat().st_size} bytes)")
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)

    _info(f"Zip: {zip_path}  ({zip_path.stat().st_size} bytes)")
    _ok(f"Zip built: {zip_path.name}")
    return zip_path


# ── Step 2b: Docker-patch path (container_configuration agents) ───────────────

def step2_build_and_push(agent_name: str, agent_cfg: dict,
                          container_uri: str, with_hook: bool = True) -> str:
    """Pull existing container, add sitecustomize.py layer, push to ACR."""
    
    if not with_hook:
        _head("Step 2/3 — Revert: restoring original image")
        return container_uri.replace(":wxo-patched", ":latest") if ":wxo-patched" in container_uri else container_uri

    _head("Step 2/3 — Build + push patched Docker image")
    
    # Extract registry more robustly
    acr_registry = agent_cfg.get("acr_registry", "")
    if not acr_registry:
        # Parse from URI: gcr.io/project/image:tag → gcr.io
        parts = container_uri.split("/")
        if len(parts) > 0 and "." in parts[0]:
            acr_registry = parts[0]
    
    if not acr_registry:
        _err(f"Cannot determine registry from {container_uri}")
        _err(f"Add 'acr_registry' to WXO_AGENT_MAP['{agent_name}']")
        sys.exit(1)

    base = container_uri.replace(":wxo-patched", ":latest")
    img_name = base.split("/")[-1].rsplit(":", 1)[0]
    patched_image = f"{acr_registry}/{img_name}:wxo-patched-{int(time.time())}"  # ← timestamp for versioning
    
    _info(f"Source : {base}")
    _info(f"Patched: {patched_image}")

    # Validate Dockerfile.patch
    if not _DOCKERFILE_PATCH.exists():
        _err(f"Dockerfile.patch not found at {_DOCKERFILE_PATCH}")
        sys.exit(1)

    # ACR login with retry
    registry_name = acr_registry.split(".")[0]
    for attempt in range(3):
        result = subprocess.run(
            ["az", "acr", "login", "--name", registry_name],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            break
        if attempt == 2:
            _err(f"Failed to login to ACR '{registry_name}': {result.stderr[:200]}")
            sys.exit(1)
        time.sleep(2 ** attempt)

    # Docker operations with error checking
    try:
        # Pull with retry
        for attempt in range(3):
            result = subprocess.run(
                ["docker", "pull", "--platform", "linux/amd64", base],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                _info(f"Pulled: {base}")
                break
            if attempt == 2:
                _err(f"Failed to pull {base}: {result.stderr[:200]}")
                sys.exit(1)
            time.sleep(2 ** attempt)

        # Build
        repo_root = str(PATCH_DIR.parent)
        result = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64",
             "--build-arg", f"SOURCE_IMAGE={base}",
             "-t", patched_image,
             "-f", str(_DOCKERFILE_PATCH),
             repo_root],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            _err(f"Docker build failed: {result.stderr[:500]}")
            sys.exit(1)
        _ok(f"Image built: {patched_image}")

        # Push
        result = subprocess.run(
            ["docker", "push", patched_image],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            _err(f"Docker push failed: {result.stderr[:500]}")
            sys.exit(1)
        _ok(f"Image pushed: {patched_image}")
        
        return patched_image

    except subprocess.TimeoutExpired:
        _err("Docker operation timed out (pull/build/push)")
        sys.exit(1)
    except Exception as e:
        _err(f"Unexpected error during image patching: {e}")
        sys.exit(1)


# ── Step 3: Create new agent version ──────────────────────────────────────────

def step3_create_version(
    agent_name: str,
    agent_cfg: dict,
    current: dict,
    with_hook: bool,
    zip_path: "Path | None" = None,
    patched_image: str = "",
) -> str:
    """Create a new agent version — source zip or container image, auto-detected."""
    _head("Step 3/3 — Create new agent version in Foundry")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import (
        CodeConfiguration, CodeDependencyResolution,
        HostedAgentDefinition, ProtocolVersionRecord,
    )
    from azure.identity import DefaultAzureCredential

    protos = current["protos"]
    code_cfg = current["code_cfg"]

    # Preserve all user env vars; only overlay/strip WXO keys
    env_vars: dict = dict(current["env_vars"])
    if with_hook:
        env_vars.update(_wxo_auth_settings(overrides=agent_cfg))
        env_vars.update({
            "WXO_AGENT_ID":         agent_cfg["wxo_agent_id"],
            "WXO_AGENT_NAME":       agent_cfg["wxo_agent_name"],
            "WXO_ENVIRONMENT":      agent_cfg.get("environment",         _WXO_ENVIRONMENT),
            "OTEL_DISABLE_CONSOLE": DISABLE_CONSOLE,
        })
    else:
        for k in ("WXO_OTEL_EXPORT_URL", "WXO_API_KEY", "WXO_TOKEN_URL",
                  "WXO_AGENT_ID", "WXO_AGENT_NAME", "WXO_TENANT_ID",
                  "WXO_DEPLOYMENT_MODE", "CPD_HOST_URL", "CPD_USERNAME", "CPD_PASSWORD",
                  "CPD_INSTANCE_ID", "CPD_NAMESPACE", "CPD_VERIFY_TLS",
                  "WXO_ENVIRONMENT", "OTEL_DISABLE_CONSOLE"):
            env_vars.pop(k, None)

    proto_records = [
        ProtocolVersionRecord(protocol=p.get("protocol", "responses"),
                              version=p.get("version", "2.0.0"))
        for p in protos
    ] or [ProtocolVersionRecord(protocol="responses", version="2.0.0")]

    description = ("WXO tracing enabled — sitecustomize.py hook applied."
                   if with_hook else "Clean version — WXO tracing removed.")

    with (DefaultAzureCredential() as credential,
          AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential) as client):

        if current["deploy_type"] == "code":
            # Source-zip path
            assert zip_path is not None
            with zip_path.open("rb") as code_stream:
                created = client.agents.create_version_from_code(
                    agent_name=agent_name,
                    description=description,
                    definition=HostedAgentDefinition(
                        cpu=code_cfg.get("cpu", "0.5"),
                        memory=code_cfg.get("memory", "1Gi"),
                        code_configuration=CodeConfiguration(
                            runtime=code_cfg.get("runtime", "python_3_14"),
                            entry_point=code_cfg.get("entry_point", ["python", "main.py"]),
                            dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
                        ),
                        environment_variables=env_vars,
                        protocol_versions=proto_records,
                    ),
                    code=code_stream,
                )
        else:
            # Container-image path
            try:
                from azure.ai.projects.models import ContainerConfiguration
            except ImportError:
                _err("ContainerConfiguration not available in this SDK version.")
                sys.exit(1)
            created = client.agents.create_version_from_container(
                agent_name=agent_name,
                description=description,
                definition=HostedAgentDefinition(
                    cpu="0.5",
                    memory="1Gi",
                    container_configuration=ContainerConfiguration(
                        container_uri=patched_image,
                    ),
                    environment_variables=env_vars,
                    protocol_versions=proto_records,
                ),
            )

    new_version = str(created.version)
    _ok(f"Version {new_version} created — provisioning started.")
    return new_version


# ── Wire / Revert a single hosted agent ───────────────────────────────────────

def _wire_one(agent_name: str, do_revert: bool) -> None:
    agent_cfg = AGENTS.get(agent_name)
    if agent_cfg is None:
        _err(f"Unknown agent '{agent_name}'. Known agents: {list(AGENTS)}")
        sys.exit(1)

    action = "Revert" if do_revert else "Wire"
    _head(f"{action} — WXO tracing on {agent_name}")

    current = step1_fetch_current(agent_name)
    zip_path = None
    patched_image = ""

    if current["deploy_type"] == "code":
        zip_path = step2_build_zip(agent_name, current["code_cfg"], with_hook=not do_revert)
    else:
        patched_image = step2_build_and_push(
            agent_name, agent_cfg, current["container_uri"], with_hook=not do_revert)

    new_version = step3_create_version(
        agent_name, agent_cfg, current,
        with_hook=not do_revert,
        zip_path=zip_path,
        patched_image=patched_image,
    )
    _poll_active_for(agent_name, new_version)
    base = f"{PROJECT_ENDPOINT}/agents/{agent_name}"
    print()
    print("══════════════════════════════════════════════════════════════════")
    if do_revert:
        print("  Reverted — WXO tracing removed.")
        print(f"  Agent   : {agent_name}")
        print(f"  Version : {new_version}")
        print(f"  Endpoint: {base}/endpoint/protocols/openai/responses?api-version=v1")
    else:
        print("  Done — WXO tracing installed.")
        print(f"  Agent         : {agent_name}  (updated)")
        print(f"  Version       : {new_version}  (previous versions untouched)")
        if patched_image:
            print(f"  Patched image : {patched_image}")
        print()
        print("  Trace path: sitecustomize.py → POST WXO_OTEL_EXPORT_URL")
        print()
        print("  Endpoints:")
        print(f"    Responses   POST  {base}/endpoint/protocols/openai/responses?api-version=v1")
        print(f"    Invocations POST  {base}/endpoint/protocols/invocations?api-version=v1")
        print()
        print(f"  To remove tracing:  python3 wire_agent.py --hosted --agent {agent_name} --revert")
    print("══════════════════════════════════════════════════════════════════")


def flow_hosted(agent_names: list[str], do_revert: bool = False) -> None:
    """Flow B — wire / revert WXO tracing on one or more kind:hosted agents."""
    action = "Revert" if do_revert else "Wire"
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print(f"║  Flow B — kind:hosted  ({action} WXO tracing)                  ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    for name in agent_names:
        cfg = AGENTS.get(name, {})
        print(f"  Agent   : {name}  (wxo_id={cfg.get('wxo_agent_id', '?')})")
    print(f"  Project : {PROJECT_ENDPOINT}")
    if not do_revert:
        print(f"  WXO     : {_WXO_OTEL_EXPORT_URL[:60]}...")
        print(f"  Hook    : {PATCH_DIR / 'sitecustomize.py'}")
    print()

    subprocess.run(
        ["az", "account", "set", "--subscription", SUBSCRIPTION],
        check=True, capture_output=True,
    )

    for name in agent_names:
        _wire_one(name, do_revert=do_revert)


# =============================================================================
# ── Main ──────────────────────────────────────────────────────────────────────
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wire_agent.py",
        description=(
            "Wire Azure Foundry agents with WXO tracing.\n\n"
            "  Agent input: WXO_AGENT_MAP in config/azure.env (required for both flows).\n"
            "  One or many agents — each key is the Foundry agent name.\n\n"
            "  --prompt   Flow A: kind:prompt — deploy/update the Azure Function App\n"
            "             that polls Log Analytics and forwards spans to WXO.\n\n"
            "  --hosted   Flow B: kind:hosted — bundle sitecustomize.py into a new\n"
            "             agent source version (default when neither flag is given)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Flow selection ─────────────────────────────────────────────────────────
    flow_group = parser.add_mutually_exclusive_group()
    flow_group.add_argument(
        "--prompt", action="store_true",
        help="Flow A: deploy/update the Function App for kind:prompt agents",
    )
    flow_group.add_argument(
        "--hosted", action="store_true",
        help="Flow B: wire/revert sitecustomize.py on kind:hosted agents (default)",
    )

    # ── Shared flags ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without making any changes",
    )

    # ── Flow A flags ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--publish-only", action="store_true",
        help="(--prompt) Skip resource creation; only republish the function code",
    )

    # ── Flow B flags ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--revert", action="store_true",
        help="(--hosted) Remove WXO tracing — deploy a clean version without the hook",
    )
    parser.add_argument(
        "--agent", metavar="NAME",
        help="(--hosted) Wire only this agent (must be a key in WXO_AGENT_MAP)",
    )

    args = parser.parse_args()

    # Default to --hosted when neither flag is given (backward-compat)
    use_prompt = args.prompt
    use_hosted = args.hosted or not args.prompt

    # ── Banner ─────────────────────────────────────────────────────────────────
    flow_label = "Flow A — kind:prompt" if use_prompt else "Flow B — kind:hosted"
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print(f"║  WXO Azure Foundry Agent Tracing  ({flow_label})  ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    if args.dry_run:
        print(f"  --dry-run: would run {flow_label}.")
        if use_prompt:
            print(f"    Function App : {FUNCTION_APP_NAME}")
            print(f"    Resource Grp : {RESOURCE_GROUP}")
            print(f"    Log Analytics: {LOG_ANALYTICS_WORKSPACE_ID}")
            print(f"    publish-only : {args.publish_only}")
        else:
            print(f"    Agents  : {list(AGENTS)}")
            print(f"    Project : {PROJECT_ENDPOINT}")
            print(f"    Revert  : {args.revert}")
        print()
        print("  No changes made.")
        return

    # ── Dispatch ───────────────────────────────────────────────────────────────
    if use_prompt:
        flow_prompt(publish_only=args.publish_only)
    else:
        if args.agent:
            agent_names = [args.agent]
        else:
            agent_names = list(AGENTS)
        flow_hosted(agent_names=agent_names, do_revert=args.revert)


if __name__ == "__main__":
    main()
