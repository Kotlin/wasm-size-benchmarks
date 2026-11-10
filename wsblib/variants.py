"""Compiler "variants".

A variant is one build of the Kotlin compiler to compare, given by a *spec*:

    2.2.21            a released version, resolved from Maven Central (nothing to build)
    <sha|HEAD|master|...>  a revision of the main Kotlin repo: gets a linked worktree and
                      is installed to ~/.m2 as <base>-<label>-SNAPSHOT (label = short sha)
    dirty | .         the main Kotlin working tree exactly as it is, uncommitted changes
                      included (installed in place, labelled wt-<shortsha>)
    maven:2.5.255-kc-SNAPSHOT  an already-installed ~/.m2 snapshot, reused as-is

Variants are registered in WSB_HOME/variants.json so install steps can be skipped on re-runs
(label is pinned to a revision; incremental gradle builds make re-installs cheap).
"""

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

RELEASED_RE = re.compile(r"^\d+\.\d+\.\d+$")
LEGACY_KGP_PREFIXES = ("1.", "2.0.", "2.1.", "2.2.")


@dataclass
class Variant:
    label: str
    kind: str  # released | commit | dirty | maven
    spec: str
    rev: str = ""  # resolved sha ("" for maven/released)
    deploy_version: str = ""  # version string written into the projects' libs.versions.toml
    source_dir: str = ""  # directory to run `./gradlew install` in
    dirty_hash: str = ""  # fingerprint of uncommitted state (dirty variants only)
    install_done: bool = False


def load_registry():
    if config.VARIANTS_JSON.exists():
        return json.loads(config.VARIANTS_JSON.read_text())
    return {}


def save_registry(reg):
    config.VARIANTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.VARIANTS_JSON.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


def kgp_modern(version: str) -> bool:
    """KGP renamed BinaryenExec.binaryenArgs -> binaryenArguments after the 2.2.x line."""
    return not version.startswith(LEGACY_KGP_PREFIXES)


def version_tuple(version: str):
    """(major, minor, patch) of a version string, ignoring any -suffix; None if unparsable
    (unparsable variant versions are treated as 'new enough', never skipped)."""
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def _rev_parse(rev):
    def try_rev():
        return config.git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=config.KOTLIN_REPO,
                          check=False)
    sha = try_rev()
    if not sha:
        config.log(f"revision {rev} not local, fetching origin")
        config.git("fetch", "origin", cwd=config.KOTLIN_REPO, check=False)
        sha = try_rev()
    if not sha:
        config.die(f"cannot resolve revision '{rev}' in {config.KOTLIN_REPO}")
    return sha


def dirty_hash():
    diff = config.git("diff", "HEAD", cwd=config.KOTLIN_REPO, check=False)
    untracked = config.git("ls-files", "--others", "--exclude-standard", cwd=config.KOTLIN_REPO)
    h = hashlib.sha256()
    h.update(diff.encode())
    h.update(b"\0")
    h.update(untracked.encode())
    return h.hexdigest()[:12]


def resolve(spec: str, label: str = None) -> Variant:
    if spec.startswith("maven:"):
        version = spec[len("maven:"):]
        if not version:
            config.die("maven: spec needs a version, e.g. maven:2.5.255-kc-SNAPSHOT")
        return Variant(label or f"maven-{version}", "maven", spec, deploy_version=version)
    if spec in ("dirty", ".", "worktree"):
        rev = config.git("rev-parse", "HEAD", cwd=config.KOTLIN_REPO)
        return Variant(label or f"wt-{rev[:10]}", "dirty", spec, rev=rev,
                       dirty_hash=dirty_hash(), source_dir=str(config.KOTLIN_REPO))
    if RELEASED_RE.match(spec):
        return Variant(label or spec, "released", spec, deploy_version=spec)
    rev = _rev_parse(spec)
    return Variant(label or rev[:10], "commit", spec, rev=rev)


def _check_consistency(variant, prev):
    """A label is pinned to one revision; refuse silent reuse for a different state.
    Dirty variants are exempt from the dirty_hash check: their working-tree content is
    *expected* to change between runs — that's the iterate-on-your-change workflow;
    install() notices and reinstalls."""
    if variant.rev and prev.get("rev") and prev["rev"] != variant.rev:
        config.die(f"label '{variant.label}' is already registered for {prev['rev'][:10]} but "
                   f"the spec now resolves to {variant.rev[:10]}. Pick a different --label or "
                   f"clean the registry (see 'wsb clean').")
    if variant.kind != "dirty" and prev.get("dirty_hash") and variant.dirty_hash \
            and prev["dirty_hash"] != variant.dirty_hash:
        config.die(f"variant '{variant.label}' was registered with different uncommitted "
                   f"changes (was {prev['dirty_hash']}, now {variant.dirty_hash}). "
                   f"Use a new --label.")


