"""
Writes the files the dashboard reads, and publishes them to a git branch.

File version: 2.0.0
  2.0.0: one Publisher now serves several strategies (a store per strategy),
         and stores can keep a small state file between runs.
  1.0.0: single strategy.

Files (one folder per strategy):

    strategies.json                    list of strategies, for the dashboard's switcher
    <strategy-id>/status.json          latest snapshot: account, positions, orders, bot health
    <strategy-id>/equity.csv           account value over time
    <strategy-id>/decisions.jsonl      every check the bot ran, and why
    <strategy-id>/orders.jsonl         every order the bot placed or tried to place
    <strategy-id>/trades.jsonl         every closed trade, with its profit or loss
    <strategy-id>/meta.json            when tracking began and the starting balance
    <strategy-id>/state.json           the bot's own notes between runs (not shown on the dashboard)

On GitHub Actions (DASHBOARD_BRANCH is set) these files live on their own
branch, "dashboard-data", kept as a SINGLE commit that is rewritten on every
publish. That keeps your main branch clean and stops the repo from filling
up with thousands of data commits. Running locally just writes to a
"dashboard_data" folder and skips git.

Nothing in here is allowed to stop the bot from trading: callers wrap
publishing in try/except, and push() reports failure instead of raising.
"""

import csv
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("publisher")

MAX_LINES = {"decisions.jsonl": 2500, "orders.jsonl": 600, "trades.jsonl": 1500}
MAX_EQUITY_ROWS = 20000

CLOCK = lambda: datetime.now(timezone.utc)   # the tests swap this for a simulated clock


def utc_now_iso() -> str:
    return CLOCK().isoformat(timespec="seconds")


class StrategyStore:
    """The files of one strategy. Same job the old single-strategy Publisher had."""

    def __init__(self, root: Path, strategy_id: str):
        self.id = strategy_id
        self.dir = root / strategy_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, name, obj):
        tmp = self.dir / f".{name}.tmp"
        tmp.write_text(json.dumps(obj, separators=(",", ":"), default=str), encoding="utf-8")
        tmp.replace(self.dir / name)

    def read_json(self, name, default=None):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except Exception:
            return default

    def append_jsonl(self, name, record):
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def append_equity(self, timestamp_iso, equity):
        path = self.dir / "equity.csv"
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["t", "equity"])
            w.writerow([timestamp_iso, f"{equity:.2f}"])

    def equity_rows(self) -> int:
        path = self.dir / "equity.csv"
        if not path.exists():
            return 0
        with open(path, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)

    def meta(self, equity=None) -> dict:
        """Tracking start date and starting balance, set once and then kept."""
        existing = self.read_json("meta.json")
        if existing:
            return existing
        if equity is None:
            return {}
        meta = {"tracking_since": utc_now_iso(), "starting_equity": round(float(equity), 2)}
        (self.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta


class Publisher:
    def __init__(self, local_dir="dashboard_data", branch=None, worktree_dir="data-branch"):
        self.branch = (branch if branch is not None else os.environ.get("DASHBOARD_BRANCH", "")).strip()
        self.git_enabled = False
        self.root = Path(local_dir)

        if self.branch:
            try:
                self._attach_branch(worktree_dir)
                self.root = Path(worktree_dir)
                self.git_enabled = True
            except Exception as e:
                log.warning(f"Could not set up the {self.branch} branch ({e}). "
                            f"Dashboard data will be written locally only.")

        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, strategy_id) -> StrategyStore:
        return StrategyStore(self.root, strategy_id)

    # ------------------------------------------------------------------ git

    def _run(self, args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)

    def _git(self, args, cwd=None):
        r = self._run(args, cwd)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
        return r

    def _attach_branch(self, worktree_dir):
        """Check the data branch out into its own folder, creating it if needed."""
        wt = Path(worktree_dir)
        if wt.exists():
            self._run(["worktree", "remove", "--force", str(wt)])
            shutil.rmtree(wt, ignore_errors=True)

        self._git(["config", "user.name", "trading-bot"])
        self._git(["config", "user.email", "actions@users.noreply.github.com"])

        probe = self._run(["ls-remote", "--exit-code", "--heads", "origin", self.branch])
        if probe.returncode == 0:
            self._git(["fetch", "--depth", "1", "origin", self.branch])
            self._git(["worktree", "add", "--detach", str(wt), "FETCH_HEAD"])
        elif probe.returncode == 2:
            # The branch doesn't exist yet: start a fresh, empty one.
            self._git(["worktree", "add", "--detach", str(wt)])
            self._git(["checkout", "--orphan", self.branch], cwd=wt)
            self._run(["rm", "-rf", "--quiet", "."], cwd=wt)
        else:
            raise RuntimeError(f"git ls-remote failed: {(probe.stderr or probe.stdout).strip()}")

    def push(self) -> bool:
        """Commit everything and force-push it as the branch's only commit. Returns True on success."""
        if not self.git_enabled:
            return True
        try:
            self._trim_files()
            self._git(["add", "-A"], cwd=self.root)
            if self._run(["diff", "--cached", "--quiet"], cwd=self.root).returncode == 0:
                return True  # nothing changed since the last push
            has_commit = self._run(["rev-parse", "--verify", "-q", "HEAD"], cwd=self.root).returncode == 0
            if has_commit:
                self._git(["commit", "--amend", "--no-edit", "--quiet"], cwd=self.root)
            else:
                self._git(["commit", "-m", "Dashboard data", "--quiet"], cwd=self.root)
            self._git(["push", "--force", "--quiet", "origin", f"HEAD:refs/heads/{self.branch}"], cwd=self.root)
            return True
        except Exception as e:
            log.warning(f"Could not publish dashboard data: {e}")
            return False

    # ---------------------------------------------------------------- files

    def write_index(self, strategies):
        obj = {"updated_at": utc_now_iso(), "strategies": strategies}
        tmp = self.root / ".strategies.json.tmp"
        tmp.write_text(json.dumps(obj, separators=(",", ":"), default=str), encoding="utf-8")
        tmp.replace(self.root / "strategies.json")

    def _trim_files(self):
        for sdir in self.root.iterdir():
            if not sdir.is_dir() or sdir.name.startswith("."):
                continue
            for name, keep in MAX_LINES.items():
                path = sdir / name
                if path.exists():
                    lines = path.read_text(encoding="utf-8").splitlines()
                    if len(lines) > keep:
                        path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
            path = sdir / "equity.csv"
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) > MAX_EQUITY_ROWS + 1:
                    path.write_text("\n".join([lines[0]] + lines[-MAX_EQUITY_ROWS:]) + "\n", encoding="utf-8")
