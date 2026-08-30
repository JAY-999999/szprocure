"""P1-D-A — Batch Runner: orchestrates stages with strict Run/Release separation.

Key guarantees
--------------
* Run produces a Batch result + READY_FOR_RELEASE state only.
* Run NEVER builds the site, commits, pushes, or deploys.
* Run NEVER calls release() — release() is a separate human gate.  In P1-D-A it
  is a boundary stub: it records the human approval (APPROVED) but performs NO
  git / build / deploy.  Those land in later phases and are intentionally absent
  so Run can never trigger them.
* STOP exceptions block every downstream stage.  AUTO_SKIP / AUTO_PASS never do.
* Every stage is resumable (re-run with from_stage=) and re-executable.
* An explicit cross-stage state machine prevents "fake completion" after a crash:
  the manifest status only advances along ALLOWED_TRANSITIONS, and a STOP sets
  it to FAILED.
"""
from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime

from . import ids, manifest as MAN, gate
from .manifest import FAILED
from .stages import BatchContext, StageResult, STAGE_ORDER, STAGE_FLOW, STAGES


def _now():
    return datetime.now().isoformat(timespec="seconds")


class RunResult:
    """Aggregated outcome of a full run — answers the batch questions."""

    def __init__(self, batch_id):
        self.batch_id = batch_id
        self.ok = False
        self.stopped = False
        self.stage_results = []
        self.stop_reasons = []
        self.release_eligible = False
        self.manifest_status = None

    def summary(self):
        return {
            "batch_id": self.batch_id,
            "ok": self.ok,
            "stopped": self.stopped,
            "status": self.manifest_status,
            "release_eligible": self.release_eligible,
            "stop_reasons": self.stop_reasons,
            "stages": [s.as_dict() for s in self.stage_results],
        }


# --------------------------------------------------------------------------
# state-machine helpers
# --------------------------------------------------------------------------
def _path_status(cur, target):
    """BFS shortest valid transition path cur -> target (None if unreachable)."""
    if cur == target:
        return [cur]
    prev = {cur: None}
    q = deque([cur])
    while q:
        n = q.popleft()
        for nxt in MAN.ALLOWED_TRANSITIONS.get(n, ()):
            if nxt in prev:
                continue
            prev[nxt] = n
            if nxt == target:
                path = [target]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return list(reversed(path))
            q.append(nxt)
    return None


def _ensure_status(m, target):
    """Advance the manifest status to `target` via valid transitions.

    On resume the batch may need to fast-forward several statuses; we walk the
    state machine so every intermediate transition stays legal.  If `target` is
    unreachable from the current status (should not happen for a healthy batch)
    we set it directly so a stuck batch is not silently dropped.
    """
    if m.status == target:
        return
    path = _path_status(m.status, target)
    if path is None:
        m.data["status"] = target
        m.save()
        return
    for s in path[1:]:
        m.set_status(s)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def run(batch_id=None, strategy=None, count=None, when=None,
        root=None, pool_root=None, backup_root=None, options=None,
        from_stage=None, create=True):
    """Execute the full (or resumed) batch pipeline.

    Parameters
    ----------
    batch_id   : explicit id; if None one is generated via ids.make_batch_id.
    strategy   : batch strategy used for id generation / manifest.
    root       : manifest batch root (default DEFAULT_BATCH_ROOT).
    pool_root  : SZ_POOL_ROOT for pool modules (default = env / DEFAULT_POOL_ROOT).
    backup_root: staging backup root (default <root>/backups).
    options    : dict passed to every stage (source_path, master_csv, mfr_csv,
                 selector, category, limit, workers, retries, timeout, r2_client,
                 r2_bucket, dry_run, ...).
    from_stage : resume the pipeline starting at this stage (skips earlier ones,
                 assumes their results already exist).
    create     : create the manifest if it does not exist (else load it).
    """
    options = dict(options or {})
    root = root or MAN.DEFAULT_BATCH_ROOT
    backup_root = backup_root or os.path.join(root, "backups")

    # resolve batch id
    if batch_id is None:
        batch_id = ids.make_batch_id(
            strategy or "BATCH", count if count is not None else 0,
            when=when, exist_ok_check=create, root=root)

    # create or load manifest
    if create and not ids.batch_exists(batch_id, root):
        m = MAN.BatchManifest.create(
            batch_id, strategy or "BATCH",
            category=options.get("category"), root=root)
    else:
        m = MAN.BatchManifest.load(batch_id, root)

    ctx = BatchContext(batch_id, root, pool_root, backup_root, m, options)
    rr = RunResult(batch_id)

    # determine start index + fast-forward the manifest status
    if from_stage is not None:
        if from_stage not in STAGE_ORDER:
            raise ValueError(f"unknown stage {from_stage!r}")
        idx = STAGE_ORDER.index(from_stage)
        # Resuming a failed / stopped batch: the prior run's exceptions belong
        # to stages we are about to re-derive, so clear them. A re-exec must not
        # stay blocked by a STOP that the new inputs no longer produce.
        if m.has_stop() or m.status == FAILED:
            m.data["exceptions"] = []
            m.data.setdefault("stage_log", []).append(
                {"stage": "RESUME", "at": _now(),
                 "ok": True, "note": f"resuming from {from_stage}; "
                                     f"prior exceptions cleared"})
            m.save()
        _ensure_status(m, STAGE_FLOW[idx][1])
    else:
        idx = 0

    for (name, in_s, out_s) in STAGE_FLOW[idx:]:
        try:
            sr = STAGES[name](ctx)
        except Exception as e:                       # unexpected stage error
            m.add_exception(gate.MANIFEST_ERROR, gate.STOP, None,
                            f"{name} raised {type(e).__name__}: {e}")
            sr = StageResult(name)
            sr.fail(note=f"unexpected error: {e}", stopped=True)
        rr.stage_results.append(sr)

        # gate check: STOP blocks every downstream stage
        if (not sr.ok) or m.has_stop():
            rr.stopped = True
            rr.stop_reasons = [
                f"{e.get('code')}: {e.get('message')}"
                for e in m.exceptions_by_severity(gate.STOP)
            ]
            _ensure_status(m, FAILED)
            rr.manifest_status = m.status
            return rr

        # advance the manifest state machine
        _ensure_status(m, out_s)
        m.save()

    rr.ok = True
    rr.manifest_status = m.status
    rr.release_eligible = (m.status == MAN.READY_FOR_RELEASE)
    return rr


