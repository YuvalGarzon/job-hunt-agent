"""Install (or remove) the background worker: it starts when you log in and restarts if it stops.

Usage (from the project folder, with the project's Python):
    python scripts/install_worker.py            # install or reinstall
    python scripts/install_worker.py --remove   # uninstall

  macOS    a launchd agent    ~/Library/LaunchAgents/com.job-hunt-agent.worker.plist
  Linux    a systemd user service    ~/.config/systemd/user/job-hunt-agent.service
  Windows  a Task Scheduler task "job-hunt-agent" (at log on, restarts on failure)
The worker writes its log to ~/.job-hunt-agent/logs/worker.log on every system.
"""
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
LOGS = Path.home() / ".job-hunt-agent" / "logs"
NAME = "job-hunt-agent"
LABEL = "com.job-hunt-agent.worker"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
UNIT = Path.home() / ".config" / "systemd" / "user" / f"{NAME}.service"


def python() -> str:
    """The project's Python; on Windows the windowless pythonw.exe next to it."""
    exe = Path(sys.executable)
    if platform.system() == "Windows" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def run(cmd: list, check: bool = True) -> int:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r.returncode


# --- macOS -----------------------------------------------------------------------------------
def mac(remove: bool) -> None:
    import os
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    if remove:
        PLIST.unlink(missing_ok=True)
        return
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{escape(python())}</string><string>-u</string><string>-m</string><string>agent.worker</string></array>
  <key>WorkingDirectory</key><string>{escape(str(ROOT))}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>{home}</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{LOGS}/worker.service.log</string>
  <key>StandardErrorPath</key><string>{LOGS}/worker.service.log</string>
</dict>
</plist>
""", encoding="utf-8")
    run(["plutil", "-lint", str(PLIST)])
    run(["launchctl", "bootstrap", domain, str(PLIST)])


# --- Linux -----------------------------------------------------------------------------------
def linux(remove: bool) -> None:
    run(["systemctl", "--user", "disable", "--now", f"{NAME}.service"], check=False)
    if remove:
        UNIT.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], check=False)
        return
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(f"""[Unit]
Description=job-hunt-agent worker
After=network-online.target

[Service]
WorkingDirectory={ROOT}
ExecStart="{python()}" -u -m agent.worker
Environment=PATH={Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""", encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"])


# --- Windows ---------------------------------------------------------------------------------
def windows(remove: bool) -> None:
    run(["schtasks", "/Delete", "/TN", NAME, "/F"], check=False)
    if remove:
        return
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>job-hunt-agent worker</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python())}</Command>
      <Arguments>-u -m agent.worker</Arguments>
      <WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(xml)
    try:
        run(["schtasks", "/Create", "/TN", NAME, "/XML", f.name, "/F"])
    finally:
        Path(f.name).unlink(missing_ok=True)
    run(["schtasks", "/Run", "/TN", NAME], check=False)


def installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return PLIST.exists()
    if system == "Linux":
        return UNIT.exists()
    if system == "Windows":
        return run(["schtasks", "/Query", "/TN", NAME], check=False) == 0
    return False


def main() -> None:
    remove = "--remove" in sys.argv
    LOGS.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    fn = {"Darwin": mac, "Linux": linux, "Windows": windows}.get(system)
    if not fn:
        sys.exit(f"{system} is not supported: run `python -m agent.worker` yourself")
    fn(remove)
    print("removed" if remove else f"installed: the worker runs in the background (log: {LOGS / 'worker.log'})")


if __name__ == "__main__":
    main()
