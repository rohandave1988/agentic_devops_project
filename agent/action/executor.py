from dataclasses import dataclass, field
from datetime import datetime

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.exceptions import ApiException

import config
from planning.decision import ActionPlan
from tracing.spans import agent_span, set_span_attrs
from logging_setup import get_logger

logger = get_logger("build-deploy")

# Lazy imports for patch pipeline — only loaded when patch_code action is triggered
def _get_patch_components():
    from agents.specialists.code_patch import CodePatchAgent
    from action.git_ops import GitOps
    from action.build_deploy import build_and_deploy
    return CodePatchAgent, GitOps, build_and_deploy


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

        with agent_span(
            "agent.act",
            **{
                "action.type":   plan.action,
                "action.dry_run": config.DRY_RUN,
                "action.params": str(plan.params),
            },
        ) as sp:
            if config.DRY_RUN:
                sp.set_attribute("action.result", "dry_run")
                return Result(action=plan.action, status="dry_run", detail="dry run — no changes made")

            try:
                if plan.action == "restart_pods":
                    detail = self._restart_pods()
                elif plan.action in ("scale_up", "scale_down"):
                    detail = self._scale_deployment(plan.params)
                elif plan.action == "rollback":
                    detail = self._rollback_deployment()
                elif plan.action == "patch_code":
                    detail = self._patch_code(plan.params)
                elif plan.action == "no_action":
                    sp.set_attribute("action.result", "no_action")
                    return Result(action="no_action", status="success", detail="no action required")
                else:
                    sp.set_attribute("action.result", "error")
                    return Result(action=plan.action, status="error", detail=f"unknown action: {plan.action}")

                set_span_attrs(sp, **{"action.result": "success", "action.detail": detail[:200]})
                logger.info(f"action completed: {plan.action} — {detail}")
                return Result(action=plan.action, status="success", detail=detail)

            except (ApiException, ValueError, RuntimeError) as e:
                sp.set_attribute("action.result", "error")
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

    def _patch_code(self, params: dict) -> str:
        """Generate a code patch, commit it, open a PR, and optionally deploy."""
        CodePatchAgent, GitOps, build_and_deploy = _get_patch_components()

        root_cause    = params.get("root_cause", "unknown root cause")
        severity      = params.get("severity", "high")
        confidence    = float(params.get("confidence", 0.75))
        recent_logs   = params.get("recent_logs", "")
        actions_tried = params.get("actions_tried", [])
        incident_id   = params.get("incident_id", "")

        # 1. Generate patch via CodePatchAgent's own ReAct loop
        logger.info(f"running CodePatchAgent — root_cause: {root_cause[:100]}", extra={"severity": severity, "confidence": round(confidence, 2)})
        agent   = CodePatchAgent()
        proposal = agent.generate(
            root_cause=root_cause,
            severity=severity,
            confidence=confidence,
            recent_logs=recent_logs,
            actions_tried=actions_tried,
            incident_id=incident_id,
        )

        if not proposal:
            return "patch_code: CodePatchAgent could not generate a fix — manual investigation required"

        logger.info(
            f"patch generated — {proposal.file_path} | {proposal.description[:80]}",
            extra={"file": proposal.file_path, "confidence": round(proposal.confidence, 2)},
        )

        # 2. Human diff review (if HUMAN_IN_LOOP — second gate before any git ops)
        if config.HUMAN_IN_LOOP:
            from hitl.review import review_patch
            from pathlib import Path
            try:
                original = (Path(config.APP_SOURCE_DIR) / proposal.file_path).read_text()
            except Exception:
                original = ""
            approved = review_patch(
                file_path=proposal.file_path,
                original=original,
                patched=proposal.patched_content,
                description=proposal.description,
                confidence=proposal.confidence,
                timeout_sec=config.HUMAN_REVIEW_TIMEOUT,
            )
            if not approved:
                return "patch_code: patch rejected by operator — no changes made"

        # 3. Commit + open PR
        git = GitOps()

        if not git.gh_authenticated():
            logger.warning("gh CLI not authenticated — skipping PR creation")
            # Still write the patch locally so it's not lost
            from pathlib import Path
            local_path = Path(config.APP_SOURCE_DIR) / proposal.file_path
            local_path.write_text(proposal.patched_content)
            return (
                f"patch_code: patch written to {local_path} but PR skipped "
                f"(run 'gh auth login' to enable PR creation)"
            )

        # When AUTO_DEPLOY_PATCH is on we must build BEFORE switching back to main,
        # so stay on the patch branch until docker build finishes.
        stay_on_patch = config.AUTO_DEPLOY_PATCH

        pr = git.create_patch_pr(
            incident_id=incident_id,
            file_path=f"buggy-app/{proposal.file_path}",
            patched_content=proposal.patched_content,
            description=proposal.description,
            root_cause=root_cause,
            confidence=proposal.confidence,
            restore_branch=not stay_on_patch,   # defer branch restore when deploying
            old_code=proposal.old_code,
            new_code=proposal.new_code,
        )
        logger.info(f"PR opened → {pr.pr_url}", extra={"pr_url": pr.pr_url, "branch": pr.branch, "commit": pr.commit_sha[:8]})

        # 3. Auto-deploy if enabled
        # Working tree is still on the patch branch here (stay_on_patch=True).
        if config.AUTO_DEPLOY_PATCH:
            logger.info("AUTO_DEPLOY_PATCH=true — building and deploying patch from branch")
            try:
                deploy_result = build_and_deploy(incident_id)
            finally:
                # Always restore main even if build fails
                git.restore_default_branch()

            if deploy_result.success:
                return (
                    f"patch_code: PR opened ({pr.pr_url}), "
                    f"deployed {deploy_result.image_tag} — {deploy_result.detail}"
                )
            else:
                return (
                    f"patch_code: PR opened ({pr.pr_url}) but deploy failed — {deploy_result.detail}"
                )

        logger.warning(
            "cluster still degraded — patch PR opened but not deployed; "
            "set AUTO_DEPLOY_PATCH=true to rebuild and redeploy automatically",
            extra={"pr_url": pr.pr_url, "branch": pr.branch},
        )
        return (
            f"patch_code: PR opened at {pr.pr_url} "
            f"(branch: {pr.branch}, commit: {pr.commit_sha[:8]}) — "
            f"CLUSTER STILL DEGRADED until PR is merged and redeployed. "
            f"Set AUTO_DEPLOY_PATCH=true to build + deploy automatically."
        )

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
