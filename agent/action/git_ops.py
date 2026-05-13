"""Git + GitHub operations for automated patch PRs.

Uses the system `git` and `gh` CLI — no extra Python dependencies.
Prerequisite: `gh auth login` must have been run once.

All operations work on the project root (parent of agent/).
"""
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import config
from logging_setup import get_logger

_log = get_logger("git-ops")


@dataclass
class PRResult:
    branch: str
    commit_sha: str
    pr_url: str
    title: str


class GitOps:
    def __init__(self):
        # Project root = parent of the agent/ directory (where buggy-app/ lives)
        self._root = Path(config.APP_SOURCE_DIR).resolve().parent

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            cwd=str(self._root),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"command failed: {' '.join(args)}\n"
                f"stdout: {result.stdout.strip()}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _current_branch(self) -> str:
        return self._run("git", "rev-parse", "--abbrev-ref", "HEAD")

    # ── Public API ──────────────────────────────────────────────────────────────

    def gh_authenticated(self) -> bool:
        """Return True if gh CLI is authenticated and can create PRs."""
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            cwd=str(self._root),
        )
        return result.returncode == 0

    def create_branch(self, incident_id: str) -> str:
        """Create and checkout a new branch for the patch.
        Returns the branch name.
        """
        # Sanitise incident_id for a valid branch name
        safe_id = incident_id.replace(":", "-").replace("/", "-")[:30]
        branch = f"{config.PATCH_BRANCH_PREFIX}/{safe_id}"

        # Ensure we start from the default branch.
        # Stash local changes first so git pull doesn't fail on modified tracked files.
        default = config.DEFAULT_BRANCH
        try:
            stash_out = self._run("git", "stash", "--include-untracked", "-m", "agent-pre-patch-stash")
            _log.debug(f"git stash: {stash_out}")
        except Exception as e:
            _log.debug(f"git stash skipped (nothing to stash or error): {e}")

        self._run("git", "fetch", "origin", default)
        self._run("git", "checkout", default)
        self._run("git", "pull", "--ff-only", "origin", default)
        self._run("git", "checkout", "-b", branch)

        _log.info("branch created", extra={"branch": branch})
        return branch

    def commit_patch(
        self,
        relative_path: str,
        content: str,
        commit_msg: str,
        old_code: str = "",
        new_code: str = "",
    ) -> str:
        """Write patched content to disk and commit it.

        If old_code/new_code are provided (replace_in_file path), re-apply the
        replacement against the HEAD version of the file so the PR diff is minimal
        (just the fix, not any uncommitted working-tree changes).
        Falls back to writing `content` directly when old_code is absent (propose_patch path).

        Returns short commit SHA.
        """
        full_path = self._root / relative_path

        if old_code and new_code:
            try:
                head_content = full_path.read_text()
                if old_code in head_content:
                    content = head_content.replace(old_code, new_code, 1)
                    _log.debug("re-applied patch against HEAD file — PR will be minimal")
                else:
                    _log.warning(
                        "old_code not found in HEAD file; falling back to working-tree content",
                        extra={"file": relative_path},
                    )
            except Exception as e:
                _log.warning(f"could not re-apply against HEAD: {e}; falling back to patched content")

        full_path.write_text(content)
        self._run("git", "add", str(full_path))
        self._run("git", "commit", "-m", commit_msg)
        sha = self._run("git", "rev-parse", "--short", "HEAD")
        _log.info("patch committed", extra={"file": relative_path, "sha": sha})
        return sha

    def push_branch(self, branch: str) -> None:
        """Push the branch to origin."""
        self._run("git", "push", "origin", branch)
        _log.info("branch pushed", extra={"branch": branch})

    def open_pr(
        self,
        branch: str,
        title: str,
        body: str,
    ) -> str:
        """Open a GitHub PR and return its URL."""
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body",  body,
                "--head",  branch,
                "--base",  config.DEFAULT_BRANCH,
            ],
            capture_output=True,
            text=True,
            cwd=str(self._root),
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh pr create failed:\n{result.stderr.strip()}")

        pr_url = result.stdout.strip()
        _log.info("PR opened", extra={"pr_url": pr_url, "branch": branch})
        return pr_url

    def restore_default_branch(self) -> None:
        """Switch back to the default branch after patch operations."""
        try:
            self._run("git", "checkout", config.DEFAULT_BRANCH)
        except Exception as e:
            _log.warning(f"could not restore default branch: {e}")

    def create_patch_pr(
        self,
        incident_id: str,
        file_path: str,
        patched_content: str,
        description: str,
        root_cause: str,
        confidence: float,
        restore_branch: bool = True,
        old_code: str = "",
        new_code: str = "",
    ) -> PRResult:
        """Branch → commit → push → PR.

        restore_branch=True  (default): checkout the default branch before returning.
        restore_branch=False: stay on the patch branch so the caller can build the
                              patched image before switching back.  Caller MUST call
                              restore_default_branch() when done.
        """
        branch = ""
        try:
            branch = self.create_branch(incident_id)

            relative_file = str(Path(file_path))
            sha = self.commit_patch(
                relative_file,
                patched_content,
                old_code=old_code,
                new_code=new_code,
                commit_msg=(
                    f"fix: {description}\n\n"
                    f"Auto-generated by devops-agent.\n"
                    f"Incident: {incident_id}\n"
                    f"Root cause: {root_cause}\n"
                    f"Confidence: {confidence:.0%}\n\n"
                    f"Co-Authored-By: DevOps Agent <agent@devops-agent.local>"
                ),
            )

            self.push_branch(branch)

            pr_body = (
                f"## Summary\n\n"
                f"Auto-generated patch by the self-healing DevOps agent.\n\n"
                f"- **Incident:** `{incident_id}`\n"
                f"- **Root cause:** {root_cause}\n"
                f"- **Confidence:** {confidence:.0%}\n"
                f"- **Fix:** {description}\n\n"
                f"## Changed files\n\n"
                f"- `{file_path}`\n\n"
                f"## Test plan\n\n"
                f"- [ ] SLO error rate returns to < 1% after deploy\n"
                f"- [ ] Latency P99 remains < 200ms\n"
                f"- [ ] All health checks pass\n"
                f"- [ ] No new pod restarts within 5 minutes\n\n"
                f"🤖 Generated by [devops-agent](https://github.com/rohandave1988/agentic_devops_project)"
            )

            pr_url = self.open_pr(
                branch=branch,
                title=f"fix({Path(file_path).stem}): {description[:60]}",
                body=pr_body,
            )

            return PRResult(
                branch=branch,
                commit_sha=sha,
                pr_url=pr_url,
                title=description,
            )

        except Exception:
            # Always restore the branch on error so the repo isn't left on a dead branch
            self.restore_default_branch()
            raise

        finally:
            if restore_branch:
                self.restore_default_branch()
