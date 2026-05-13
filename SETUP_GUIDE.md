# Setup Guide — Agentic DevOps Self-Healing System

## Prerequisites (install once)

```bash
brew install kind kubectl helm python
brew install --cask docker        # start Docker Desktop after install
```

For local LLM (optional):
```bash
brew install ollama
ollama pull qwen2.5:7b            # ~4GB download (recommended)
# or: ollama pull mistral-nemo
```

---

## Step 1 — Run the bootstrap script

```bash
cd agentic_devops_project
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This will:
- Create a 3-node kind cluster (`devops-agent`)
- Build and load the buggy-app Docker image
- Deploy buggy-app to the `demo` namespace
- Install Prometheus + Grafana via Helm
- Install Loki + Promtail via Helm
- Create the Python virtual environment

---

## Step 2 — Configure the agent

```bash
cd agent
# Edit .env — set your LLM backend and key
```

Key settings in `.env`:

```bash
# Option A — Anthropic Claude (recommended for best results)
LLM_BACKEND=claude
# Set ANTHROPIC_API_KEY as env var or in .env

# Option B — local Ollama (free, no API key needed)
LLM_BACKEND=ollama
OLLAMA_MODEL=qwen2.5:7b

# Human-in-the-loop (pause for approval before each action)
HUMAN_IN_LOOP=false
HUMAN_REVIEW_TIMEOUT_SEC=60

# Dry-run (investigate but don't execute)
DRY_RUN=false
```

---

## Step 3 — Start the agent

```bash
cd agent
python main.py
```

You'll see the terminal dashboard update every 10 seconds:

```
──────────────── Agent Cycle #1 ────────────────

  Metric                   Value          SLO            Status
  ──────────────────────────────────────────────────────────────────────
  5xx Error Rate           0.00%          <1%            OK
  4xx Error Rate           0.00%          <5%            OK
  P99 Latency (ms)         45ms           <200ms         OK
  P50 Latency (ms)         12ms           <50ms          OK
  CPU Usage                12.00%         <80%           OK
  CPU Throttle             0.0%           <25%           OK
  Memory Usage             34.00%         <85%           OK
  OOM Kills (5m)           0              =0             OK
  Active Requests          3              ≤50            OK
  Pod Restarts (5m)        0              ≤3             OK
  Ready Replicas           2/2            =desired       OK

all SLOs healthy — no LLM analysis needed
```

---

## Step 4 — Open dashboards

| Service    | URL                        | Credentials      |
|------------|----------------------------|------------------|
| Grafana    | http://localhost:30300     | admin / admin123 |
| Prometheus | http://localhost:30090     | —                |
| buggy-app  | http://localhost:30080     | —                |

Import dashboards:
```
Grafana → Dashboards → New → Import → Upload dashboards/slo-dashboard.json
Grafana → Dashboards → New → Import → Upload dashboards/agent-dashboard.json
```

---

## Step 5 — Run demo scenarios

Open a second terminal:

```bash
# Scenario 1: High Error Rate
curl -X POST http://localhost:30080/fault/errors
# Agent: detects 5xx > 1% → LogsAgent finds error pattern → restart_pods or rollback

# Scenario 2: CPU Spike
curl -X POST http://localhost:30080/fault/cpu
# Agent: detects CPU > 80% → MetricsAgent confirms → scale_up

# Scenario 3: Memory Leak
curl -X POST http://localhost:30080/fault/memory
# Agent: detects memory > 85% → restart_pods

# Reset everything back to healthy
curl -X POST http://localhost:30080/fault/reset
```

The agent detects SLO breaches within one poll cycle (~10s), runs the multi-agent investigation, takes action, and verifies recovery — all automatically.

---

## Step 6 — Optional: Langfuse LLM Tracing

Langfuse gives you visibility into every LLM call — what questions the orchestrator asked, what each specialist answered, with what confidence.

```bash
# Start Langfuse + Postgres
docker compose -f docker-compose.langfuse.yml up -d

# Open http://localhost:3000
# 1. Sign up (local only)
# 2. Create an organisation + project (name: devops-agent)
# 3. Settings → API Keys → Create new secret key
# 4. Copy the public key and secret key

# Add to agent/.env
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000

# Restart the agent — traces appear in Langfuse UI per incident
python main.py
```

---

## Step 7 — Optional: OpenTelemetry → Jaeger

Jaeger shows the full distributed trace per agent cycle — spans for each LLM call, tool execution, and Kubernetes API call.

```bash
# Start Jaeger
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one

# Add to agent/.env
OTLP_ENDPOINT=localhost:4317

# Restart the agent
python main.py

# View traces at http://localhost:16686
# Select service: devops-agent
```

---

## Step 8 — Optional: Decision Audit Trail

Inspect the reasoning chain for any incident directly in the terminal:

```python
# From the agent/ directory
python -c "
from tracing.decisions import show_chain, get_decision_log
incidents = get_decision_log().list_incidents(n=5)
for inc in incidents:
    print(inc['incident_id'], inc['outcome'])
"

# Show full chain for a specific incident
python -c "
from tracing.decisions import show_chain
show_chain('run-<paste-incident-id-here>')
"
```

---

## Dry-run mode

Investigate without acting:
```bash
DRY_RUN=true python main.py
```

The agent detects issues, runs the full multi-agent investigation, and logs what action it *would* take — without executing anything against Kubernetes.

---

## Switching LLM backends

```bash
# Use local Ollama (free, no API key)
LLM_BACKEND=ollama OLLAMA_MODEL=qwen2.5:7b python main.py

# Use Claude (best reasoning quality)
LLM_BACKEND=claude python main.py  # requires ANTHROPIC_API_KEY in env
```

---

## Run Agent Inside the Cluster

```bash
docker build -t devops-agent:latest agent/
kind load docker-image devops-agent:latest --name devops-agent

kubectl create secret generic agent-secrets \
  --from-literal=anthropic-api-key=$ANTHROPIC_API_KEY \
  -n agent-system

kubectl apply -f k8s/agent/rbac.yaml
kubectl apply -f k8s/agent/deployment.yaml
kubectl logs -f deployment/devops-agent -n agent-system
```

The agent runs with a minimal-privilege `ServiceAccount` — only the RBAC verbs it actually needs.

---

## Teardown

```bash
kind delete cluster --name devops-agent
docker compose -f docker-compose.langfuse.yml down -v   # if Langfuse was running
docker stop jaeger && docker rm jaeger                   # if Jaeger was running
```

---

## Architecture at a glance

```
kind cluster
  ├── demo/buggy-app          ← fault-injectable Python Flask app
  └── monitoring/
       ├── prometheus          ← scrapes /metrics every 15s
       ├── grafana             ← SLO + agent dashboards
       ├── loki                ← log aggregation
       └── promtail            ← ships pod logs to Loki

agent/ (runs locally, outside cluster)
  ├── agents/                 ← OrchestratorAgent + 3 specialists + memory
  ├── tracing/                ← OTel spans + DecisionLog + Langfuse
  ├── perception/             ← pulls metrics + logs
  ├── planning/               ← safety-checked action selection
  ├── action/                 ← kubernetes-python SDK execution
  └── memory/                 ← SQLite incident history

observability/ (optional, local Docker)
  ├── Langfuse :3000          ← LLM call traces per investigation
  └── Jaeger   :16686         ← distributed trace per agent cycle
```
