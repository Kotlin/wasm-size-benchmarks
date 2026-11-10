"""Clone a benchmark project at its pinned revision, patch it for a given compiler variant, build the
production wasm, and collect the compileSync outputs (unoptimized + optimized) per variant."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from . import config
from . import variants as variants_mod
from .projects import Patch


def _repo_root(p):
    return config.CLONES / p.clone_dir


def _build_root(p):
    return _repo_root(p) / p.subdir


def _repo_rel(p, path):
    """A config path (relative to the gradle build root) -> path relative to the repo root."""
    return str(Path(p.subdir) / path) if p.subdir else path


def ensure_clone(p):
    root = _repo_root(p)
    if not root.exists():
        config.CLONES.mkdir(parents=True, exist_ok=True)
        config.log(f"cloning {p.repo} (blobless; first clone takes a while)")
        rc = config.stream(["git", "clone", "--filter=blob:none", "--no-checkout", p.repo, str(root)],
                           config.CLONES, config.WSB_HOME / f"clone-{p.name}.log")
        if rc != 0:
            shutil.rmtree(root, ignore_errors=True)
            config.log("blobless clone failed, retrying a full clone")
            rc = config.stream(["git", "clone", p.repo, str(root)],
                               config.CLONES, config.WSB_HOME / f"clone-{p.name}.log")
        if rc != 0:
            config.die(f"cloning {p.repo} failed, see {config.WSB_HOME}/clone-{p.name}.log")
    have = config.git("rev-parse", "--verify", f"{p.revision}^{{commit}}", cwd=root, check=False)
    if not have:
        config.log(f"fetching {p.name} @ {p.revision[:10]}")
        config.git("fetch", "origin", cwd=root, check=False)
        have = config.git("rev-parse", "--verify", f"{p.revision}^{{commit}}", cwd=root, check=False)
        if not have:
            config.die(f"pinned revision {p.revision} not found in {p.repo}")
    config.git("checkout", "-f", "--detach", p.revision, cwd=root)


def _reset_files(p):
    files = [_repo_rel(p, f) for f in p.patched_files()]
    config.git("checkout", "-f", "--", *files, cwd=_repo_root(p))


def _apply_patch(patch: Patch, variant, root: Path):
    if patch.when == "kgp-modern" and not variants_mod.kgp_modern(variant.deploy_version):
        return False
    if patch.when == "kgp-legacy" and variants_mod.kgp_modern(variant.deploy_version):
        return False
    path = root / patch.file
    text = path.read_text()
    if patch.append is not None:
        if patch.append.strip() in text:
            pass  # already applied (shouldn't happen after reset, but be safe)
        else:
            text += patch.append
    else:
        if patch.find not in text:
            config.die(f"patch for {patch.file} failed: {patch.find!r} not found "
                       f"(upstream file drifted from the pinned revision?)")
        text = text.replace(patch.find, patch.replace, 1)
    path.write_text(text)
    return True


def _set_version(p, variant, root: Path):
    import re
    for vf in p.version_files:
        path = root / vf
        text = path.read_text()
        if not re.search(p.version_re, text):
            config.die(f"no version line matching {p.version_re!r} in {path}")
        new = re.sub(p.version_re, p.version_replace.format(version=variant.deploy_version), text)
        path.write_text(new)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(p, variant, force=False):
    """Build project p against a compiler variant. Returns the output dir, or None when the
    project is not applicable to this variant (min_kotlin) — callers skip measurement then.
    Skippable/resumable via the meta.json fingerprint."""
    if p.min_kotlin:
        have_v = variants_mod.version_tuple(variant.deploy_version)
        need_v = variants_mod.version_tuple(p.min_kotlin)
        if have_v and need_v and have_v < need_v:
            config.log(f"{p.name}/{variant.label}: skipping, project needs Kotlin >= "
                       f"{p.min_kotlin} (variant is {variant.deploy_version})")
            return None
    out = config.BUILDS / p.name / variant.label
    meta_path = out / "meta.json"
    if meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text())
        if (meta.get("rev") == variant.rev
                and meta.get("dirty_hash", "") == (variant.dirty_hash or "")
                and meta.get("deploy_version") == variant.deploy_version):
            config.log(f"{p.name}/{variant.label}: build already done, skipping (--force to redo)")
            return out
    ensure_clone(p)
    root = _build_root(p)
    _reset_files(p)
    applied = []
    for patch in p.patches:
        if _apply_patch(patch, variant, root):
            applied.append(f"{patch.file}:{patch.when}")
    _set_version(p, variant, root)
    for w in p.wipe:
        shutil.rmtree(root / w.format(module=p.module), ignore_errors=True)

    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "build.log"
    log_path.unlink(missing_ok=True)
    config.log(f"{p.name}/{variant.label}: building with {variant.deploy_version}")
    cmd = ["./gradlew", *p.tasks, "-q", "--console=plain", "--no-configuration-cache"]
    rc = config.stream(cmd, root, log_path)
    config.log(f"{p.name}/{variant.label}: build rc={rc}")
    if rc != 0:
        tail = subprocess.run(["grep", "-E", "^e: |error:|FAILURE|What went wrong", "-A3",
                               str(log_path)], capture_output=True, text=True).stdout
        config.die(f"build of {p.name} with {variant.label} failed (rc={rc}), "
                  f"full log: {log_path}\n{tail[:2000]}")

    # sanity: the log should name the expected compiler somewhere
    log_text = log_path.read_text(errors="replace")
    if variant.deploy_version not in log_text:
        config.warn(f"{p.name}/{variant.label}: build log does not mention "
                    f"{variant.deploy_version}; check which compiler actually ran")

    src = root / p.module / "build" / "compileSync"
    if not src.exists():
        config.die(f"no compileSync output at {src} — check artifact paths for {p.name}")
    if (out / "compileSync").exists():
        shutil.rmtree(out / "compileSync")
    shutil.copytree(src, out / "compileSync")
    wasms = sorted(str(f.relative_to(out)) for f in out.glob("compileSync/**/*.wasm"))
    meta = {
        "project": p.name, "variant": variant.label, "kind": variant.kind, "rev": variant.rev,
        "dirty_hash": variant.dirty_hash, "deploy_version": variant.deploy_version,
        "project_revision": p.revision, "patches_applied": applied,
        "wasms": {w: _sha256(out / w) for w in wasms},
        "gradle": " ".join(p.tasks),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return out
