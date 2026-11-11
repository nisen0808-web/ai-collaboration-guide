"""Candidate text-only adapter for the official Grok CLI.

The user controls CLI installation, login, subscription and any charges.  This
adapter does not use the xAI HTTP API or read/copy API keys.  Real-client and
system-policy behavior has not yet been tested; see README before any release.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import uuid

if __package__:
    from .inspect_policy import INSPECTION_REASONS, inspect_report_diagnostics
else:
    from inspect_policy import INSPECTION_REASONS, inspect_report_diagnostics


_NETWORK_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                      "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR")
_ENV_NAMES = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR", *_NETWORK_ENV_NAMES)
_INSPECT_KEYS = ("hooks", "plugins", "mcpServers", "skills", "lspServers", "projectInstructions")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_MAX_TASK_BYTES = 100_000
_MAX_RESULT_BYTES = 256_000


class GrokAdapter:
    """In-process worker; job recovery after parent restart is unavailable."""

    def __init__(self, cli: str, model: str, effort: str, profile_dir: str,
                 timeout_seconds: int = 600, max_concurrency: int = 2):
        self.cli = Path(cli)
        self.profile = Path(profile_dir)
        if not self.cli.is_absolute() or not self.profile.is_absolute():
            raise ValueError("CLI and profile paths must be absolute")
        if not _MODEL.fullmatch(model or "") or effort not in _EFFORTS:
            raise ValueError("choose a valid model and effort")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 900:
            raise ValueError("timeout must be between 1 and 900 seconds")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 8:
            raise ValueError("local concurrency must be between 1 and 8")
        self.model, self.effort, self.timeout = model, effort, timeout_seconds
        # This is a local worker bound, never an assertion about an account's
        # official subscription concurrency, quota, or rate limits.
        self.max_concurrency = max_concurrency
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._task_keys: dict[str, tuple[str, str]] = {}
        self._active_job_ids: set[str] = set()

    def _env(self, isolated_home: Path) -> dict[str, str]:
        # Proxy variables may contain credentials: pass them without logging.
        # Never inspect or inherit API-key/config overrides.
        env = {k: v for k in _ENV_NAMES if isinstance((v := os.environ.get(k)), str) and "\0" not in v}
        env.update({"HOME": str(isolated_home), "USERPROFILE": str(isolated_home),
                    "GROK_HOME": str(self.profile), "TEMP": str(isolated_home),
                    "TMP": str(isolated_home), "GROK_DISABLE_AUTOUPDATER": "1",
                    "GROK_MEMORY": "0", "GROK_SUBAGENTS": "0", "GROK_GOAL": "0",
                    "GROK_WRITE_FILE": "0", "GROK_TOOL_SEARCH": "0", "GROK_WEB_FETCH": "0",
                    "GROK_LSP_TOOLS": "0", "GROK_CRASH_HANDLER": "0",
                    "GROK_REMEMBER_TOOL_APPROVALS": "0", "GROK_SANDBOX_AUTO_ALLOW_BASH": "0"})
        for family in ("CURSOR", "CLAUDE"):
            for feature in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
                env[f"GROK_{family}_{feature}_ENABLED"] = "0"
        return env

    def _paths_ready(self) -> bool:
        # Do not read the contents of the user's profile or credentials.
        try:
            attrs = self.profile.lstat()
            return (self.cli.is_file() and self.profile.is_dir() and not self.profile.is_symlink()
                    and not (getattr(attrs, "st_file_attributes", 0) & 0x400))
        except OSError:
            return False

    def login(self) -> int:
        """Run explicit interactive login; no passwords/tokens are captured."""
        if not self._paths_ready():
            raise ValueError("install official CLI and create a dedicated profile directory first")
        home = Path(tempfile.mkdtemp(prefix="grok-login-"))
        try:
            return subprocess.call([str(self.cli), "login"], cwd=str(home), env=self._env(home), shell=False)
        finally:
            try:
                home.rmdir()
            except OSError:
                pass  # Never recursively delete CLI-created data.

    def submit(self, task: str, task_id: str) -> dict:
        if not isinstance(task, str) or not task.strip():
            return {"status": "not_submitted", "reason": "invalid_task"}
        try:
            task_bytes = task.encode("utf-8")
        except UnicodeError:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if len(task_bytes) > _MAX_TASK_BYTES:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            return {"status": "not_submitted", "reason": "invalid_task_id"}
        if not self._paths_ready():
            return {"status": "not_submitted", "reason": "cli_or_profile_unavailable"}
        digest = hashlib.sha256(task_bytes).hexdigest()
        with self._lock:
            previous = self._task_keys.get(task_id)
            if previous:
                if previous[0] != digest:
                    return {"status": "not_submitted", "reason": "task_id_conflict"}
                existing_status = self._jobs[previous[1]]["status"]
                if existing_status in {"queued", "running", "submission_uncertain"}:
                    return {"status": existing_status, "job_id": previous[1], "replayed": True}
                # This invocation did not dispatch another task; the caller
                # can retrieve the existing terminal result with its job ID.
                return {"status": "not_submitted", "reason": "already_submitted",
                        "job_id": previous[1], "replayed": True}
            if len(self._active_job_ids) >= self.max_concurrency:
                return {"status": "not_submitted", "reason": "provider_busy"}
            job_id = str(uuid.uuid4())
            self._task_keys[task_id] = (digest, job_id)
            self._jobs[job_id] = {"status": "queued", "job_id": job_id}
            self._active_job_ids.add(job_id)
            worker = threading.Thread(target=self._execute, args=(job_id, task), daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._active_job_ids.discard(job_id)
                del self._task_keys[task_id]
                del self._jobs[job_id]
                return {"status": "not_submitted", "reason": "worker_unavailable"}
        return {"status": "queued", "job_id": job_id}

    def poll(self, job_id: str) -> dict:
        with self._lock:
            value = self._jobs.get(job_id)
            # A missing in-memory job (including after restart) is not proof
            # that no provider request happened.
            return dict(value) if value else {"status": "submission_uncertain", "job_id": job_id,
                                               "error": "job_state_unavailable"}

    def __call__(self, task: str, task_id: str) -> dict:
        return self.submit(task, task_id)

    def status(self) -> dict:
        """Report only this adapter's local occupancy, not provider quotas."""
        with self._lock:
            active = len(self._active_job_ids)
        return {"provider": "grok", "local_active_jobs": active,
                "local_max_concurrency": self.max_concurrency,
                "local_available_slots": self.max_concurrency - active,
                "official_quota_verified": False, "login_verified": False}

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            self._jobs[job_id].update(fields)

    def _execute(self, job_id: str, task: str) -> None:
        work: Path | None = None
        launched = False
        safe_cleanup = True
        preserve_output = False
        try:
            work = Path(tempfile.mkdtemp(prefix="grok-collab-"))
            home = work / "home"
            home.mkdir(mode=0o700)
            env = self._env(home)
            prompt, output = work / "task.txt", work / "output.txt"
            argv = [str(self.cli), "--model", self.model, "--reasoning-effort", self.effort,
                    "--prompt-file", str(prompt), "--session-id", job_id, "--output-format", "plain",
                    "--tools", "", "--disallowed-tools", "bash,read,write,edit,glob,grep,web_search,web_fetch,task,lsp",
                    "--deny", "*", "--permission-mode", "dontAsk", "--disable-web-search",
                    "--no-subagents", "--no-plan", "--max-turns", "2"]
            self._update(job_id, status="running", phase="inspection")
            inspect = subprocess.run([str(self.cli), "inspect", "--json"], cwd=str(work), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=15, shell=False, check=False)
            if inspect.returncode != 0 or len(inspect.stdout) > 1_000_000:
                self._update(job_id, status="failed", error="inspection_unavailable", submitted=False)
                return
            report = json.loads(inspect.stdout.decode("utf-8-sig"))
            diagnostics = inspect_report_diagnostics(
                report, subagents_disabled=env.get("GROK_SUBAGENTS") == "0" and "--no-subagents" in argv)
            if not diagnostics["allowed"]:
                # Never expose the inspection report, skill names, paths, or
                # arbitrary diagnostics: only fixed, path-free categories.
                reason = diagnostics.get("reason")
                fields = {"status": "failed", "error": "unexpected_or_unknown_extensions", "submitted": False}
                if isinstance(reason, str) and reason in INSPECTION_REASONS:
                    fields["inspection_reason"] = reason
                self._update(job_id, **fields)
                return
            self._update(job_id, inspection_metadata_unverified=diagnostics["unverified_metadata"])
            # Windows ACLs inherit from temp directory: do not claim Unix mode
            # alone protects private data on Windows.
            descriptor = os.open(prompt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(task)
            output_descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_descriptor, "wb") as result_file:
                self._update(job_id, phase="submitting")
                # Once launch is attempted, be conservative if the outcome is
                # unknown; never automatically send the task a second time.
                launched = True
                safe_cleanup = False
                child = subprocess.Popen(argv, cwd=str(work), env=env, stdin=subprocess.DEVNULL,
                                         stdout=result_file, stderr=subprocess.DEVNULL,
                                         shell=False, close_fds=True)
                self._update(job_id, phase="waiting")
                try:
                    code = child.wait(timeout=self.timeout)
                    safe_cleanup = type(code) is int
                except subprocess.TimeoutExpired:
                    # Report promptly so coordination can continue, but keep
                    # tracking the SAME child in this worker. We never launch
                    # another CLI, kill this process, or discard its files.
                    self._update(job_id, status="submission_uncertain",
                                 phase="waiting_for_original_process",
                                 error="process_still_running")
                    code = child.wait()
                    safe_cleanup = type(code) is int
            if not safe_cleanup:
                # A malformed wait result does not prove the child exited.
                # Preserve the files and slot; it may still be writing.
                preserve_output = True
                self._update(job_id, status="submission_uncertain", phase="waiting",
                             error="process_still_running", local_exit_confirmed=False)
                return
            if code != 0:
                # A non-zero local exit does not establish what the remote
                # service received. Keep any body explicitly partial and
                # retain the file even if persisting metadata fails.
                preserve_output = True
                with output.open("rb") as result_file:
                    data = result_file.read(_MAX_RESULT_BYTES + 1)
                details = {"status": "submission_uncertain", "phase": "completed",
                           "error": "cli_failed", "exit_code": code,
                           "local_exit_confirmed": True}
                if data:
                    details.update(output=data[:_MAX_RESULT_BYTES].decode("utf-8", errors="replace"),
                                   truncated=len(data) > _MAX_RESULT_BYTES, output_kind="partial")
                self._update(job_id, **details)
                return
            with output.open("rb") as result_file:
                data = result_file.read(_MAX_RESULT_BYTES + 1)
            if not data.strip():
                self._update(job_id, status="submission_uncertain", phase="completed",
                             error="empty_result", local_exit_confirmed=True)
                return
            truncated = len(data) > _MAX_RESULT_BYTES
            self._update(job_id, status="succeeded", phase="completed", error=None,
                         output=data[:_MAX_RESULT_BYTES].decode("utf-8", errors="replace"),
                         truncated=truncated, quality_verified=False, billing_verified=False)
        except Exception:
            if launched:
                # Retain any output after a failed read or metadata write.
                preserve_output = True
            self._update(job_id, status="submission_uncertain" if launched else "failed",
                         error="execution_unverified", submitted=None if launched else False,
                         local_exit_confirmed=bool(launched and safe_cleanup))
        finally:
            if work is not None and safe_cleanup:
                for name in (("task.txt",) if preserve_output else ("task.txt", "output.txt")):
                    try:
                        (work / name).unlink(missing_ok=True)
                    except OSError:
                        pass
                for directory in (work / "home", work):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass  # Never recursively remove CLI-created files.
            # Once a process may have launched, an unconfirmed exit reserves
            # the slot, even if this worker itself fails while waiting.
            if not launched or safe_cleanup:
                with self._lock:
                    self._active_job_ids.discard(job_id)
