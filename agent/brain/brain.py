import ctypes
import errno
import heapq
import json
import os
import sys
import textwrap
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class CoreAgentError(RuntimeError):
    """Исключение при сбое вызова функции нативного ядра."""

    def __init__(self, code: int) -> None:
        detail = ctypes.FormatError(code) if sys.platform == "win32" else os.strerror(code)
        super().__init__(f"[CoreError {code}] {detail}")
        self.code = code


class CSystemInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_int64),
        ("cpus", ctypes.c_int32),
        ("page_size", ctypes.c_int32),
        ("host", ctypes.c_char * 256),
        ("uptime_s", ctypes.c_int64),
    ]


class CProcessRecord(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_int64),
        ("ppid", ctypes.c_int64),
        ("name", ctypes.c_char * 64),
    ]


class CChildProcess(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_int64),
        ("handle", ctypes.c_int64),
    ]


class CCpuCounters(ctypes.Structure):
    _fields_ = [
        ("user_us", ctypes.c_int64),
        ("kernel_us", ctypes.c_int64),
    ]


@dataclass(frozen=True)
class AgentConfig:
    core_dir: Path
    default_proc_limit: int = 256
    max_tree_depth: int = 64
    http_timeout: int = 120


class NativeCoreClient:
    """Клиент для работы с C-библиотекой мониторинга."""

    def __init__(self, config: Optional[AgentConfig] = None) -> None:
        self.cfg = config or AgentConfig(core_dir=Path(__file__).resolve().parents[1] / "core")
        self._range_err = 122 if sys.platform == "win32" else errno.ERANGE
        self._dll = self._load_shared_lib()
        self._register_prototypes()

    def _load_shared_lib(self) -> ctypes.CDLL:
        lib_map = {
            "win32": "sentry_core.dll",
            "darwin": "libsentry_core.dylib",
        }
        target_name = lib_map.get(sys.platform, "libsentry_core.so")
        return ctypes.CDLL(str(self.cfg.core_dir / target_name))

    def _register_prototypes(self) -> None:
        signatures = {
            "core_get_info": ([ctypes.POINTER(CSystemInfo)], ctypes.c_int),
            "core_list_procs": (
                [ctypes.POINTER(CProcessRecord), ctypes.c_int32, ctypes.POINTER(ctypes.c_int32)],
                ctypes.c_int,
            ),
            "core_spawn": ([ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(CChildProcess)], ctypes.c_int),
            "core_wait": ([ctypes.POINTER(CChildProcess), ctypes.c_int32, ctypes.POINTER(ctypes.c_int32)], ctypes.c_int),
            "core_proc_cpu": ([ctypes.c_int64, ctypes.POINTER(CCpuCounters)], ctypes.c_int),
            "core_proc_threads": ([ctypes.c_int64, ctypes.POINTER(ctypes.c_int32)], ctypes.c_int),
        }
        for func_name, (args, res) in signatures.items():
            fn = getattr(self._dll, func_name)
            fn.argtypes = args
            fn.restype = res

    @staticmethod
    def _ensure_ok(status: int) -> None:
        if status != 0:
            raise CoreAgentError(status)

    def read_host_metrics(self) -> dict[str, Any]:
        buf = CSystemInfo()
        self._ensure_ok(self._dll.core_get_info(ctypes.byref(buf)))
        return {
            "pid": buf.pid,
            "cpus": buf.cpus,
            "page_size": buf.page_size,
            "host": buf.host.decode("utf-8"),
            "uptime_s": buf.uptime_s,
        }

    def snapshot_processes(self) -> list[dict[str, Any]]:
        capacity = self.cfg.default_proc_limit
        while True:
            array_type = CProcessRecord * capacity
            records = array_type()
            actual = ctypes.c_int32(0)

            code = self._dll.core_list_procs(records, capacity, ctypes.byref(actual))
            if code == 0:
                return [
                    {
                        "pid": records[i].pid,
                        "ppid": records[i].ppid,
                        "name": records[i].name.decode("utf-8", errors="replace"),
                    }
                    for i in range(actual.value)
                ]

            if code != self._range_err:
                raise CoreAgentError(code)

            capacity = actual.value + 64

    def get_parent_chain(self, pid: int, processes: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        items = processes if processes is not None else self.snapshot_processes()
        index = {row["pid"]: row for row in items}
        chain: list[dict[str, Any]] = []
        cursor = pid

        while cursor in index and len(chain) < self.cfg.max_tree_depth:
            entry = index[cursor]
            chain.append(entry)
            if entry["ppid"] == cursor:
                break
            cursor = entry["ppid"]

        return chain

    def start_child(self, command: list[str]) -> CChildProcess:
        raw_args = [part.encode("utf-8") for part in command]
        argv = (ctypes.c_char_p * (len(raw_args) + 1))(*raw_args, None)
        handle = CChildProcess()
        self._ensure_ok(self._dll.core_spawn(argv, ctypes.byref(handle)))
        return handle

    def join_child(self, handle: CChildProcess, timeout_ms: int = -1) -> int:
        exit_code = ctypes.c_int32(0)
        self._ensure_ok(self._dll.core_wait(ctypes.byref(handle), timeout_ms, ctypes.byref(exit_code)))
        return exit_code.value

    def read_cpu_time_us(self, pid: int) -> Optional[int]:
        counters = CCpuCounters()
        if self._dll.core_proc_cpu(pid, ctypes.byref(counters)) != 0:
            return None
        return counters.user_us + counters.kernel_us

    def read_thread_count(self, pid: int) -> Optional[int]:
        count = ctypes.c_int32(0)
        if self._dll.core_proc_threads(pid, ctypes.byref(count)) != 0:
            return None
        return count.value

    def sample_cpu_load(self, window_sec: float = 1.0) -> list[dict[str, Any]]:
        procs = self.snapshot_processes()
        initial_ticks = {p["pid"]: self.read_cpu_time_us(p["pid"]) for p in procs}
        ts_start = time.monotonic()

        time.sleep(window_sec)

        delta_wall_us = (time.monotonic() - ts_start) * 1_000_000
        if delta_wall_us <= 0:
            return []

        metrics: list[dict[str, Any]] = []
        for p in procs:
            pid = p["pid"]
            t1 = initial_ticks.get(pid)
            t2 = self.read_cpu_time_us(pid)
            if t1 is not None and t2 is not None:
                pct = round(((t2 - t1) * 100) / delta_wall_us)
                metrics.append({"pid": pid, "name": p["name"], "cpu": pct})

        metrics.sort(key=lambda row: row["cpu"], reverse=True)
        return metrics

    def top_by_threads(self, top_n: int = 3) -> list[dict[str, Any]]:
        stats = []
        for p in self.snapshot_processes():
            t_num = self.read_thread_count(p["pid"])
            if t_num is not None:
                stats.append({"pid": p["pid"], "name": p["name"], "threads": t_num})
        return heapq.nlargest(top_n, stats, key=lambda row: row["threads"])


def push_webhook(target_url: str, data: dict[str, Any], timeout: int = 120) -> tuple[int, str]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        target_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def run_agent() -> None:
    client = NativeCoreClient()

    probe_code = textwrap.dedent("""\
        import threading, time
        pool = [threading.Thread(target=time.sleep, args=(3,)) for _ in range(4)]
        for item in pool: item.start()
        for item in pool: item.join()
    """)

    child = client.start_child([sys.executable, "-c", probe_code])
    time.sleep(1.0)

    payload = {
        "event": "agent_started",
        "os": sys.platform,
        **client.read_host_metrics(),
        "child": child.pid,
        "child_threads": client.read_thread_count(child.pid),
        "most_threads": client.top_by_threads(3),
    }

    client.join_child(child, timeout_ms=5000)

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if len(sys.argv) > 1:
        http_code, resp_text = push_webhook(sys.argv[1], payload)
        print("n8n:", http_code, resp_text)


if __name__ == "__main__":
    run_agent()
