"""
Clash Dashboard Launcher
========================
Manages the full lifecycle of:
  1. Bundled Mihomo (optional — skip if CLASH_API_URL is set externally)
  2. FastAPI backend (always)

Usage:
  python launcher.py              # auto-detect: bundled mihomo or external Clash
  python launcher.py --no-mihomo  # only start backend, use CLASH_API_URL
  python launcher.py --kill       # stop any running mihomo and backend

Signals: Ctrl+C / SIGTERM stops all processes gracefully.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# ─── Platform detection ──────────────────────────────────────────────────────

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# ─── Directories ────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
BIN_DIR = PROJECT_DIR / "bin"
CONFIG_DIR = PROJECT_DIR / "clash-config"
BACKEND_DIR = SCRIPT_DIR
FRONTEND_DIR = PROJECT_DIR / "frontend"

# ─── Ports ──────────────────────────────────────────────────────────────────

LAUNCHER_CMD_PORT = int(os.getenv("LAUNCHER_CMD_PORT", "9099"))  # 关闭命令端口
MIHOMO_API_PORT = int(os.getenv("MIHOMO_API_PORT", "9090"))
MIHOMO_SOCKS_PORT = int(os.getenv("MIHOMO_SOCKS_PORT", "7890"))

# 全局关闭事件
_shutdown_event = None

# ─── Binary paths ───────────────────────────────────────────────────────────

def _find_mihomo_bin() -> Path | None:
    """Return the path to the mihomo binary for the current platform."""
    candidates = []
    if IS_WINDOWS:
        candidates = [BIN_DIR / "mihomo.exe", BIN_DIR / "mihomo.exe"]
    elif IS_LINUX:
        candidates = [BIN_DIR / "mihomo", Path("/usr/local/bin/mihomo")]

    for p in candidates:
        if p.exists():
            return p
    return None


# ─── Port helpers ───────────────────────────────────────────────────────────

def _port_open(host: str, port: int) -> bool:
    """Return True if a TCP port is listening."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, socket.timeout):
        return False


# ─── Launcher Command Server (HTTP) ─────────────────────────────────────────────

