"""The daemon: ingest, enrich, and (long-running mode) serve provider calls over a socket.

Two lifecycles, one code path per tick:

* ``dirvana daemon --oneshot``: one tick (ingest + enrich dirty nodes), then exit. For timers.
* ``dirvana daemon``: tick every ``daemon.interval`` seconds and serve ``<root>/run/daemon.sock``
  so hotkeys reuse warm provider clients (including a pre-warmed Copilot session).

Protocol: one JSON object per line each way. Ops: ``ping``, ``status``, ``complete``
(``instance``, ``subject``, ``request``), ``enrich`` (``paths``, ``force``). The daemon re-runs
the egress check on every ``complete``; it trusts nothing the client says about policy.

``rm -rf <root>`` while running is survivable: every tick re-creates the root skeleton, the
lock file and the socket if they disappeared.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

from dirvana.enrich import ProviderPool, enrich
from dirvana.providers.base import CompletionRequest, ProviderError, Purpose
from dirvana.runtime import Runtime
from dirvana.store.io import as_dict, as_list


class AlreadyRunning(Exception):
    pass


class Lock:
    """An flock on ``run/daemon.lock`` that notices when the file is deleted under it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.ino: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(f"another daemon holds {self.path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd, self.ino = fd, os.fstat(fd).st_ino

    def ensure(self) -> None:
        try:
            if os.stat(self.path).st_ino == self.ino:
                return
        except FileNotFoundError:
            pass
        self.release()
        self.acquire()

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _peer_uid(sock: socket.socket) -> int | None:
    if sys.platform.startswith("linux"):
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", creds)[1])
    getpeereid = getattr(os, "getpeereid", None)
    if getpeereid is not None:  # macOS/BSD (not exposed by every Python build)
        return int(getpeereid(sock.fileno())[0])
    return None


class Daemon:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.pool = ProviderPool(rt.config, rt.dirs)
        self.lock = Lock(rt.dirs.run / "daemon.lock")
        self.server: asyncio.AbstractServer | None = None
        self.sock_ino: int | None = None
        self.started = time.time()
        self.ticks = 0
        self.last_report: dict[str, Any] = {}
        self._background: set[asyncio.Future[Any]] = set()

    # -- ticks -------------------------------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        rt = self.rt
        rt.dirs.ensure_root()
        self.lock.ensure()
        rt.reload_policy()
        ingest = await asyncio.to_thread(rt.store.ingest)
        rt.budget.run_spent.clear()
        report = await enrich(rt.store, rt.config, rt.guard, self.pool, rt.budget)
        self.ticks += 1
        self.last_report = {
            "ingested": len(ingest.changed),
            "enriched": report.enriched,
            "calls": report.calls,
            "failures": report.failures,
        }
        return self.last_report

    async def run_once(self) -> dict[str, Any]:
        self.lock.acquire()
        try:
            return await self.tick()
        finally:
            await self.pool.aclose()
            self.lock.release()

    async def run_forever(self) -> None:
        self.lock.acquire()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await self._serve()
        await self._warm()
        interval = self.rt.config.num("daemon", "interval", 60)
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception as e:  # a bad tick must not kill the daemon
                    print(f"dirvana daemon: tick failed: {type(e).__name__}: {e}", file=sys.stderr)
                deadline = time.monotonic() + interval
                while not stop.is_set() and time.monotonic() < deadline:
                    await self._ensure_socket()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=min(2.0, interval))
        finally:
            if self.server:
                self.server.close()
            with contextlib.suppress(FileNotFoundError):
                Path(self.rt.socket_path).unlink()
            await self.pool.aclose()
            self.lock.release()

    async def _warm(self) -> None:
        for name in self.rt.config.configured():
            if self.rt.config.providers[name].kind == "copilot":
                provider = self.pool.get(name)
                warm = getattr(provider, "warm", None)
                if warm is not None:
                    self._background.add(asyncio.ensure_future(warm()))

    # -- socket ------------------------------------------------------------------------------

    async def _serve(self) -> None:
        path = Path(self.rt.socket_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self.server = await asyncio.start_unix_server(self._handle, path=str(path))
        path.chmod(0o600)
        self.sock_ino = path.stat().st_ino

    async def _ensure_socket(self) -> None:
        try:
            if os.stat(self.rt.socket_path).st_ino == self.sock_ino:
                return
        except FileNotFoundError:
            pass
        if self.server:
            self.server.close()
        self.rt.dirs.ensure_root()
        await self._serve()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            uid = _peer_uid(sock) if sock is not None else None
            if uid is not None and uid != os.geteuid():
                writer.write(b'{"ok":false,"error":"permission denied"}\n')
                return
            line = await reader.readline()
            try:
                msg = as_dict(json.loads(line))
            except ValueError:
                msg = {}
            resp = await self.dispatch(msg)
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            with contextlib.suppress(Exception):
                writer.write(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "status":
            return {
                "ok": True,
                "pid": os.getpid(),
                "uptime": round(time.time() - self.started),
                "ticks": self.ticks,
                "last": self.last_report,
            }
        if op == "complete":
            return await self._complete(msg)
        if op == "enrich":
            paths = [str(p) for p in as_list(msg.get("paths"))] or None
            report = await enrich(
                self.rt.store,
                self.rt.config,
                self.rt.guard,
                self.pool,
                self.rt.budget,
                paths=paths,
                force=bool(msg.get("force")),
                explicit=paths is not None,
            )
            return {
                "ok": True,
                "enriched": report.enriched,
                "skipped": report.skipped,
                "calls": report.calls,
                "failures": report.failures,
            }
        return {"ok": False, "error": f"unknown op {op!r}"}

    async def _complete(self, msg: dict[str, Any]) -> dict[str, Any]:
        rt = self.rt
        instance, subject = str(msg.get("instance", "")), str(msg.get("subject", ""))
        r = as_dict(msg.get("request"))
        purpose: Purpose = "suggest"
        if r.get("purpose") in ("enrich", "brief", "smoke"):
            purpose = r["purpose"]
        req = CompletionRequest(
            system=str(r.get("system", "")),
            user=str(r.get("user", "")),
            max_output_tokens=int(r.get("max_output_tokens", 512)),
            timeout_s=float(r.get("timeout_s", 2.5)),
            purpose=purpose,
        )
        if instance not in rt.config.providers:
            return {"ok": False, "error": f"unknown provider {instance!r}"}
        pc = rt.config.providers[instance]
        try:
            req = rt.guard.check_request(subject, instance, req)  # never trust the client
        except Exception as e:
            return {"ok": False, "error": str(e)}
        why = rt.budget.exhausted(pc)
        if why:
            return {"ok": False, "error": why}
        try:
            result = await asyncio.wait_for(
                self.pool.get(instance).complete(req), req.timeout_s + 1
            )
        except (ProviderError, TimeoutError) as e:
            rt.budget.record(
                pc,
                model=pc.model,
                purpose=req.purpose,
                usage=None,
                ok=False,
                node=subject,
                reason=str(e),
            )
            return {"ok": False, "error": str(e)}
        rt.budget.record(
            pc, model=result.model, purpose=req.purpose, usage=result.usage, ok=True, node=subject
        )
        u = result.usage
        return {
            "ok": True,
            "text": result.text,
            "model": result.model,
            "usage": {"unit": u.unit, "input": u.input, "output": u.output, "requests": u.requests},
        }


def request(sock_path: str, msg: dict[str, Any], timeout: float = 5.0) -> dict[str, Any] | None:
    """Send one message to a running daemon; ``None`` if none is listening."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
    except OSError:
        s.close()
        return None
    try:
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return as_dict(json.loads(buf or b"{}"))
