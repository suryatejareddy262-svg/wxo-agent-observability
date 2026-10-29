# WXO Agent Analytics Tracing — Unified Multi-Cloud Repo

Sends traces from agents running on **AWS**, **Azure**, and **Google Cloud (Vertex AI)** to **IBM watsonx Orchestrate (WXO) Agent Analytics** using OpenTelemetry.

All three platforms are wired through a **single entrypoint** (`wire_agent.sh`) and per-platform config files under `config/`.

---

## Supported platforms and flows

| Platform | Flow | Mechanism |
|---|---|---|
| **AWS AgentCore — Harness** | `--aws --harness` | Inject `OTEL_*` env vars into the AWS-managed Harness image. The pre-installed ADOT SDK emits spans → API GW → Lambda → WXO. No image change. |
| **AWS AgentCore — Runtime** | `--aws --runtime` | Patch your ECR image with `sitecustomize.py`. Python auto-imports it at startup and posts spans directly to WXO. |
| **Azure AI Foundry — kind:prompt** | `--azure --prompt` | Azure Function App polls `AppGenAIContent` in Log Analytics every 30 s and forwards spans to WXO. One deployment covers all prompt agents. |
| **Azure AI Foundry — kind:hosted** | `--azure --hosted` | Bundle `sitecustomize.py` into a new agent source version. Python auto-imports it at startup — before `main.py`. |
| **Vertex AI — Reasoning Engine** | `--vertex --reasoning-engine` | WXO trace code embedded directly in `query()` via cloudpickle. No container access needed. |
| **Vertex AI — Self-hosted container** | `--vertex --selfhosted` | Patch your Cloud Run / GKE image with `sitecustomize.py`. Python auto-imports it at startup and posts spans directly to WXO. |
| **Salesforce Agentforce / Data 360** *(Coming in Next Release)* | `--salesforce` | In-org Apex poller + REST endpoint forwards Agentforce session traces to WXO. |
| **OTLP Trace Collector / Forwarder (any OTLP-capable agent, e.g. Claude)** | `--forwarder [--deploy [--claude]]` | Receives standard OTLP/HTTP push (JSON or Protobuf), maps agent names/IDs to WXO agent UUIDs & tenants, transforms traces into WXO format, and ships to WXO. Deployable as standalone daemon, Docker, AWS Lambda, or Azure Functions. See [OTLP Trace Collector](#otlp-trace-collector-forwarder). |

---

## Repository structure

```
wxo-external-agent-tracing/
├── wire_agent.sh                ← Top-level entrypoint — dispatches to aws/azure/vertex/forwarder
│
├── config/
│   ├── shared.env.example       ← WXO API key + token URL (copy → shared.env)
│   ├── aws.env.example          ← AWS account + WXO agent map (copy → aws.env)
│   ├── azure.env.example        ← Azure subscription + WXO agent map (copy → azure.env)
│   ├── forwarder.env.example    ← OTLP Forwarder: WXO tenant, agent map & target (copy → forwarder.env)
│   ├── salesforce.env.example   ← Salesforce Agentforce (coming in next release)
│   └── vertex.env.example       ← GCP project + WXO agent map (copy → vertex.env)
│
└── src/                         ← All code (no need to edit)
    ├── forwarder/                   ← OTLP Trace Forwarder (Docker, AWS Lambda, Azure Functions)
    │   ├── deploy.py                ← --deploy / --sync for aws | azure | docker
    │   ├── forwarder.py             ← Core OTLP decode, agent mapping & WXO forwarding engine
    │   ├── wire_agent.py            ← CLI & standalone HTTP daemon entrypoint
    │   ├── forwarder_ctl.py         ← AWS helper used by deploy.py (also usable directly)
    │   ├── lambda_function.py       ← AWS Lambda handler
    │   ├── function_app.py          ← Azure Functions v2 Python HTTP trigger app
    │   ├── Dockerfile               ← Container image
    │   ├── hook.py, sitecustomize.py← In-process auto-instrumentation hook
    │   ├── template.yaml            ← Optional AWS SAM template
    │   └── setup_db.sh, manage_db.py← Optional DynamoDB / PostgreSQL agent-map storage
    │
    ├── shared/
    │   └── sitecustomize.py         ← CANONICAL sitecustomize.py — edit only here
    │
    ├── container_hook/
    │   └── Dockerfile               ← One-line patch layer: copies sitecustomize.py → site-packages
    │
    ├── aws/
    │   ├── wire_agent.sh            ← AWS entrypoint
    │   ├── ssm_params.sh            ← Store WXO credentials in SSM (run once)
    │   ├── agentcore_trace_hook.py  ← Lambda: decode OTLP protobuf + forward to WXO
    │   ├── agent-patch/
    │   │   └── Dockerfile           ← Builds patched ECR image (runtime flow)
    │   └── lib/
    │       ├── infra.sh             ← Lambda + API GW provisioning
    │       ├── flow_harness.sh      ← Harness flow (env vars only)
    │       └── flow_runtime.sh      ← Runtime flow (image patch)
    │
    ├── azure/
    │   ├── wire_agent.py            ← Azure entrypoint (Python)
    │   ├── function_app.py          ← Azure Function App: polls Log Analytics → WXO
    │   ├── Dockerfile.patch         ← Optional: patch an existing container image
    │   ├── requirements.txt         ← azure-functions (for the Function App)
    │   ├── host.json                ← Function App runtime config
    │   └── local.settings.json      ← Local dev settings (placeholder values)
    │
    └── vertex/
        ├── wire_agent.sh            ← Vertex entrypoint
        ├── lib/
        │   └── flow_selfhosted.sh   ← Self-hosted container flow
        └── reasoning_engine/
            └── deploy.py            ← Reasoning Engine deploy + patch
```

---

## Quick start

### Step 1 — Configure your credentials (Choose Option A or Option B)

You can choose whichever configuration style you prefer:

#### Option A: Dedicated Separate Files under `config/` (Recommended for focus)
Edit only the files relevant to your platform without scrolling through other cloud settings:

```bash
cd wxo-external-agent-tracing

# 1. Account-wide WXO credentials (set once):
cp config/shared.env.example config/shared.env && vi config/shared.env

# 2. Platform-specific config (only what you use):
cp config/aws.env.example        config/aws.env        # For AWS
cp config/azure.env.example      config/azure.env      # For Azure
cp config/vertex.env.example     config/vertex.env     # For GCP Vertex
cp config/forwarder.env.example  config/forwarder.env  # For OTLP Forwarder
# cp config/salesforce.env.example config/salesforce.env # Salesforce (coming in next release)
```

#### Option B: Single Unified `config.env`
If you prefer having all platforms in one file with section headers (`[forwarder]`, `[aws]`, `[azure]`, `[vertex]`):

```bash
cp config.env.example config.env && vi config.env
```

### Step 2 — Check configuration status & make scripts executable

```bash
chmod +x wire_agent.sh src/aws/wire_agent.sh src/aws/ssm_params.sh src/vertex/wire_agent.sh

# Check readiness across all platforms
./wire_agent.sh --status

# Inspect a specific platform's configuration
./wire_agent.sh --show aws
```

### Step 3 — Wire your agents

```bash
# ── AWS ───────────────────────────────────────────────────────────────────────
./src/aws/ssm_params.sh              # run once — stores WXO credentials in SSM
./wire_agent.sh --aws --harness  # Flow A: Harness (env vars only)
./wire_agent.sh --aws --runtime  # Flow B: Runtime (image patch)

# ── Azure ─────────────────────────────────────────────────────────────────────
az login
./wire_agent.sh --azure --prompt  # Flow A: kind:prompt (Function App)
./wire_agent.sh --azure --hosted  # Flow B: kind:hosted (sitecustomize.py)

# ── Vertex ────────────────────────────────────────────────────────────────────
gcloud auth login
./wire_agent.sh --vertex --reasoning-engine  # Reasoning Engine
./wire_agent.sh --vertex --selfhosted        # Self-hosted container

# ── Salesforce Agentforce (Coming in Next Release) ───────────────────────────
# ./wire_agent.sh --salesforce                   # deploy in-org Apex poller & REST endpoint
# ./wire_agent.sh --salesforce --dry-run         # validate configuration without changes

# ── OTLP Trace Collector / Forwarder (e.g. Claude) ───────────────────────────
# 1. Fill in config/forwarder.env (WXO credentials, WXO_AGENT_MAP, FORWARDER_TARGET).
# 2. Deploy, configure Claude, or run locally:
./wire_agent.sh --forwarder --deploy             # deploy forwarder (aws|azure|docker per config) + push config
./wire_agent.sh --forwarder --deploy --claude    # deploy forwarder AND register Claude (Anthropic) telemetry
./wire_agent.sh --forwarder                      # or: run the OTLP receiver locally on port 4318
./wire_agent.sh --forwarder --dry-run            # validate config & IAM credentials
```

All commands support `--dry-run` to preview changes without deploying.

---

## Platform details

### AWS AgentCore

Two flows controlled by `--harness` / `--runtime`:

#### Flow A — Harness (configuration-driven)

```mermaid
flowchart LR
    A["Harness agent<br/>AWS-managed image"] --> B["ADOT SDK<br/>OTLP/HTTP protobuf"]
    B --> C["API Gateway"]
    C --> D["Lambda collector<br/>decode + enrich"]
    D --> E["WXO Agent Analytics"]
```

No image changes. The ADOT SDK is pre-installed in AWS-managed images.
`wire_agent.sh` injects `OTEL_*` env vars into the Harness and stores agent identity in SSM.

#### Flow B — Runtime (image-driven)

```mermaid
flowchart LR
    A["Runtime agent<br/>custom ECR image"] --> B["sitecustomize.py<br/>startup hook"]
    B --> C["Agent HTTP handler"]
    B --> D["WXO endpoint<br/>direct POST"]
```

`wire_agent.sh` builds a patched ECR image and updates the Runtime.
`sitecustomize.py` is the only addition — no user code changes.

**Prerequisites:** AWS CLI authenticated, Docker running (runtime only), `ssm_params.sh` run once.

---

### Azure AI Foundry

Two flows controlled by `--prompt` / `--hosted`:

#### Flow A — kind:prompt (Function App polling)

```mermaid
flowchart LR
    A["Prompt agents"] -->|auto-telemetry| B["App Insights /\nLog Analytics"]
    B -->|KQL every 30s| C["Azure Function App"]
    C -->|OTLP spans| D["WXO Agent Analytics"]
```

`wire_agent.py --prompt` deploys a Function App that polls Log Analytics every 30 s.
One deployment covers **all** prompt agents in the workspace.

#### Flow B — kind:hosted (sitecustomize.py injection)

```mermaid
flowchart LR
    A["wire_agent.py --hosted"] -->|inject sitecustomize.py| B["New agent version zip"]
    B -->|create_version_from_code| C["Foundry managed container"]
    C -->|spans at runtime| D["WXO Agent Analytics"]
```

`wire_agent.py --hosted` creates a new agent version bundled with `sitecustomize.py`.
The previous version is untouched — Foundry keeps full version history.

#### Agent source resolution (Flow B)

`wire_agent.py --hosted` needs your agent's source files to build the deployment zip.
It resolves them in this order — the first match wins:

| Priority | Source | Behaviour |
|---|---|---|
| **1** | `AGENT_SOURCE_DIR` (env var) | Any local directory — **all files zipped automatically** |
| **2** | `src/azure/agents/<foundry-agent-name>/` | Per-agent folder in this repo — **all files zipped** |
| **3** | `src/azure/agent/` | Legacy single-agent fallback — **all files zipped** |
| **4** | `AGENT_SOURCE_URL` — GitHub raw URL | Contents API lists the directory; **all files downloaded** |
| **5** | `AGENT_SOURCE_URL` — other HTTP(S) URL | `main.py` + `requirements.txt` fetched only |
| **6** | Built-in GitHub mirror | Microsoft Foundry sample via Contents API — **all files** |

**Local directories (options 1–3): all files included automatically**

No file list is needed. Every file in the directory is zipped — `main.py`, `requirements.txt`, tool files, helper modules, proto stubs, anything. Only `sitecustomize.py` is excluded (it is injected separately by the tool).

```bash
# Option 1 — agent lives outside this repo:
export AGENT_SOURCE_DIR="/absolute/path/to/my-agent"
# or relative to where you run the script:
export AGENT_SOURCE_DIR="../../my-agents/my-agent"

# Option 2 — agent is in this repo (no env var needed):
#   Drop all files into:  src/azure/agents/<your-foundry-agent-name>/
#   wire_agent.py detects it automatically.
```

**Remote fetch (options 4–6): triggered only when no local directory is found**

For **GitHub raw URLs** (`raw.githubusercontent.com`), the tool calls the GitHub Contents API to list the directory and downloads **every file** — no hardcoded file list. For other URLs, only `main.py` and `requirements.txt` are fetched (HTTP servers have no standard directory-listing protocol).

```bash
# config/azure.env

# GitHub raw URL — ALL files in the directory are fetched automatically:
export AGENT_SOURCE_URL="https://raw.githubusercontent.com/<org>/<repo>/<branch>/path/to/agent"

# Other HTTP(S) server — main.py + requirements.txt only:
export AGENT_SOURCE_URL="https://<account>.blob.core.windows.net/<container>/<agent-folder>"

# Leave unset → Microsoft Foundry GitHub mirror (also fetches all files via Contents API).

# Optional: GitHub PAT to avoid the 60 req/h unauthenticated rate limit.
# Required for private repos. Needs read:contents scope.
export GITHUB_TOKEN="ghp_..."
```

**Additional flags:**
```bash
python3 src/azure/wire_agent.py --hosted --revert          # remove tracing
python3 src/azure/wire_agent.py --prompt --publish-only    # redeploy code only
python3 src/azure/wire_agent.py --prompt --dry-run
python3 src/azure/wire_agent.py --hosted --dry-run
```

**Prerequisites:** `az` CLI authenticated, `func` CLI (Flow A only), `azure-ai-projects` SDK (Flow B only).

---

### Vertex AI

Two flows controlled by `--reasoning-engine` / `--selfhosted`:

#### Flow A — Reasoning Engine (bytecode injection)

```
Caller → Vertex AI Reasoning Engine (Google-managed container)
           └─ query() with embedded WXO trace code
                └─ POST span to WXO_OTEL_EXPORT_URL
```

`wire_agent.sh --reasoning-engine` re-uploads the agent bytecode with WXO tracing frozen in.
No new engine is created — updates the existing one via `engine.update()`.

#### Flow B — Self-hosted container (image patch)

```mermaid
flowchart LR
    A["wire_agent.sh --selfhosted"] -->|inject sitecustomize.py| B["Patched image"]
    B -->|docker push| C["Artifact Registry"]
    C -->|gcloud run deploy| D["Cloud Run / GKE"]
    D -->|spans at runtime| E["WXO Agent Analytics"]
```

`wire_agent.sh --selfhosted` builds a patched image and deploys to Cloud Run (or just pushes for GKE).

**Prerequisites:** `gcloud` authenticated, Docker running.

---

## OTLP Trace Collector (Forwarder)

The OTLP Trace Collector — called the *forwarder* in config files and commands — is a small HTTP service that you deploy once. Any agent or SDK that speaks standard OpenTelemetry (OTLP/HTTP, JSON or Protobuf) can push traces to it.

> **Other Use Cases:** While shown with Claude below, you can use this forwarder to trace **any** agent or framework (LangChain, LangGraph, CrewAI, AutoGen, LlamaIndex, LiteLLM, or custom agents in Python/Node/Java/Go). Simply point standard OpenTelemetry exporters to `https://<forwarder-url>`.

For each trace it:

1. Works out which WXO agent and tenant the trace belongs to (see [Agent Identity Resolution Order](#agent-identity-resolution-order)).
2. Converts the spans to the WXO trace format.
3. Gets an IBM IAM token from your `API_KEY` and posts the traces to WXO Agent Analytics.

```mermaid
flowchart LR
    Agent["Your agent (e.g. Claude)"] -->|"OTLP/HTTP push to /v1/traces"| Fwd["OTLP Trace Collector (forwarder)"]
    Fwd -->|"map to WXO agent ID + tenant"| Map["WXO_AGENT_MAP"]
    Fwd -->|"POST with IBM IAM token"| WXO["WXO Agent Analytics"]
```

The collector exposes these paths: `POST /v1/traces`, `POST /v1/traces/{agent_id}`, `POST /traces`, and `GET /health`.

### Part 1 — Deploy the collector

One config file, one command.

| File | What it holds |
|---|---|
| `config/shared.env` | `API_KEY`, `TOKEN_URL` (WXO credentials) |
| `config/forwarder.env` | `WXO_TENANT_ID`, `WXO_OTEL_EXPORT_URL`, `ENVIRONMENT_NAME`, `WXO_AGENT_MAP`, `FORWARDER_TARGET=aws\|azure\|docker` and target settings |

```bash
cp config/forwarder.env.example config/forwarder.env     # or use unified config.env
# edit the file, then:

./wire_agent.sh --forwarder --deploy            # create/update the forwarder AND push config
./wire_agent.sh --forwarder --deploy --claude   # deploy forwarder AND configure Claude telemetry destination
./wire_agent.sh --forwarder --sync              # later: push config only (after editing WXO_AGENT_MAP)
```

Add `--target aws|azure|docker` to override `FORWARDER_TARGET` for one run, and `--dry-run` to preview without changing anything.

| Target | `--deploy` does | `--sync` does | Config lives in |
|---|---|---|---|
| **aws** | Creates/updates the Lambda, IAM role and Function URL | Writes `/wxo-agent/settings` and `/wxo-agent/agent_map` in SSM; the Lambda picks it up within ~60 s | SSM SecureString |
| **azure** | Creates the Function App if missing (needs `AZURE_LOCATION` + `AZURE_STORAGE_ACCOUNT`), then publishes code | Updates App Settings (the app restarts) | App Settings |
| **docker** | Builds the image, starts the container on `OTLP_PORT` | Recreates the container with the new env | Container env |

When `--deploy` finishes it prints the collector endpoint and a health check result:

```
Forwarder endpoint : https://<your-collector-host>[/api]
Health check       : ok
```

Keep this URL — it is the **collector endpoint** you give to your agent in Part 2. Do not append `/v1/traces` yourself; OpenTelemetry exporters add it automatically.

### Part 2 — Configure your agent to send traces (example: Claude)

Once you have the collector endpoint (and, if you enabled it, the auth token), go to the Claude platform and set the collector as its OpenTelemetry (OTLP) destination. Other OTLP-capable agents and SDKs are configured the same way: in their own settings, set an OTLP/HTTP trace destination.

**Step 1 — Make sure the collector knows your agent.** Add an entry to `WXO_AGENT_MAP` in `config/forwarder.env` whose key is the name you will use for the agent (here `claude`), then push it with `./wire_agent.sh --forwarder --sync`:

```bash
WXO_AGENT_MAP='{"claude": {"wxo_agent_id": "<WXO agent UUID>", "tenant_id": "<WXO tenant>", "export_url": "<WXO export URL>"}}'
```

See `config/forwarder.env.example` for the exact format.

**Step 2 — Set the collector as your OTel destination in the Claude platform.** Open the Claude platform's OpenTelemetry / telemetry export settings and enter:

| Claude setting | Value |
|---|---|
| Destination / exporter type | OTLP over HTTP (traces) |
| Endpoint | The collector endpoint printed by `--deploy` |
| Protocol | `http/protobuf` (`http/json` also works) |
| Headers (only if `FORWARDER_AUTH_TOKEN` is set on the collector) | `Authorization: Bearer <FORWARDER_AUTH_TOKEN>` |
| Service name / agent identifier | `claude` (must match the `WXO_AGENT_MAP` key from Step 1) |

Save the settings. The exact labels depend on the Claude platform UI; if it asks for a full traces URL rather than a base endpoint, use `<collector endpoint>/v1/traces`.

**Step 3 — Verify.**

```bash
curl "<collector endpoint>/health"           # collector is up
./wire_agent.sh --forwarder --test           # send a synthetic trace through to WXO
```

Then use Claude to run a prompt and open WXO Agent Analytics to look for the agent mapped to `claude`. Traces appear after the exporter's next flush (a few seconds to a minute).

**If the Claude settings have no service-name field**, identify the agent through the URL instead: set the endpoint to `<collector endpoint>/v1/traces/claude`, or send the header `x-wxo-agent-id: claude`. The full lookup order is in [Agent Identity Resolution Order](#agent-identity-resolution-order).

**Automatic Claude (Anthropic) Telemetry Setup with `--claude`:**

You can deploy the forwarder and configure Claude's organization-wide OTel destination in a single step using the Anthropic admin API:

```bash
export ANTHROPIC_ADMIN_API_KEY=sk-ant-admin...      # keep in shell, not in tracked files
# In config.env / forwarder.env: ANTHROPIC_WORKSPACE_ID="wrkspc_..."  (or "all")

# Deploy forwarder and configure Claude telemetry destination in one command:
./wire_agent.sh --forwarder --deploy --claude

# Preview without applying changes:
./wire_agent.sh --forwarder --deploy --claude --dry-run
```

#### Content Mode (Full Payload vs. Metadata Only)
Anthropic allows choosing what content is exported:
- **`content` (default)**: Exports full prompts, completion text, and tool input/output.
- **`metadata_only`**: Exports trace timestamps, tokens, latencies, and metadata without text payloads.

You can select the mode via CLI flag or config:
```bash
# Via CLI flags:
./wire_agent.sh --forwarder --deploy --claude --full-content     # full payload (default)
./wire_agent.sh --forwarder --deploy --claude --metadata-only    # metadata only

# Or set in config.env:
ANTHROPIC_CONTENT_MODE="content"   # or "metadata_only"
```

The destination appears in the Anthropic Console as `watsonx Orchestrate - Agent Analytics (OTLP forwarder)`. Set `ANTHROPIC_DESTINATION_NAME` to use another name. If `FORWARDER_AUTH_TOKEN` is set, it is sent as `Authorization: Bearer <token>`.

### Notes
- **`--sync` replaces the stored agent map** with exactly what is in `forwarder.env`. Delete an entry there, `--sync`, and it is gone from the forwarder.
- **Auth is optional and off by default.** Set `FORWARDER_AUTH_TOKEN` in `forwarder.env` to require `Authorization: Bearer <token>` (or `x-api-key`) on every target; leave it unset and nothing is checked. AWS `--sync` writes it to SSM; `./src/forwarder/forwarder_ctl.py auth show|rotate|disable` also works.
- **AWS credentials:** use your AWS CLI profile/login; avoid putting keys in `forwarder.env`.
- **Run locally without deploying** (no container): `./wire_agent.sh --forwarder`, `--test`, `--dry-run`, `--port 8080`.
- **Docker beyond localhost:** the image built by `--deploy` (`DOCKER_IMAGE_NAME`) can be pushed and run on ECS, Cloud Run, Kubernetes or a VM with the same env vars.

### Centralized Agent Mapping Storage Options

The default per target is above. The forwarder can also read the map from a database or a URL, so many forwarders share one source of truth.

#### Option 1: AWS SSM Parameter Store (default for the `aws` target)
`--sync` manages `/wxo-agent/agent_map` for you. Manual access:
```bash
aws ssm get-parameter --name /wxo-agent/agent_map --with-decryption --query Parameter.Value --output text
```
Forwarders cache SSM values and refresh every 60 seconds.

#### Option 2: Database Table (AWS DynamoDB & PostgreSQL)

We provide automated management scripts to create tables, seed mappings, and perform updates:

##### A. AWS DynamoDB Table Setup & Management
```bash
# 1. One-click setup (creates table 'wxo_agent_mappings' and seeds from config)
./src/forwarder/setup_db.sh --dynamodb

# 2. Add or update an agent mapping
python3 src/forwarder/manage_db.py --dynamodb put \
  --agent "customer-support-bot" \
  --wxo-agent-id "61e315a1-0749-49fc-9972-fd92068fd881" \
  --tenant-id "tenant-support-prod" \
  --export-url "https://api.watson-orchestrate.ibm.com/instances/instance-1/v1/orchestrate/inject/traces" \
  --display-name "Customer Support Agent" \
  --environment "live"

# 3. List all mapped agents
python3 src/forwarder/manage_db.py --dynamodb list

# Configure forwarder environment:
export DYNAMODB_TABLE_NAME="wxo_agent_mappings"
```

##### B. PostgreSQL Database Setup & Management
```bash
# 1. One-click setup (creates table 'wxo_agent_mappings' and seeds from config)
./src/forwarder/setup_db.sh --postgres "postgresql://user:password@localhost:5432/wxo"

# 2. Add or update an agent mapping
python3 src/forwarder/manage_db.py --postgres --db-url "postgresql://user:password@localhost:5432/wxo" put \
  --agent "sales-lead-qualifier" \
  --wxo-agent-id "8a9b0c1d-1122-3344-5566-778899aabbcc" \
  --tenant-id "tenant-sales-prod" \
  --export-url "https://api.watson-orchestrate.ibm.com/instances/instance-2/v1/orchestrate/inject/traces" \
  --display-name "Sales Lead Qualifier"

# 3. List all mapped agents
python3 src/forwarder/manage_db.py --postgres --db-url "postgresql://user:password@localhost:5432/wxo" list

# Configure forwarder environment:
export POSTGRES_URL="postgresql://user:password@localhost:5432/wxo"
```

---

### Agent Identity Resolution Order
When an OTLP trace payload arrives at `/v1/traces`, `/v1/traces/{agent_id}`, or `/traces`, the forwarder resolves the target WXO Agent and Tenant in this priority order:
1. **URL Path Parameter:** `POST /v1/traces/<agent_id>`
2. **Query Parameter:** `?wxo_agent_id=...` or `?agent_id=...`
3. **HTTP Header:** `x-wxo-agent-id`, `x-agent-id`, or `x-ibm-agent-id`
4. **Direct UUID Match:** If an attribute or key is directly a valid UUID
5. **WXO_AGENT_MAP Exact Key Match:** Matching `agent.id`, `gen_ai.agent.id`, `service.name`, `agent.name`, or model name
6. **WXO_AGENT_MAP Default:** The `"default"` key in `WXO_AGENT_MAP`
7. **First Map Entry / Global Fallback:** Fallback to global `WXO_TENANT_ID` and `WXO_OTEL_EXPORT_URL`

---

## The sitecustomize.py hook

`src/shared/sitecustomize.py` is the **single canonical source** for the shared trace hook.
The `src/container_hook/Dockerfile` copies it directly from `src/shared/` — there are no separate platform copies to keep in sync.

It patches all major Python HTTP / RPC frameworks automatically at Python startup — no changes to agent code required:

| Framework | Mechanism | Notes |
|---|---|---|
| `http.server` | `BaseHTTPRequestHandler.do_POST` patch | stdlib, works with wsgiref dev server |
| **Starlette / FastAPI** | `BaseHTTPMiddleware` injected at app init | ASGI; FastAPI subclasses Starlette transparently |
| **Flask** | `Flask.wsgi_app` wrapper | works with any WSGI server (Gunicorn, uWSGI, waitress) |
| **Django** | `WSGIHandler.__call__` patch | covers `runserver`, Gunicorn, uWSGI, mod_wsgi |
| **Generic WSGI** | module-level `app`/`application` `__call__` wrap | fallback for Falcon, Bottle, Pyramid, raw wsgiref |
| **Tornado** | `tornado.web.Application.__call__` wrap | captures response via `connection.finish` hook |
| **aiohttp** | `aiohttp.web.Application` middleware injection | prepended at `__init__`; deferred GC walk handles pre-existing apps |
| **gRPC** | `grpc.ServerInterceptor` injected into `grpc.server()` | all four call types: unary-unary, server-streaming, client-streaming, bidi-streaming |

All config comes from env vars — no hardcoded values.

A deferred background thread re-runs all patches every 2 s for 30 s after startup, catching apps constructed after `sitecustomize.py` first runs (e.g. lazy app creation, hot reload).

Thread-local guards (`_IN_TRACE_POST`, `_IN_REQUEST_TRACE`) prevent the hook from tracing its own outbound OTLP POST or double-posting when multiple framework layers are stacked.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No traces in WXO | Missing env vars | Check `WXO_TENANT_ID`, `API_KEY`, `WXO_OTEL_EXPORT_URL` in your platform config |
| `WXO_OTEL_EXPORT_URL not set` | Config not sourced | Confirm `config/shared.env` + platform env are loaded |
| AWS: No trace after Harness update | ADOT not sending | Check Lambda logs: `aws logs tail /aws/lambda/wxo-agentcore-trace-hook --follow` |
| AWS: Runtime image not patched | Docker not running | Start Docker Desktop, re-run `--runtime` |
| Azure: Function App not publishing | `func` CLI missing | `brew install azure-functions-core-tools@4` |
| Azure: Hosted agent version stuck | Provisioning delay | Poll output shows status — wait up to 10 min |
| Vertex: Reasoning Engine not updating | SDK not installed | `pip install "google-cloud-aiplatform[agent_engines]>=1.60.0" cloudpickle` |
| Vertex: selfhosted — no hook logs | Image not pushed | Re-run `--selfhosted` to rebuild and push |

---

## Security

- **Never commit real credentials.** `config/shared.env`, `config/aws.env`, `config/azure.env`, and `config/vertex.env` are all in `.gitignore`.
- AWS: credentials are stored in SSM Parameter Store (SecureString).
- Azure: credentials are stored as Function App Settings / agent version env vars.
- Vertex: credentials are injected as Cloud Run / Reasoning Engine env vars — use GCP Secret Manager for production.
