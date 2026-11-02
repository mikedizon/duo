"""Optional live WireGuard test: a real WireGuard-app tunnel and the real network.

Not collected by `unittest discover` (the file name does not start with
test_). It starts one of your WireGuard-app tunnels while it runs, so it
refuses to start unless DUO_WG_LIVE=1 is set:

    DUO_WG_LIVE=1 python3 tests/live_wireguard.py

It connects one random WireGuard-app tunnel, verifies it, checks health,
stops it, and asserts that the default route, the probe route and the system
resolvers match the baseline captured before connecting. Cleanup runs from
try/finally, atexit and SIGINT/SIGTERM handlers. No sudo is involved.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wg_macos import MacPlatform  # noqa: E402
from wg_manager import ManagerSettings, ProcessLock, WireGuardManager  # noqa: E402


def say(text: str) -> None:
    print(f"  {text}", flush=True)


def main() -> int:
    if os.environ.get("DUO_WG_LIVE") != "1":
        print("Refusing to run: set DUO_WG_LIVE=1 to allow a real WireGuard connection.", file=sys.stderr)
        return 2

    platform = MacPlatform()
    settings = ManagerSettings()
    lock = ProcessLock(Path(tempfile.gettempdir()) / "duo-wireguard-live-test.lock")
    lock.acquire()
    mgr = WireGuardManager(platform, settings=settings, console=say)

    done = {"cleaned": False}

    def cleanup() -> None:
        if done["cleaned"]:
            return
        done["cleaned"] = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("cleanup: stopping the tunnel and verifying the host network…", flush=True)
        ok = mgr.cleanup()
        print(f"cleanup: {'restored' if ok else 'INCOMPLETE - follow the manual recovery steps above'}", flush=True)
        lock.release()

    def on_signal(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, on_signal)

    failures: list[str] = []
    try:
        print("recover: checking for sessions left open by an earlier run", flush=True)
        mgr.recover_orphans()
        before = platform.snapshot()
        probe_before = platform.route_get(settings.route_probe_ip).interface
        print(f"baseline: default via {before.default_interface}, resolvers {before.resolvers}", flush=True)
        print("connect:", flush=True)
        st = mgr.connect(reason="live_test")
        for line in st.lines():
            say(line)
        if not mgr.health_check():
            failures.append("health check failed right after connect")
        print("disconnect:", flush=True)
        mgr.disconnect(reason="live_test")
        after = platform.snapshot()
        if after.default_interface != before.default_interface:
            failures.append(f"default route {after.default_interface} != baseline {before.default_interface}")
        probe_after = platform.route_get(settings.route_probe_ip).interface
        if probe_after != probe_before:
            failures.append(f"probe route {probe_after} != baseline {probe_before}")
        if sorted(after.resolvers) != sorted(before.resolvers):
            failures.append(f"resolvers {after.resolvers} != baseline {before.resolvers}")
        if after.connected_vpns:
            failures.append(f"VPN services still connected: {after.connected_vpns}")
    except KeyboardInterrupt:
        failures.append("interrupted")
    except Exception as exc:  # report everything; cleanup still runs
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        cleanup()

    print("RESULT:", "PASS" if not failures else "FAIL", flush=True)
    for f in failures:
        print(f"  - {f}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
