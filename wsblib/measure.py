"""Measure one (project, variant): the three measurements from wasm-size-since-beta.org.

  1. unoptimized  — compiler output before Binaryen (compileSync .../kotlin/*.wasm)
  2. backend only — the SAME pinned Binaryen + args run on every variant's unoptimized output
                    (isolates the backend change from KGP toolchain changes)
  3. as shipped   — each KGP's own optimized output (compileSync .../optimized/*.wasm)

All sizes are taken after `wasm-tools strip --all` (custom sections carry no code).
Compressed sizes matter most: raw deltas exaggerate wins that are just string literals
moving from the data section to import names.
"""

import gzip
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from . import config

SECTIONS = ("types", "imports", "globals", "code", "data")
NAME_PATTERNS = {
    "lambda": r"\$lambda",
    "suspend-lambda / coroutine": r"(?i)(suspendlambda|\$slambda|continuationimpl|coroutineimpl)",
    "callable ref": r"(?i)(\$ref|functionreference|kfunctionimpl)",
}


def _pick(p, variant, which):
    out = config.BUILDS / p.name / variant.label
    files = [f for f in out.glob(p.artifacts[which])
             if not any(x in f.name for x in p.artifact_exclude)]
    if not files:
        config.die(f"{p.name}/{variant.label}: no wasm matching {p.artifacts[which]} under {out}")
    if len(files) > 1:
        config.warn(f"{p.name}/{variant.label}: {len(files)} non-excluded wasm files for "
                    f"{which!r}, taking the largest: {[f.name for f in files]}")
    return max(files, key=lambda f: f.stat().st_size)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def binaryen_args():
    """The pinned arg list (wsb refresh-args regenerates it from BinaryenConfig.kt)."""
    if not config.BINARYEN_ARGS_FILE.exists():
        config.die(f"{config.BINARYEN_ARGS_FILE} missing — run 'wsb refresh-args' first")
    return [l.strip() for l in config.BINARYEN_ARGS_FILE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def _run_binaryen(inp: Path, outp: Path):
    """Same Binaryen + same args on every variant (plus -g: keep names for analysis)."""
    sidecar = outp.with_suffix(outp.suffix + ".input.sha256")
    digest = _sha256(inp)
    if outp.exists() and sidecar.exists() and sidecar.read_text().strip() == digest:
        return outp
    outp.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.binaryen_path(), str(inp), "-o", str(outp), "-g", *binaryen_args()]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        config.die(f"binaryen failed on {inp}:\n{res.stderr[:2000]}")
    sidecar.write_text(digest + "\n")
    return outp


def _stripped(f: Path) -> Path:
    out = f.with_name(f.name + ".stripped")
    if not out.exists() or out.stat().st_mtime < f.stat().st_mtime:
        subprocess.run(["wasm-tools", "strip", "--all", str(f), "-o", str(out)], check=True)
    return out


def _sizes(f: Path):
    data = f.read_bytes()
    br = subprocess.run(["brotli", "-c", "-q", "11", str(f)],
                        capture_output=True, check=True).stdout
    return {"raw": len(data), "gzip": len(gzip.compress(data, 9, mtime=0)), "brotli": len(br)}


