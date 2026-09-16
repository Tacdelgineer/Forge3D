"""Client for the host resource-control helper (host/forge3d-resctl).

Exclusive / Max Quality needs three things done on the host that a container
with no Docker socket, no systemd and no root cannot do for itself. The helper
does exactly those three; this module is the only thing in Forge3D that talks
to it, over an AF_UNIX socket bind-mounted at RESCTL_SOCKET.

The helper, not Forge3D, owns the lifecycle: it takes the snapshot, it releases,
and it restores on a lease deadline whether or not this process is still alive.
That is deliberate — the crash case is the one that matters, and a client that
has just died cannot run its own finally block.

Every function here degrades to "not available" rather than raising when the
helper is absent, because the helper is optional: with it uninstalled, Forge3D
behaves exactly as it did before Step 6 and the UI says why.
"""
import http.client
import json
import logging
import os
import socket
import threading
import time

log = logging.getLogger("trellis.resctl")

SOCKET_PATH = os.environ.get("FORGE3D_RESCTL_SOCKET", "/run/forge3d-resctl/resctl.sock")

# Long enough that a slow Ultra run cannot outlive it, short enough that a
# crashed client does not leave content-factory paused for the afternoon.
LEASE_SECONDS = int(os.environ.get("FORGE3D_EXCLUSIVE_LEASE_S", "1800"))
RENEW_INTERVAL = 60.0

_CONNECT_TIMEOUT = 5
_CALL_TIMEOUT = 180       # /acquire waits for memory to settle
_RELEASE_TIMEOUT = 700    # /release re-warms a 32 GiB model


class ResctlError(Exception):
    """The helper refused, or could not be reached."""

    def __init__(self, message: str, error: str | None = None, data: dict | None = None):
        super().__init__(message)
        self.error = error
        self.data = data or {}


