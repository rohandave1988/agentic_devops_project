# Setup Guide — Agentic DevOps Self-Healing System

## Prerequisites (install once)

```bash
brew install kind kubectl helm python
brew install --cask docker        # start Docker Desktop after install
```

For local LLM (optional):
```bash
brew install ollama
ollama pull llama3                # ~4GB download
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
cp .env.example .env
# Edit .env and set your ANTHROPIC_API_KEY
# Or set LLM_BACKEND=ollama to use local llama3
```

---

## Step 3 — Start the agent

```bash
cd agent
source .venv/bin/activate
python agent.py
```

You'll see the Rich dashboard update every 30 seconds:

```
══════════════════════════════════════
STEP: Agent Cycle #1
══════════════════════════════════════
 Metric              Value      SLO         Status
 Error Rate          0.00%      <1%         OK
 P99 Latency (ms)    45ms       <200ms      OK
 CPU Usage           12%        <80%        OK
 Memory Usage        34%        <85%        OK
 Pod Restarts (5m)   0          ≤3          OK
 Ready Replicas      2/2        =desired    OK
```

---

## Step 4 — Open dashboards

| Service    | URL                        | Credentials      |
|------------|----------------------------|------------------|
| Grafana    | http://localhost:30300     | admin / admin123 |
| Prometheus | http://localhost:30090     | —                |
| buggy-app  | http://localhost:30080     | —                |

Import `dashboards/slo-dashboard.json` into Grafana for the SLO view.

---

## Step 5 — Run demo scenarios

Open a second terminal:

```bash
# Scenario 1: High Error Rate
./scripts/demo.sh error-rate
# Agent detects error_rate > 1% SLO → LLM diagnoses → restart_pods executed

# Scenario 2: Pod Crash Loop
./scripts/demo.sh pod-crash
# Pods begin OOMKilling/restarting → Agent detects restart_count spike → restart + scale_up

# Scenario 3: CPU Spike
./scripts/demo.sh high-cpu
# CPU > 80% → Agent detects → LLM recommends scale_up → replicas increased

# Reset everything
./scripts/demo.sh reset
./scripts/demo.sh reset-probe   # if you ran pod-crash
```

---

## Architecture at a glance

```
kind cluster
  ├── demo/buggy-app          ← fault-injectable Node.js app
  └── monitoring/
       ├── prometheus          ← scrapes /metrics every 15s
       ├── grafana             ← dashboards
       ├── loki                ← log aggregation
       └── promtail            ← ships pod logs to Loki

agent/ (runs locally, outside cluster)
  ├── perception/             ← pulls metrics + logs
  ├── reasoning/              ← LLM root cause analysis
  ├── planning/               ← safety-checked action selection
  ├── action/                 ← kubectl / k8s API execution
  └── memory/                 ← incident history (JSON or Redis)
```

---

## Switching to local LLM (Ollama)

```bash
# Ensure Ollama is running
ollama serve &
ollama pull llama3

# Set env
export LLM_BACKEND=ollama
export OLLAMA_MODEL=llama3
python agent.py
```

---

## Dry-run mode (observe without acting)

```bash
DRY_RUN=true python agent.py
```

The agent will detect issues and log what action it *would* take, without executing anything.

---

## Teardown

```bash
kind delete cluster --name devops-agent
```
