#!/usr/bin/env python3
"""wsb — Kotlin/Wasm compiler size benchmark harness.

Compares the binary size impact of a Kotlin compiler change across a set of real-world
Wasm projects, deterministically: projects are pinned by commit, every variant's compiler is
installed under a unique -SNAPSHOT version, and the "backend only" measurement always uses
the same pinned Binaryen + argument list.

Typical use (compare your working tree against HEAD):

    ./wsb.py run                                   # baseline=HEAD, change=<working tree>
    ./wsb.py run --baseline 26b01b14f72b --change ee5eff663cbc
    ./wsb.py run --variants 2.2.21,ee5eff663cbc^,ee5eff663cbc    # timeline

See README.md for the method, variant specs, and how to add projects.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wsblib import build, config, measure, projects, report  # noqa: E402
from wsblib import variants  # noqa: E402


def _split(csv):
    return [x for x in (csv or "").split(",") if x]


def _select_projects(names_csv):
    names = _split(names_csv) or [p.name for p in projects.PROJECTS]
    return [projects.get(n) for n in names]


def _resolve_variants(specs, labels):
    if not specs:
        config.die("no compiler variants given")
    if labels and len(labels) != len(specs):
        config.die("number of labels must match number of variants")
    return [variants.resolve(s, labels[i] if labels else None) for i, s in enumerate(specs)]


def cmd_run(args):
    if args.variants:
        specs = _split(args.variants)
        labels = None
    else:
        specs = [args.baseline or "HEAD", args.change or "dirty"]
        labels = [args.baseline_label, args.change_label]
    variant_list = _resolve_variants(specs, labels)
    projs = _select_projects(args.projects)
    config.log(f"run: variants [{' vs '.join(v.label for v in variant_list)}], "
               f"projects {[p.name for p in projs]}")

    for i, variant in enumerate(variant_list):
        if args.skip_install and variant.kind not in ("released", "maven"):
            variant = variants.registered(variant.label)  # must already exist
        else:
            variant = variants.install(variant, force=args.force)
        variant_list[i] = variant  # keep the resolved version/source_dir for build+measure
        for p in projs:
            build.build(p, variant, force=args.force)

    for variant in variant_list:
        for p in projs:
            measure.measure(p, variant, with_names=not args.no_names, force=args.force)

    report.compare([p.name for p in projs], [v.label for v in variant_list])


def cmd_install(args):
    for variant in _resolve_variants(args.specs, args.labels):
        variants.install(variant, force=args.force)


def cmd_build(args):
    projs = _select_projects(args.projects)
    for label in args.labels:
        variant = variants.registered(label)
        for p in projs:
            build.build(p, variant, force=args.force)


def cmd_measure(args):
    projs = _select_projects(args.projects)
    for label in args.labels:
        variant = variants.registered(label)
        for p in projs:
            measure.measure(p, variant, with_names=not args.no_names, force=args.force)


def cmd_compare(args):
    report.compare([p.name for p in _select_projects(args.projects)], args.labels)


def cmd_funcdiff(args):
    p = projects.get(args.project)
    res = {}
    for label in (args.a, args.b):
        m = measure.load(p, label)
        res[label] = m
    key = args.measurement
    if key not in res[args.a]["files"] or key not in res[args.b]["files"]:
        config.die(f"measurement keys available: {list(res[args.a]['files'])}")
    print(measure.funcdiff(res[args.a]["files"][key], res[args.b]["files"][key],
                           depth=args.depth, top=args.top))


def cmd_refresh_args(args):
    kotlin = Path(args.kotlin) if args.kotlin else config.KOTLIN_REPO
    src = kotlin / "wasm/wasm.config/src/org/jetbrains/kotlin/platform/wasm/BinaryenConfig.kt"
    if not src.exists():
        config.die(f"{src} not found (pass --kotlin <repo>)")
    text = src.read_text()
    # line comments can quote argument names (e.g. '// without "--type-merging" ...'); drop them
    text = re.sub(r"(?m)//.*$", "", text)

    def literals_after(marker):
        i = text.index(marker)
        j = text.index("listOf(", i) + len("listOf(")
        depth, k = 1, j
        while depth:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        return re.findall(r'"([^"]*)"', text[j:k - 1])

    args_list = literals_after("binaryenCommonArgs") + literals_after("val binaryenArgs")
    rev = config.git("rev-parse", "--short", "HEAD", cwd=kotlin, check=False) or "?"
    header = (f"# Pinned Binaryen args for the 'backend only' measurement (single-module\n"
              f"# pipeline). Generated by 'wsb refresh-args' from\n"
              f"#   {src.relative_to(kotlin)} @ {rev}\n"
              f"# DO NOT EDIT: regenerate instead, so every variant keeps the same args.\n")
    config.BINARYEN_ARGS_FILE.write_text(header + "\n".join(args_list) + "\n")
    config.log(f"wrote {config.BINARYEN_ARGS_FILE}: {len(args_list)} args")


def cmd_status(args):
    config.WSB_HOME.mkdir(parents=True, exist_ok=True)
    reg = variants.load_registry()
    print("Compiler variants:")
    for label, a in sorted(reg.items()):
        print(f"  {label:<22} {a['kind']:<9} {a.get('deploy_version', '?'):<40} "
              f"rev={a.get('rev', '')[:12]} install={'done' if a.get('install_done') else 'NO'}")
    print("Clones:")
    for p in projects.PROJECTS:
        d = config.CLONES / p.clone_dir
        if d.exists():
            rev = config.git("rev-parse", "HEAD", cwd=d, check=False)[:12] or "?"
            print(f"  {p.name:<22} {rev} (want {p.revision[:12]})")
        else:
            print(f"  {p.name:<22} (not cloned)")
    print("Builds:")
    if config.BUILDS.exists():
        for d in sorted(config.BUILDS.iterdir()):
            for a in sorted(d.iterdir()):
                m = (a / "measure.json").exists()
                print(f"  {d.name}/{a.name:<24} build=done measure={'done' if m else 'no'}")
    print(f"Binaryen: {config.binaryen_path()}")
    print(f"Args file: {config.BINARYEN_ARGS_FILE}"
          + ("" if config.BINARYEN_ARGS_FILE.exists() else " (MISSING: run wsb refresh-args)"))
    print(f"Home: {config.WSB_HOME}")


def cmd_clean(args):
    def rm(path, what):
        if path.exists():
            if args.force or input(f"remove {path}? [y/N] ").lower().startswith("y"):
                shutil.rmtree(path)
                config.log(f"removed {what}")
        else:
            print(f"{what}: nothing to remove")

    if args.worktrees:
        rm(config.WORKTREES, "worktrees")
        config.git("worktree", "prune", cwd=config.KOTLIN_REPO, check=False)
    if args.clones:
        rm(config.CLONES, "project clones")
    if args.builds:
        rm(config.BUILDS, "build outputs")
    if args.results:
        rm(config.RESULTS, "results")
    if not (args.worktrees or args.clones or args.builds or args.results):
        print("nothing to do: pass --worktrees/--clones/--builds/--results")


def _acquire_lock():
    """Only one mutating wsb invocation at a time. This is not cosmetic: a run once started
    while another was mid-install, consumed its partially-published ~/.m2 artifacts, and
    produced tainted builds. The lock releases automatically when the process exits."""
    import fcntl
    config.WSB_HOME.mkdir(parents=True, exist_ok=True)
    lock_path = config.WSB_HOME / "wsb.lock"
    # O_RDWR without O_TRUNC: a contender must not wipe the holder's pid while probing
    fh = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT), "r+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        pid = fh.read().strip() or "?"
        config.die(f"another wsb invocation is running (pid {pid}); concurrent runs corrupt "
                   f"~/.m2 and build state. If none is running, remove {lock_path}.")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # kept open for the process lifetime


def main():
    ap = argparse.ArgumentParser(prog="wsb", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="install variants, build+measure projects, compare")
    p.add_argument("--baseline", help="baseline variant spec (default HEAD)")
    p.add_argument("--change", help="change variant spec (default 'dirty' = working tree)")
    p.add_argument("--variants", help="comma-separated compiler variant specs, first = baseline (timeline)")
    p.add_argument("--baseline-label")
    p.add_argument("--change-label")
    p.add_argument("--projects", help="comma-separated subset of projects")
    p.add_argument("--force", action="store_true", help="redo installs/builds/measures")
    p.add_argument("--skip-install", action="store_true",
                   help="use already-registered variants, don't build compilers")
    p.add_argument("--no-names", action="store_true", help="skip name-section analysis")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("install", help="build a compiler variant into ~/.m2")
    p.add_argument("specs", nargs="+", help="compiler variant specs")
    p.add_argument("--labels", help="comma-separated variant labels (one per spec)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("build", help="build projects against a registered variant")
    p.add_argument("labels", nargs="+")
    p.add_argument("--projects")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("measure", help="measure built projects for a registered variant")
    p.add_argument("labels", nargs="+")
    p.add_argument("--projects")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-names", action="store_true")
    p.set_defaults(fn=cmd_measure)

    p = sub.add_parser("compare", help="render comparison report")
    p.add_argument("labels", nargs="+", help="variant labels, first = baseline")
    p.add_argument("--projects")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("funcdiff", help="per-function code-size diff between two variants")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--project", required=True)
    p.add_argument("--measurement", default="backend-only",
                   help="unoptimized | backend-only | as-shipped (needs names; default backend-only)")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(fn=cmd_funcdiff)

    p = sub.add_parser("refresh-args", help="regenerate binaryen_args.txt from BinaryenConfig.kt")
    p.add_argument("--kotlin", help="path to the Kotlin repo (default: parent of this harness)")
    p.set_defaults(fn=cmd_refresh_args)

    p = sub.add_parser("status", help="show variants, clones, builds, tools")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("clean", help="remove cached state")
    for what in ("worktrees", "clones", "builds", "results"):
        p.add_argument(f"--{what}", action="store_true")
    p.add_argument("--force", action="store_true", help="don't ask")
    p.set_defaults(fn=cmd_clean)

    args = ap.parse_args()
    config.check_tools()
    lock = _acquire_lock() if args.cmd not in ("status", "compare", "funcdiff") else None
    args.fn(args)


if __name__ == "__main__":
    main()