def _start_cmd_server(shutdown_event):
    """
    启动一个简单的 HTTP 服务器，监听关闭命令。
    GET /shutdown -> 触发关闭事件，返回 200 后 launcher 优雅退出
    GET /health   -> 返回 200 OK
    """
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/shutdown":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OK, shutting down")
                print("\n[launcher] Received /shutdown, initiating shutdown...")
                shutdown_event.set()
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 静默日志

    srv = HTTPServer(("127.0.0.1", LAUNCHER_CMD_PORT), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[launcher] Command server listening on http://127.0.0.1:{LAUNCHER_CMD_PORT}")
    return srv


def _wait_port(host: str, port: int, timeout: float = 20) -> bool:
    """Block until the port is open (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def _kill_port(port: int) -> None:
    """Kill the process holding a given TCP port (Windows + Linux)."""
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                f'netstat -ano | findstr ":{port} "',
                capture_output=True, text=True
            )
            for line in result.stdout.strip().splitlines():
                if "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pid = parts[-1]
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True)
                        print(f"[launcher] Killed PID {pid} on port {port}")
                        break
        else:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    except Exception as e:
        print(f"[launcher] _kill_port({port}) failed: {e}")


# ─── Mihomo lifecycle ───────────────────────────────────────────────────────

def _start_mihomo(config_path: Path) -> subprocess.Popen | None:
    """Start bundled mihomo and return its process handle."""
    bin_path = _find_mihomo_bin()
    if bin_path is None:
        print("[launcher] mihomo binary not found, skipping bundled mihomo")
        return None

    # Detect the API port from the config file so we monitor the right port
    api_port = _detect_api_port(config_path)

    if _port_open("127.0.0.1", api_port):
        print(f"[launcher] mihomo already running on port {api_port}, reusing")
        return None

    log_path = PROJECT_DIR / "mihomo.log"
    try:
        log_file = open(log_path, "w")
    except Exception as e:
        print(f"[launcher] Cannot open mihomo log {log_path}: {e}, using DEVNULL")
        log_file = subprocess.DEVNULL

    cmd = [
        str(bin_path),
        "-f", str(config_path),
        "-d", str(CONFIG_DIR),
    ]
    print(f"[launcher] Starting mihomo: {' '.join(cmd)}")
    p = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    if _wait_port("127.0.0.1", api_port, timeout=15):
        print(f"[launcher] mihomo ready on 127.0.0.1:{api_port} (PID {p.pid})")
        return p
    else:
        print(f"[launcher] mihomo failed to start within 15s, killing PID {p.pid}")
        p.terminate()
        p.wait(timeout=5)
        # 读取 mihomo 日志帮助调试
        if log_path.exists():
            content = log_path.read_text(errors="replace")
            print(f"[launcher] mihomo.log tail:\n{content[-1000:]}")
        return None


def _stop_mihomo() -> None:
    """Stop any mihomo process started by this launcher."""
    # Detect port from current config so we find the right process
    cfg_path = CONFIG_DIR / "config.yaml"
    api_port = _detect_api_port(cfg_path)
    if _port_open("127.0.0.1", api_port):
        print(f"[launcher] Stopping mihomo on port {api_port}...")
        # Find by port using netstat/findstr
        try:
            if IS_WINDOWS:
                result = subprocess.run(
                    f'netstat -ano | findstr ":{api_port} "',
                    capture_output=True, text=True
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if "LISTENING" in line and len(parts) >= 5:
                        pid = parts[-1]
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True)
                        print(f"[launcher] Killed mihomo PID {pid}")
                        break
            else:
                result = subprocess.run(
                    ["fuser", "-k", f"{api_port}/tcp"],
                    capture_output=True, text=True
                )
        except Exception as e:
            print(f"[launcher] Error stopping mihomo: {e}")


# ─── Backend lifecycle ───────────────────────────────────────────────────────

def _wait_port_free(host: str, port: int, timeout: float = 10) -> bool:
    """Block until the port is NOT open (useful after killing a process)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_open(host, port):
            return True
        time.sleep(0.3)
    return False


def _start_backend(env: dict) -> subprocess.Popen:
    """Start the FastAPI backend with a fresh log file."""
    # Guard: if something is already on 8080, kill it first to avoid
    # FastAPI binding failure and launcher restart-loop.
    if _port_open("0.0.0.0", 8080):
        print("[launcher] Port 8080 already occupied — killing stale process...")
        _kill_port(8080)
        _wait_port_free("0.0.0.0", 8080, timeout=8)

    log_path = PROJECT_DIR / "backend.log"
    try:
        out_file = open(log_path, "w", buffering=1)
        err_file = open(log_path, "a", buffering=1)
    except Exception as e:
        print(f"[launcher] Cannot open backend log {log_path}: {e}, using DEVNULL")
        out_file = err_file = subprocess.DEVNULL

    cmd = [sys.executable, "main.py"]
    print(f"[launcher] Starting backend: {' '.join(cmd)}")
    p = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=out_file,
        stderr=err_file,
    )
    print(f"[launcher] Backend PID {p.pid}, http://127.0.0.1:8080")
    return p


def _detect_api_port(cfg_path: Path) -> int:
    """
    Read the config file and extract the external-controller port.
    Falls back to MIHOMO_API_PORT if not found.
    """
    if not cfg_path.exists():
        return MIHOMO_API_PORT
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        ctrl = cfg.get("external-controller", "") or ""
        # Format: "0.0.0.0:9091" or ":9091"
        if ":" in ctrl:
            port_str = ctrl.rsplit(":", 1)[-1]
            return int(port_str)
    except Exception:
        pass
    return MIHOMO_API_PORT


# ─── Main ───────────────────────────────────────────────────────────────────

