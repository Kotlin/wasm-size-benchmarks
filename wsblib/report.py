"""Render comparison reports from measure.json files.

First variant in the list is the baseline; every later variant is reported with a delta
vs it. Markdown to stdout (and saved), machine-readable results.json alongside.
"""

import json
from datetime import datetime
from pathlib import Path

from . import config, measure
from .measure import SECTIONS

MEASUREMENTS = ("1 unoptimized", "2 backend-only", "3 as-shipped")


def _pct(a, b):
    return f"{(b - a) / a * 100:+.2f}%" if a else "n/a"


def _fmt(v, base=None):
    s = f"{v:,}"
    return s if base is None or base == v else f"{s} ({_pct(base, v)})"


def _row_sizes(m):
    return m["raw"], m["gzip"], m["brotli"]


def _project_table(name, data_by_variant):
    lines = [f"## {name}", "",
             "| measurement | variant | raw | gzip -9 | brotli -q11 | "
             + " | ".join(SECTIONS) + " |",
             "|-|-|-|-|-|" + "-|" * len(SECTIONS)]
    labels = list(data_by_variant)
    for meas in MEASUREMENTS:
        base_m = data_by_variant[labels[0]]["measurements"].get(meas)
        for label in labels:
            m = data_by_variant[label]["measurements"].get(meas)
            if not m:
                continue
            cur = _row_sizes(m)
            base = _row_sizes(base_m) if base_m and label != labels[0] else None
            cells = [_fmt(cur[i], base[i] if base else None) for i in range(3)]
            sec = " | ".join(
                f"{m['sections'][n]['bytes']:,} ({m['sections'][n]['count']:,})"
                if n in m["sections"] else "—"
                for n in SECTIONS)
            lines.append(f"| {meas} | {label} | " + " | ".join(cells) + f" | {sec} |")
    return lines


def _summary_table(all_data):
    lines = ["## Summary (delta vs first variant)", "",
             "| project | measurement | variant | raw | gzip -9 | brotli -q11 |", "|-|-|-|-|-|-|"]
    for name, data_by_variant in all_data.items():
        labels = list(data_by_variant)
        for meas in MEASUREMENTS:
            base_m = data_by_variant[labels[0]]["measurements"].get(meas)
            if not base_m:
                continue
            base = _row_sizes(base_m)
            for label in labels:
                m = data_by_variant[label]["measurements"].get(meas)
                if not m:
                    continue
                cur = _row_sizes(m)
                lines.append(
                    f"| {name} | {meas} | {label} | "
                    + " | ".join(_fmt(cur[i], base[i]) for i in range(3)) + " |")
    return lines


def _names_section(all_data):
    lines = ["## Function/type name counts (measurement 2 backend-only)", ""]
    for name, data_by_variant in all_data.items():
        rows = [(label, res.get("name_counts")) for label, res in data_by_variant.items()]
        if not all(nc for _, nc in rows):
            continue
        lines.append(f"### {name}")
        for label, nc in rows:
            lines.append(f"- {label}: "
                         + ", ".join(f"{k} {v['funcs']:,}/{v['types']:,}" for k, v in nc.items()))
        lines.append("")
    return lines


def compare(project_names, labels, save=True):
    from .projects import get
    all_data = {}
    missing = []
    for name in project_names:
        p = get(name)
        data_by_variant = {}
        for label in labels:
            mp = config.BUILDS / p.name / label / "measure.json"
            if mp.exists():
                data_by_variant[label] = measure.load(p, label)
            else:
                missing.append(f"{p.name}/{label}")
        if data_by_variant:
            all_data[p.name] = data_by_variant
    if missing:
        config.warn(f"no measurements for: {', '.join(missing)} (skipped in report)")

    md = ["# Kotlin/Wasm size comparison", "",
          f"- variants: {', '.join(labels)} (first = baseline)",
          f"- generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    if all_data:
        md += _summary_table(all_data) + [""] + _names_section(all_data)
        for name, data_by_variant in all_data.items():
            md += _project_table(name, data_by_variant) + [""]
        md += ["Measurements: 1 = compiler output before Binaryen; 2 = the same pinned "
               "Binaryen + args run on every variant (isolates backend changes); "
               "3 = each KGP's "
               "own optimized output. Sizes after `wasm-tools strip --all`.",
               "Prefer compressed (gzip/brotli) deltas: raw exaggerates wins that are just "
               "string literals moving from the data section to import names."]

    text = "\n".join(md)
    print(text)
    if save:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        outdir = config.RESULTS / f"{stamp}-{'-vs-'.join(labels)}"
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "report.md").write_text(text + "\n")
        (outdir / "results.json").write_text(json.dumps(all_data, indent=2) + "\n")
        config.log(f"report saved to {outdir}")
    return all_data