class _UnixConnection(http.client.HTTPConnection):
    """http.client over AF_UNIX. The helper speaks plain HTTP on a socket file."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def available() -> bool:
    """Is the helper installed and listening?"""
    try:
        return os.path.exists(SOCKET_PATH)
    except OSError:
        return False


def _call(method: str, path: str, payload: dict | None = None, timeout: float = _CALL_TIMEOUT) -> dict:
    if not available():
        raise ResctlError(
            "The Forge3D host helper is not installed, so Exclusive mode cannot "
            "free memory. Install it with `sudo host/install.sh`.",
            error="helper_unavailable",
        )
    conn = _UnixConnection(SOCKET_PATH, timeout)
    try:
        body = json.dumps(payload or {}) if method == "POST" else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        data = json.loads(raw) if raw else {}
        if resp.status >= 400:
            raise ResctlError(
                data.get("reason") or data.get("error") or f"helper returned HTTP {resp.status}",
                error=data.get("error"),
                data=data,
            )
        return data
    except ResctlError:
        raise
    except (OSError, ValueError) as exc:
        raise ResctlError(
            f"Could not reach the Forge3D host helper: {type(exc).__name__}: {exc}",
            error="helper_unreachable",
        ) from exc
    finally:
        conn.close()


def state() -> dict | None:
    """What the helper sees right now, or None if it is not there."""
    try:
        return _call("GET", "/state", timeout=_CONNECT_TIMEOUT + 10)
    except ResctlError as exc:
        log.debug("resctl state unavailable: %s", exc)
        return None


def releasable_gb() -> float:
    """Memory the helper believes it could free. 0.0 when it cannot say."""
    s = state()
    if not s or s.get("exclusive"):
        return 0.0
    return float((s.get("releasable") or {}).get("total_est_gb") or 0.0)


class Lease:
    """An acquired exclusive-mode lease, renewed in the background.

    Used as a context manager so the release cannot be skipped by an early
    return or an exception. If this process dies anyway, the helper's own
    deadline restores the workloads — this class is the tidy path, not the
    safety net.
    """

    def __init__(self, lease_seconds: int = LEASE_SECONDS):
        self.lease_seconds = lease_seconds
        self.token: str | None = None
        self.snapshot: dict | None = None
        self.released: dict | None = None
        self.restored: dict | None = None
        self.restore_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- renewal --
    def _renew_loop(self) -> None:
        while not self._stop.wait(RENEW_INTERVAL):
            try:
                _call("POST", "/renew",
                      {"token": self.token, "lease_seconds": self.lease_seconds},
                      timeout=_CONNECT_TIMEOUT + 10)
            except ResctlError as exc:
                # Not fatal here: the run continues and the helper restores on
                # its deadline if the renewals never resume.
                log.warning("exclusive lease renew failed: %s", exc)

    # -- lifecycle --
    def acquire(self) -> dict:
        data = _call("POST", "/acquire", {"lease_seconds": self.lease_seconds})
        self.token = data["token"]
        self.snapshot = data.get("snapshot")
        self.released = data.get("released")
        r = self.released or {}
        log.info(
            "exclusive mode acquired: MemAvailable %.2f -> %.2f GiB (freed %.2f, "
            "expected %.2f, settled=%s)",
            r.get("available_before_gb", 0.0), r.get("available_after_gb", 0.0),
            r.get("freed_gb", 0.0), r.get("expected_gb", 0.0), r.get("settled"),
        )
        for err in r.get("errors") or []:
            log.error("exclusive release problem: %s", err)
        self._thread = threading.Thread(target=self._renew_loop,
                                        name="forge3d-lease", daemon=True)
        self._thread.start()
        return data

    def release(self) -> dict | None:
        """Restore. Never raises: a caller in a finally block must not be
        derailed, but the error is recorded and logged loudly."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if not self.token:
            return None
        try:
            data = _call("POST", "/release", {"token": self.token},
                         timeout=_RELEASE_TIMEOUT)
            self.restored = data.get("restored")
            errors = (self.restored or {}).get("errors") or []
            if errors:
                self.restore_error = "; ".join(errors)
                log.error("EXCLUSIVE RESTORE FAILED: %s", self.restore_error)
            else:
                log.info("exclusive mode released; workloads restored")
            return data
        except ResctlError as exc:
            # The helper's lease deadline is the backstop for exactly this.
            self.restore_error = str(exc)
            log.error(
                "EXCLUSIVE RESTORE CALL FAILED: %s. The host helper will restore "
                "on its lease deadline (<= %ds); `sudo forge3d-resctl recover` "
                "forces it now.", exc, self.lease_seconds)
            return None
        finally:
            self.token = None

    def report(self) -> dict:
        """What to put on the job record and in /system: what was paused, what
        came back, and anything that did not."""
        r = self.released or {}
        return {
            "released": [
                {"kind": a["kind"], "name": a["name"], "ok": a["ok"], "detail": a["detail"]}
                for a in (r.get("actions") or [])
            ],
            "available_before_gb": r.get("available_before_gb"),
            "available_after_gb": r.get("available_after_gb"),
            "freed_gb": r.get("freed_gb"),
            "settled": r.get("settled"),
            "release_errors": r.get("errors") or [],
            "restored": [
                {"kind": a["kind"], "name": a["name"], "ok": a["ok"], "detail": a["detail"]}
                for a in ((self.restored or {}).get("actions") or [])
            ],
            "restore_errors": ((self.restored or {}).get("errors") or []
                               ) or ([self.restore_error] if self.restore_error else []),
            "restore_ok": not self.restore_error,
        }

    def __enter__(self) -> "Lease":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def describe_pause(s: dict | None = None) -> list[dict]:
    """Plain-language list of what Exclusive mode would pause, for the UI."""
    s = s if s is not None else state()
    if not s:
        return []
    items = (s.get("releasable") or {}).get("items") or []
    label = {
        "ollama_model": "Ollama model",
        "hunyuan_models": "Hunyuan worker models",
        "systemd_unit": "Service",
    }
    return [{
        "kind": i["kind"],
        "label": label.get(i["kind"], i["kind"]),
        "name": i["name"],
        "est_gb": i["est_gb"],
        "basis": i["basis"],
        "action": i["action"],
    } for i in items]
