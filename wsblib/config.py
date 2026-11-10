"""Shared configuration: paths, tool discovery, subprocess helpers.

Everything the harness writes (clones, worktrees, build outputs, reports) lives under
WSB_HOME (default ~/.cache/wasm-size-bench, override with the WSB_HOME env var) so the
Kotlin repo stays clean. The Kotlin repo itself is the parent of this harness directory
unless WSB_KOTLIN is set.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parents[1]
KOTLIN_REPO = Path(os.environ.get("WSB_KOTLIN", str(HARNESS_DIR.parent))).resolve()
WSB_HOME = Path(os.environ.get("WSB_HOME", str(Path.home() / ".cache" / "wasm-size-bench")))

CLONES = WSB_HOME / "clones"
WORKTREES = WSB_HOME / "worktrees"
BUILDS = WSB_HOME / "builds"
RESULTS = WSB_HOME / "results"
VARIANTS_JSON = WSB_HOME / "variants.json"
BINARYEN_ARGS_FILE = HARNESS_DIR / "binaryen_args.txt"

# The Binaryen version the "backend only" measurement is pinned to. Keep in sync with what
# current KGP uses (wasm/wasm.config/.../BinaryenConfig.kt era); override with WSB_BINARYEN_VERSION
# or point WSB_BINARYEN at a specific wasm-opt executable.
DEFAULT_BINARYEN_VERSION = "133"


def log(msg):
    print(f"=== {msg} [{time.strftime('%H:%M:%S')}]", flush=True)


def die(msg):
    sys.exit(f"wsb: error: {msg}")


def warn(msg):
    print(f"wsb: warning: {msg}", flush=True)


def need(name):
    path = shutil.which(name)
    if not path:
        die(f"required tool '{name}' not found on PATH")
    return path


def git(*args, cwd=None, check=True):
    """Run git, return stdout (stripped)."""
    res = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if check and res.returncode != 0:
        die(f"git {' '.join(args)} failed in {cwd}:\n{res.stderr.strip()}")
    return res.stdout.strip()


def stream(cmd, cwd, log_path):
    """Run cmd with cwd, appending stdout+stderr to log_path AS IT HAPPENS — long gradle
    runs can then be followed with `tail -f`, and interactive prompts end up in the log
    immediately instead of hiding in a buffer until the process exits. Returns exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as logf:  # truncate: a log must reflect exactly one command run
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for line in proc.stdout:
            logf.write(line)
            logf.flush()
        proc.wait()
    return proc.returncode


def check_tools():
    for t in ("wasm-tools", "brotli", "git"):
        need(t)
    binaryen_path()


def binaryen_path():
    """Path to the pinned wasm-opt. Deterministic: the same Binaryen must be used for the
    'backend only' measurement of every variant, or the comparison is meaningless."""
    explicit = os.environ.get("WSB_BINARYEN")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            die(f"WSB_BINARYEN={explicit} does not exist")
        return p
    root = Path.home() / ".gradle" / "binaryen"
    candidates = sorted(
        (d for d in root.glob("binaryen-version_*") if d.is_dir()),
        key=lambda d: d.name,
    )
    if not candidates:
        die("no Binaryen found under ~/.gradle/binaryen (build a Kotlin/Wasm project once, "
            "or set WSB_BINARYEN to a wasm-opt executable)")
    wanted = f"binaryen-version_{os.environ.get('WSB_BINARYEN_VERSION', DEFAULT_BINARYEN_VERSION)}"
    chosen = next((d for d in candidates if d.name == wanted), None)
    if chosen is None:
        warn(f"pinned Binaryen {wanted} not found; falling back to {candidates[-1].name} "
             "(results will NOT be comparable with runs that used a different Binaryen)")
        chosen = candidates[-1]
    return chosen / "bin" / "wasm-opt"
