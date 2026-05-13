"""Build and deploy a patched application image.

Pipeline:
  1. docker build  — builds a new image from updated source
  2. kind load     — loads image into the cluster (local dev only)
  3. kubectl set image  — updates the Deployment
  4. kubectl rollout status  — waits for rollout to finish

Only runs when AUTO_DEPLOY_PATCH=true in config.
"""
import subprocess
import time
from dataclasses import dataclass

import config
from logging_setup import get_logger
from tracing.spans import agent_span

_log = get_logger("build-deploy")


@dataclass
class DeployResult:
    success: bool
    image_tag: str
    detail: str


def build_and_deploy(incident_id: str) -> DeployResult:
    """Build a new image from APP_SOURCE_DIR and roll it out.

    Uses a timestamped tag so the Deployment detects a real change.
    Returns DeployResult with success flag and detail string.
    """
    tag = f"{config.TARGET_DEPLOYMENT}:agent-patch-{int(time.time())}"

    with agent_span("build_deploy", **{"incident.id": incident_id, "image.tag": tag}):
        try:
            _log.info("building Docker image", extra={"tag": tag})
            _run(
                "docker", "build",
                "-t", tag,
                config.APP_SOURCE_DIR,
            )

            _log.info("loading image into kind cluster", extra={"tag": tag, "cluster": config.KIND_CLUSTER_NAME})
            _run(
                "kind", "load", "docker-image", tag,
                "--name", config.KIND_CLUSTER_NAME,
            )

            _log.info("updating Deployment image", extra={"tag": tag})
            _run(
                "kubectl", "set", "image",
                f"deployment/{config.TARGET_DEPLOYMENT}",
                f"{config.TARGET_DEPLOYMENT}={tag}",
                "-n", config.TARGET_NAMESPACE,
            )

            _log.info("waiting for rollout")
            _run(
                "kubectl", "rollout", "status",
                f"deployment/{config.TARGET_DEPLOYMENT}",
                "-n", config.TARGET_NAMESPACE,
                "--timeout=120s",
            )

            detail = f"deployed {tag} to {config.TARGET_NAMESPACE}/{config.TARGET_DEPLOYMENT}"
            _log.info("rollout complete", extra={"tag": tag})
            return DeployResult(success=True, image_tag=tag, detail=detail)

        except RuntimeError as e:
            _log.error(f"build/deploy failed: {e}")
            return DeployResult(success=False, image_tag=tag, detail=str(e))


def _run(*args: str) -> str:
    result = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()
