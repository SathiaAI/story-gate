"""Console entry point for `story-gate`.

`story-gate init` (and any command before story-gate is installed on this computer) runs the copy in this package.
Once the verified runtime is installed, every other command goes to it, so the AI and you always run the verified copy.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def payload_dir() -> Path:
    return Path(__file__).resolve().parent / "_payload"


def runtime_launcher():
    """The installed runtime's launcher, or None (same folder rules as sg_github.config_dir)."""
    home = os.environ.get("STORY_GATE_HOME")
    if not home:
        base = os.environ.get("APPDATA") if os.name == "nt" else (os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        home = os.path.join(base or os.path.expanduser("~"), "story-gate")
    launch = Path(home) / "runtime" / "launch.py"
    try:
        json.loads((launch.parent / "active.json").read_text(encoding="utf-8"))["dir"]
    except Exception:
        return None
    return launch if launch.is_file() else None


def main() -> None:
    args = sys.argv[1:]
    launch = runtime_launcher()
    if launch and (args[:1] or ["help"])[0] not in ("init", "install"):
        cmd = [sys.executable, "-I", str(launch), *args]
    else:
        cmd = [sys.executable, str(payload_dir() / "gate.py"), *args]
    try:
        rc = subprocess.call(cmd)
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
