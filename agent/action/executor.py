import logging
from dataclasses import dataclass, field
from datetime import datetime

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.exceptions import ApiException

import config
from planning.decision import ActionPlan

logger = logging.getLogger(__name__)


@dataclass
class Result:
    action: str
    status: str   # "success" | "error" | "dry_run"
    detail: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class Executor:
    def __init__(self):
        try:
            k8s_config.load_incluster_config()
            logger.info("using in-cluster kubeconfig")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(context=config.KUBE_CONTEXT)
            logger.info("using local kubeconfig")

        self._v1      = k8s_client.CoreV1Api()
        self._apps_v1 = k8s_client.AppsV1Api()
        self._ns      = config.TARGET_NAMESPACE
        self._deploy  = config.TARGET_DEPLOYMENT

    def execute(self, plan: ActionPlan) -> Result:
        logger.info(f"executing action: {plan.action}", extra={"params": plan.params})

        if config.DRY_RUN:
            return Result(action=plan.action, status="dry_run", detail="dry run — no changes made")

        try:
            if plan.action == "restart_pods":
                detail = self._restart_pods()
            elif plan.action in ("scale_up", "scale_down"):
                detail = self._scale_deployment(plan.params)
            elif plan.action == "rollback":
                detail = self._rollback_deployment()
            elif plan.action == "no_action":
                return Result(action="no_action", status="success", detail="no action required")
            else:
                return Result(action=plan.action, status="error", detail=f"unknown action: {plan.action}")

            logger.info(f"action completed: {plan.action} — {detail}")
            return Result(action=plan.action, status="success", detail=detail)

        except (ApiException, ValueError, RuntimeError) as e:
            logger.error(f"action failed: {plan.action} — {e}")
            return Result(action=plan.action, status="error", detail=str(e))

    def _restart_pods(self) -> str:
        pods = self._v1.list_namespaced_pod(self._ns, label_selector=f"app={self._deploy}")
        deleted = []
        for pod in pods.items:
            try:
                self._v1.delete_namespaced_pod(pod.metadata.name, self._ns)
                deleted.append(pod.metadata.name)
                logger.info(f"pod deleted: {pod.metadata.name}")
            except ApiException as e:
                logger.warning(f"failed to delete pod {pod.metadata.name}: {e}")
        return f"restarted {len(deleted)} pods: {deleted}"

    def _scale_deployment(self, params: dict) -> str:
        target = int(params["replicas"])
        dep = self._apps_v1.read_namespaced_deployment(self._deploy, self._ns)
        before = dep.spec.replicas
        dep.spec.replicas = target
        self._apps_v1.patch_namespaced_deployment(self._deploy, self._ns, dep)
        return f"scaled {self._deploy}: {before} → {target}"

    def _rollback_deployment(self) -> str:
        dep = self._apps_v1.read_namespaced_deployment(self._deploy, self._ns)
        annotations = dep.metadata.annotations or {}
        current_rev = int(annotations.get("deployment.kubernetes.io/revision", "1"))
        if current_rev <= 1:
            raise ValueError(f"no previous revision to roll back to (current: {current_rev})")

        target_rev = str(current_rev - 1)
        rs_list = self._apps_v1.list_namespaced_replica_set(self._ns)

        prev_rs = None
        for rs in rs_list.items:
            rs_annotations = rs.metadata.annotations or {}
            if rs_annotations.get("deployment.kubernetes.io/revision") != target_rev:
                continue
            for ref in rs.metadata.owner_references or []:
                if ref.kind == "Deployment" and ref.name == self._deploy:
                    prev_rs = rs
                    break
            if prev_rs:
                break

        if prev_rs is None:
            raise RuntimeError(f"ReplicaSet for revision {target_rev} not found in history")

        dep.spec.template = prev_rs.spec.template
        self._apps_v1.patch_namespaced_deployment(self._deploy, self._ns, dep)
        return f"rolled back {self._deploy}: revision {current_rev} → {current_rev - 1}"
