"""Systemd service management for hive-slack.

Manages the bot as a systemd --user service for persistent background operation.
Based on the pattern from amplifier-app-log-viewer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ServiceStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


@dataclass
class ServiceInfo:
    status: ServiceStatus
    pid: int | None = None
    message: str = ""
    service_file: str = ""


SERVICE_NAME = "hive-slack"
UNIT_FILE = f"{SERVICE_NAME}.service"


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _unit_path() -> Path:
    return _systemd_dir() / UNIT_FILE


def _run_systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _find_executable() -> str:
    """Find the hive-slack executable path."""
    # Check if installed via pip/uv (entry point script)
    which = shutil.which("hive-slack")
    if which:
        return which
    # Fallback: run as python module
    return f"{sys.executable} -m hive_slack.main"


def install(config_path: str, env_file: str | None = None) -> ServiceInfo:
    """Install the systemd user service."""
    systemd_dir = _systemd_dir()
    systemd_dir.mkdir(parents=True, exist_ok=True)

    exec_start = _find_executable()
    abs_config = str(Path(config_path).resolve())

    # If executable is a python -m command, use it directly
    if exec_start.startswith("/"):
        exec_line = f"{exec_start} {abs_config}"
    else:
        exec_line = exec_start.replace(
            "hive_slack.main", f"hive_slack.main {abs_config}"
        )

    # Build the unit file
    lines = [
        "[Unit]",
        "Description=Amplifier Hive Slack Connector",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_line}",
        "Restart=on-failure",
        "RestartSec=10",
        f"WorkingDirectory={Path.cwd()}",
    ]

    # Add environment file if specified
    if env_file:
        abs_env = str(Path(env_file).resolve())
        lines.append(f"EnvironmentFile={abs_env}")
    else:
        # Default: look for .env in the project directory
        default_env = Path.cwd() / ".env"
        if default_env.exists():
            lines.append(f"EnvironmentFile={default_env.resolve()}")

    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=default.target",
        ]
    )

    unit_content = "\n".join(lines) + "\n"
    unit_path = _unit_path()
    unit_path.write_text(unit_content)

    _run_systemctl("daemon-reload")
    _run_systemctl("enable", UNIT_FILE)

    return ServiceInfo(
        status=ServiceStatus.STOPPED,
        message=f"Installed at {unit_path}",
        service_file=str(unit_path),
    )


def uninstall() -> ServiceInfo:
    """Stop, disable, and remove the systemd service."""
    try:
        stop()
    except Exception:
        pass

    try:
        _run_systemctl("disable", UNIT_FILE, check=False)
    except Exception:
        pass

    unit_path = _unit_path()
    if unit_path.exists():
        unit_path.unlink()

    _run_systemctl("daemon-reload", check=False)

    return ServiceInfo(
        status=ServiceStatus.NOT_INSTALLED,
        message="Service uninstalled",
    )


def start() -> ServiceInfo:
    """Start the service."""
    _run_systemctl("start", UNIT_FILE)
    return status()


def stop() -> ServiceInfo:
    """Stop the service."""
    _run_systemctl("stop", UNIT_FILE)
    return status()


def restart() -> ServiceInfo:
    """Restart the service."""
    _run_systemctl("restart", UNIT_FILE)
    return status()


def status() -> ServiceInfo:
    """Get current service status."""
    unit_path = _unit_path()
    if not unit_path.exists():
        return ServiceInfo(
            status=ServiceStatus.NOT_INSTALLED, message="Service not installed"
        )

    result = _run_systemctl(
        "show",
        UNIT_FILE,
        "--property=ActiveState,MainPID,SubState",
        check=False,
    )

    props = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            props[key] = val

    active_state = props.get("ActiveState", "unknown")
    pid_str = props.get("MainPID", "0")
    pid = int(pid_str) if pid_str.isdigit() and int(pid_str) > 0 else None

    if active_state == "active":
        return ServiceInfo(
            status=ServiceStatus.RUNNING,
            pid=pid,
            message=f"Running (PID {pid})",
            service_file=str(unit_path),
        )
    elif active_state == "failed":
        return ServiceInfo(
            status=ServiceStatus.FAILED,
            message="Service failed -- check logs with: hive-slack service logs",
            service_file=str(unit_path),
        )
    else:
        return ServiceInfo(
            status=ServiceStatus.STOPPED,
            message="Stopped",
            service_file=str(unit_path),
        )


def logs(follow: bool = False, lines: int = 50) -> None:
    """Show service logs from journald."""
    cmd = [
        "journalctl",
        "--user",
        "-u",
        UNIT_FILE,
        f"-n{lines}",
        "--no-pager",
    ]
    if follow:
        cmd.append("-f")
        # Use exec so Ctrl+C works properly
        os.execvp("journalctl", cmd)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