def _sections(f: Path):
    # wabt's wasm-objdump cannot parse current GC/exnref encodings; wasm-tools can.
    out = subprocess.run(["wasm-tools", "objdump", str(f)],
                         capture_output=True, text=True, check=True).stdout
    res = {}
    for m in re.finditer(r"^\s*(\w[\w ]*?)\s*\|.*\|\s*(\d+) bytes \|\s*(\d+) count", out, re.M):
        res[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return {k: {"bytes": v[0], "count": v[1]} for k, v in res.items() if k in SECTIONS}


def _name_counts(f: Path):
    txt = subprocess.run(["wasm-tools", "print", str(f)],
                         capture_output=True, text=True, check=True).stdout
    funcs = re.findall(r"^\s*\(func \$(\S+)", txt, re.M)
    types = re.findall(r"^\s*\(type \$(\S+)", txt, re.M)
    return {k: {"funcs": sum(1 for n in funcs if re.search(p, n)),
                "types": sum(1 for n in types if re.search(p, n))}
            for k, p in NAME_PATTERNS.items()}


def measure(p, variant, with_names=True, force=False):
    out = config.BUILDS / p.name / variant.label
    if not (out / "meta.json").exists():
        config.warn(f"{p.name}/{variant.label}: no build (skipped or failed), "
                   "skipping measurement")
        return None
    measure_path = out / "measure.json"
    if measure_path.exists() and not force:
        config.log(f"{p.name}/{variant.label}: already measured, skipping (--force to redo)")
        return json.loads(measure_path.read_text())

    unopt = _pick(p, variant, "unoptimized")
    shipped = _pick(p, variant, "as-shipped")
    backend = _run_binaryen(unopt, out / "same-opt.wasm")

    result = {
        "project": p.name, "variant": variant.label, "rev": variant.rev,
        "deploy_version": variant.deploy_version,
        "project_revision": p.revision,
        "binaryen": config.binaryen_path().parent.parent.name,
        "files": {"unoptimized": str(unopt), "backend-only": str(backend),
                  "as-shipped": str(shipped)},
        "measurements": {},
    }
    for name, f in (("1 unoptimized", unopt), ("2 backend-only", backend),
                    ("3 as-shipped", shipped)):
        s = _stripped(f)
        result["measurements"][name] = {"file": str(f), **_sizes(s), "sections": _sections(s)}
    if with_names:
        result["name_counts"] = _name_counts(backend)

    measure_path.write_text(json.dumps(result, indent=2) + "\n")
    config.log(f"{p.name}/{variant.label}: measured "
               + ", ".join(f"{k}: {v['raw']:,} raw / {v['brotli']:,} brotli"
                           for k, v in result["measurements"].items()))
    return result


def load(p, variant_label):
    measure_path = config.BUILDS / p.name / variant_label / "measure.json"
    if not measure_path.exists():
        config.die(f"no measurement for {p.name}/{variant_label} — run 'wsb measure {variant_label}'")
    return json.loads(measure_path.read_text())


# ---------------------------------------------------------------- per-function diff

def _leb(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _parse_wasm(path):
    """[(function name, body size)] from the code + name sections (no name section ->
    anonymous). Homegrown parser: wasm-tools print is too slow for big modules."""
    b = open(path, "rb").read()
    i = 8
    bodies = []
    names = {}
    while i < len(b):
        sid = b[i]
        size, j = _leb(b, i + 1)
        end = j + size
        if sid == 10:
            n, k = _leb(b, j)
            for _ in range(n):
                sz, k2 = _leb(b, k)
                bodies.append(sz + (k2 - k))
                k = k2 + sz
        elif sid == 0:
            nl, k = _leb(b, j)
            if b[k:k + nl] == b"name":
                k += nl
                while k < end:
                    sub = b[k]
                    ssz, k2 = _leb(b, k + 1)
                    if sub == 1:
                        cnt, q = _leb(b, k2)
                        for _ in range(cnt):
                            idx, q = _leb(b, q)
                            ln, q = _leb(b, q)
                            names[idx] = b[q:q + ln].decode("utf-8", "replace")
                            q += ln
                    k = k2 + ssz
        i = end
    out = subprocess.run(["wasm-tools", "print", path], capture_output=True, text=True).stdout
    n_imports = len(re.findall(r"^\s*\(import .*\(func", out, re.M))
    return [(names.get(n_imports + n, f"<anon {n}>"), s) for n, s in enumerate(bodies)]


def _bucket(name, depth):
    kind = ""
    for pat, k in ((r"\$slambda|SuspendLambda|\$suspendBridge", "suspend-lambda"),
                   (r"\$lambda", "lambda"), (r"\$ref|FunctionReference", "callable-ref"),
                   (r"\$\$\$", "synthetic")):
        if re.search(pat, name):
            kind = f" <{k}>"
            break
    base = re.split(r"[\$<(]", name, 1)[0]
    return ".".join(base.split(".")[:depth]) + kind


def funcdiff(a_path, b_path, depth=3, top=30):
    """Code-section bytes per function-name prefix, a vs b (which pair of variants is up to
    the caller). Needs a name section — the backend-only output keeps -g."""
    a, b = _parse_wasm(a_path), _parse_wasm(b_path)

    def agg(funcs):
        c = Counter()
        for n, s in funcs:
            c[_bucket(n, depth)] += s
        return c

    ca, cb = agg(a), agg(b)
    ta, tb = sum(s for _, s in a), sum(s for _, s in b)
    lines = [f"total code: {ta:,} -> {tb:,} ({tb - ta:+,});  funcs {len(a):,} -> {len(b):,}",
             "| prefix | a | b | delta |", "|-"]
    keys = sorted(set(ca) | set(cb), key=lambda k: -abs(cb[k] - ca[k]))[:top]
    for k in keys:
        lines.append(f"| {k} | {ca[k]:,} | {cb[k]:,} | {cb[k] - ca[k]:+,} |")
    return "\n".join(lines)
