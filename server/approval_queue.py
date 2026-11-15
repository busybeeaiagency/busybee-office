"""Loader for the SHARED approval-queue module (the decision queue's one funnel).

The canonical code lives on the Hermes side (`/opt/data/scripts/approval_queue.py`, mounted here
read-only at `/hermes/scripts/approval_queue.py`): `pending.json`, the review-gate marker and the
decisions overlay belong to it, not to this repo, so the dashboard must call that exact file rather
than keep a second copy of the rules. This module only finds and loads it.

Nothing here raises and nothing here touches the queue: `queue()` returns the module, or `None` when
it cannot be loaded (no Hermes mount, a half-deployed script, a syntax error in it). A button on the
dashboard must still move the card when the queue is unreachable — the `kanban_review_gate.py` cron
reconciles the leftovers within its own tick — so the caller branches on `None` instead of wrapping
every call in a try. One line to stderr the first time a load fails per process: the container logs
are the only place that failure would ever be visible, and a per-request line would spam them.
"""
import importlib.util
import os
import sys

ENV_VAR = "OFFICE_APPROVAL_MODULE"
# Only used when the env var is unset: container path first (the read-only Hermes mount), host path
# second (running `python3 -m server.office` locally against /opt/data).
DEFAULT_PATHS = ("/hermes/scripts/approval_queue.py", "/opt/data/scripts/approval_queue.py")
MODULE_NAME = "busybee_office_approval_queue"

_CACHE = {}
_WARNED = False


def candidate_paths():
    """Paths to try, in order. Resolved at CALL time so tests (and a second deployment) repoint them.

    An explicitly configured path is used ALONE: a deliberate override must not quietly fall back to
    a different machine's copy of the queue, because the module and its state dir have to be the same
    checkout — otherwise a test would "pass" while publishing to production's `pending.json`.
    """
    configured = (os.environ.get(ENV_VAR) or "").strip()
    if configured:
        return [configured]
    return list(DEFAULT_PATHS)


def _load(path):
    """exec_module one file as a plain module, or None. The canonical module is pure stdlib, so no
    sys.path games (that would let /hermes/scripts shadow stdlib names process-wide)."""
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(MODULE_NAME, None)
            raise
        return module
    except BaseException:                                 # noqa: BLE001 - a broken queue must not
        return None                                        # break an operator's button click


def queue():
    """The shared approval_queue module, or None when it is unavailable. Never raises."""
    key = tuple(candidate_paths())
    if key in _CACHE:
        return _CACHE[key]
    module = None
    for path in key:
        if path and os.path.isfile(path):
            module = _load(path)
            if module is not None:
                break
    if module is None:
        _warn(key)
    _CACHE[key] = module
    return module


def _warn(paths):
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    sys.stderr.write("[office] approval_queue unavailable (looked at: %s)\n" % ", ".join(paths))


def available():
    """Cheap probe for /healthz-style reporting: is the shared module loadable right now?"""
    return queue() is not None