# --------------------------------------------------------------------------
# run_stage — re-execute a single stage (retry / fix-then-resume)
# --------------------------------------------------------------------------
def run_stage(batch_id, stage_name, root=None, pool_root=None, backup_root=None,
              options=None):
    if stage_name not in STAGE_ORDER:
        raise ValueError(f"unknown stage {stage_name!r}")
    m = MAN.BatchManifest.load(batch_id, root)
    options = dict(options or {})
    backup_root = backup_root or os.path.join(root or MAN.DEFAULT_BATCH_ROOT, "backups")
    ctx = BatchContext(batch_id, root or MAN.DEFAULT_BATCH_ROOT, pool_root,
                       backup_root, m, options)
    idx = STAGE_ORDER.index(stage_name)
    _ensure_status(m, STAGE_FLOW[idx][1])
    sr = STAGES[stage_name](ctx)
    if (not sr.ok) or m.has_stop():
        _ensure_status(m, FAILED)
    else:
        _ensure_status(m, STAGE_FLOW[idx][2])
        m.save()
    return sr


# --------------------------------------------------------------------------
# batch_status — read-only inspection
# --------------------------------------------------------------------------
def batch_status(batch_id, root=None):
    m = MAN.BatchManifest.load(batch_id, root)
    return m.summary()


def stage_status(batch_id, root=None):
    """Return the per-stage ok/note list from the manifest stage_log."""
    m = MAN.BatchManifest.load(batch_id, root)
    return list(m.data.get("stage_log", []))


# --------------------------------------------------------------------------
# release — SEPARATE human gate.  NEVER called by run().
# --------------------------------------------------------------------------
def release(batch_id, root=None, approved_by=None):
    """Human-gated Release boundary.  Intentionally NOT invoked by run().

    P1-D-A scope: records the human approval gate (APPROVED).  It performs NO
    git commit / push / deploy / site build — those land in later phases and
    are absent here so that Run can never trigger a release on its own.
    """
    m = MAN.BatchManifest.load(batch_id, root)
    if m.status != MAN.READY_FOR_RELEASE:
        raise RuntimeError(
            f"batch {batch_id} is not READY_FOR_RELEASE (status={m.status}); "
            f"cannot release")
    m.set_status(MAN.APPROVED,
                 note=f"human release approval by {approved_by}")
    return {"released": False, "status": m.status,
            "note": "release boundary: Build/Commit/Push/Deploy out of P1-D-A scope"}


def main(argv=None):
    """Minimal CLI for manual inspection / sandbox runs.

    Usage:
      python -m tools.factory.batch_runner status <batch_id>
      python -m tools.factory.batch_runner stages <batch_id>
    """
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: batch_runner <status|stages> <batch_id>")
        return 1
    cmd, bid = args[0], args[1]
    if cmd == "status":
        print(json.dumps(batch_status(bid), ensure_ascii=False, indent=2))
    elif cmd == "stages":
        print(json.dumps(stage_status(bid), ensure_ascii=False, indent=2))
    else:
        print(f"unknown command {cmd!r}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