def snapshot_base(source_dir: Path) -> str:
    """Base version for -SNAPSHOT labels, read from the variant's own gradle.properties
    (currently defaultSnapshotVersion=2.5.255-SNAPSHOT)."""
    text = (source_dir / "gradle.properties").read_text()
    m = re.search(r"(?m)^defaultSnapshotVersion=(\S+)", text)
    if not m:
        config.die(f"no defaultSnapshotVersion in {source_dir}/gradle.properties")
    return m.group(1).removesuffix("-SNAPSHOT")


def ensure_worktree(variant: Variant):
    wt = config.WORKTREES / variant.label
    if wt.exists():
        cur = config.git("rev-parse", "HEAD", cwd=wt)
        if cur != variant.rev:
            config.die(f"worktree {wt} is at {cur[:10]}, expected {variant.rev[:10]} "
                       f"(label reuse across revisions is not allowed)")
        if config.git("status", "--porcelain", cwd=wt, check=False):
            config.warn(f"worktree {wt} has local modifications; building with them")
        variant.source_dir = str(wt)
        return
    config.log(f"creating worktree {variant.label} at {variant.rev[:12]}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    config.git("worktree", "add", "--detach", str(wt), variant.rev, cwd=config.KOTLIN_REPO)
    variant.source_dir = str(wt)


def _m2_marker(version):
    return Path.home() / ".m2" / "repository" / "org" / "jetbrains" / "kotlin" \
        / "kotlin-gradle-plugin" / version


def install(variant: Variant, force=False) -> Variant:
    """Make the variant buildable by the benchmark projects: nothing for released/maven
    variants, `./gradlew install` to ~/.m2 for commit/dirty variants. Idempotent per
    (label, rev); a dirty variant whose working-tree content changed is reinstalled
    (incremental gradle)."""
    reg = load_registry()
    prev = reg.get(variant.label)
    if prev:
        _check_consistency(variant, prev)
    dirty_changed = (variant.kind == "dirty" and prev is not None
                     and prev.get("dirty_hash") != variant.dirty_hash)
    if dirty_changed:
        config.log(f"dirty variant {variant.label}: working tree changed since the last "
                   "install, reinstalling")

    if variant.kind in ("released", "maven"):
        variant.install_done = True
        config.log(f"variant {variant.label}: using "
                   f"{'Maven Central' if variant.kind == 'released' else '~/.m2'} "
                   f"version {variant.deploy_version}, nothing to build")
    else:
        if variant.kind == "commit":
            ensure_worktree(variant)
        # dirty variants build in place in the main working tree
        variant.deploy_version = \
            f"{snapshot_base(Path(variant.source_dir))}-{variant.label}-SNAPSHOT"
        already = bool(prev and prev.get("install_done")) \
            or _m2_marker(variant.deploy_version).exists()
        if already and not dirty_changed and not force:
            config.log(f"variant {variant.label}: install already done "
                       f"({variant.deploy_version})")
            variant.install_done = True
        else:
            config.log(f"installing {variant.label} as {variant.deploy_version} "
                       f"(this takes a while; log: "
                       f"{config.WSB_HOME}/install-{variant.label}.log)")
            cmd = ["./gradlew", "install",
                   f"-PdeployVersion={variant.deploy_version}",
                   f"-Pbuild.number={variant.deploy_version}",
                   # fresh worktrees have no local.properties, and the repo's gradle.properties
                   # enables the internal setup plugin whose consent prompt blocks a
                   # non-interactive build forever (it reads stdin; stdout is captured here)
                   "-Pkotlin.build.internal.gradle.setup=false",
                   "-q", "--console=plain"]
            rc = config.stream(cmd, variant.source_dir,
                               config.WSB_HOME / f"install-{variant.label}.log")
            config.log(f"install {variant.label} rc={rc}")
            if rc != 0:
                log_path = config.WSB_HOME / f"install-{variant.label}.log"
                tail = subprocess.run(["tail", "-25", str(log_path)],
                                      capture_output=True, text=True)
                config.die(f"install of variant {variant.label} failed (rc={rc}):\n{tail.stdout}")
            variant.install_done = True

    reg[variant.label] = asdict(variant)
    save_registry(reg)
    return variant


def registered(label: str) -> Variant:
    prev = load_registry().get(label)
    if not prev:
        config.die(f"unknown variant label '{label}' (see 'wsb status' or install it first)")
    return Variant(**prev)
