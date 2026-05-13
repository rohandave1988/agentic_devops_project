# Runbook: Ready Replicas Below Desired Count

## Symptoms
- `kube_deployment_status_replicas_ready < kube_deployment_spec_replicas`
- Some pods in Pending, ContainerCreating, or CrashLoopBackOff state
- Effective capacity reduced — remaining pods handle more load

## Common Causes

### 1. Pods failing to start (image pull error, bad config)
**Signal:** Pod status shows ImagePullBackOff or ErrImagePull.
**Action:** `rollback` to known-good image tag.

### 2. Node resource exhaustion (no room to schedule)
**Signal:** Pods stuck in Pending with "Insufficient CPU/memory" in events.
**Action:** `no_action` — node-level issue, not app-level. Scale down or add nodes.

### 3. Crash loop reducing ready count
**Signal:** Pods exist but restart count is high; status cycles.
**Action:** `restart_pods` to break the loop; `rollback` if post-deploy.

### 4. Deployment rollout in progress
**Signal:** Ready count temporarily low but climbing; no errors in running pods.
**Action:** `no_action` — wait for rollout to complete (usually 30–60s).

### 5. Scale-down recently executed
**Signal:** Desired count was just reduced by the agent; ready = new desired.
**Action:** `no_action` — this is expected.

## Investigation Steps
1. Check pod states: which pods are not ready and why?
2. Check recent deployment events for rollout progress.
3. Check node capacity — are pods pending due to resource pressure?
4. Check if desired count was intentionally changed.

## Remediation Priority
| Scenario | Action | Notes |
|---|---|---|
| Pods in CrashLoopBackOff | restart_pods | Break crash loop |
| Bad image after deploy | rollback | Restore previous image |
| Pending pods (node pressure) | no_action | Infrastructure problem |
| Rolling rollout | no_action | Wait for convergence |

## Recovery Verification
SLO restored when: `ready_replicas == desired_replicas` for 2+ consecutive checks

## Escalation
Escalate if ready_replicas reaches 0 (complete outage). If pods can't start after rollback, escalate to node/cluster health check.