def _resolve_clash_url() -> str:
    """
    Decide which Clash API to use:
      1. CLASH_API_URL env var (highest priority)
      2. Bundled mihomo if it can be started (port auto-detected from config)
      3. Fallback: http://127.0.0.1:9090
    """
    env_url = os.getenv("CLASH_API_URL", "").strip()
    if env_url:
        print(f"[launcher] Using external CLASH_API_URL: {env_url}")
        return env_url

    # Try bundled mihomo
    default_config = CONFIG_DIR / "config.yaml"
    if not default_config.exists():
        # 镜像内默认配置可能被 volume mount 覆盖，自动生成一个
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        default_config.write_text(
            "mixed-port: 7890\n"
            "allow-lan: true\n"
            "mode: rule\n"
            "log-level: info\n"
            "external-controller: 0.0.0.0:9090\n"
            "dns:\n"
            "  enable: true\n"
            "  enhanced-mode: fake-ip\n"
            "  nameserver:\n"
            "    - 223.5.5.5\n"
            "    - 119.29.29.29\n"
        )
        print(f"[launcher] Generated default config at {default_config}")
    if default_config.exists():
        print(f"[launcher] config.yaml found, attempting to start bundled mihomo...")
        mihomo_proc = _start_mihomo(default_config)
        detected_port = _detect_api_port(default_config)
        if mihomo_proc is not None:
            url = f"http://127.0.0.1:{detected_port}"
            print(f"[launcher] Bundled mihomo started, CLASH_API_URL = {url}")
            return url
        if _port_open("127.0.0.1", detected_port):
            url = f"http://127.0.0.1:{detected_port}"
            print(f"[launcher] Bundled mihomo already running, CLASH_API_URL = {url}")
            return url
        print(f"[launcher] Bundled mihomo failed to start, falling back")

    # Fallback
    url = os.getenv("CLASH_API_URL", "http://127.0.0.1:9090")
    print(f"[launcher] No bundled mihomo, falling back to: {url}")
    return url


def _build_env(clash_url: str) -> dict:
    env = os.environ.copy()
    env["CLASH_API_URL"] = clash_url
    env["STATIC_DIR"] = str(FRONTEND_DIR)
    return env


def _signal_handler(signum, frame):
    sig = signal.Signals(signum).name
    print(f"\n[launcher] Received {sig}, shutting down...")
    _stop_mihomo()
    sys.exit(0)


def main():
    global _shutdown_event

    args = sys.argv[1:]

    if "--kill" in args:
        _stop_mihomo()
        print("[launcher] Done.")
        return

    skip_mihomo = "--no-mihomo" in args

    # 创建关闭事件
    import threading
    shutdown_event = threading.Event()
    _shutdown_event = shutdown_event

    # 启动命令服务器
    cmd_server = _start_cmd_server(shutdown_event)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _signal_handler)

    # Resolve Clash URL and build env
    clash_url = _resolve_clash_url() if not skip_mihomo else (
        os.getenv("CLASH_API_URL", "http://127.0.0.1:9090")
    )
    env = _build_env(clash_url)

    # Start backend
    backend_proc = _start_backend(env)

    # Monitor backend, restart if it dies (except on --kill)
    while True:
        try:
            # 使用 wait_for 支持 shutdown_event
            if IS_WINDOWS:
                # Windows 不支持 select，轮询检查
                while not shutdown_event.is_set():
                    rc = backend_proc.poll()
                    if rc is not None:
                        break
                    time.sleep(0.5)
                if shutdown_event.is_set():
                    break
            else:
                import select
                import fcntl
                import os
                # 设置 backend_proc.stdout 为非阻塞（DEVNULL 时 stdout 为 None，跳过）
                if backend_proc.stdout is not None:
                    fd = backend_proc.stdout.fileno()
                    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                while not shutdown_event.is_set():
                    rc = backend_proc.poll()
                    if rc is not None:
                        break
                    time.sleep(0.5)

            if shutdown_event.is_set():
                break

        except KeyboardInterrupt:
            break
        rc = backend_proc.poll()
        if rc is None:
            break  # Still running
        print(f"[launcher] Backend exited with {rc}, restarting in 3s...")
        time.sleep(3)
        backend_proc = _start_backend(env)

    # 优雅关闭
    print("[launcher] Shutting down...")
    _stop_mihomo()
    backend_proc.terminate()
    try:
        backend_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend_proc.kill()
    cmd_server.shutdown()
    print("[launcher] Exited.")


if __name__ == "__main__":
    main()
