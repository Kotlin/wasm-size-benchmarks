# wsb — Kotlin/Wasm compiler size benchmark harness

Measures the **Kotlin/Wasm binary-size impact of a compiler change**: it takes one or more
Kotlin compiler versions, builds each pinned real-world project once with each of them, and
reports the size deltas.

Each compiler build in a comparison is a **variant**. The flag
`--variants` takes a comma separated list where the first variant is
the baseline and and every other variant's sizes are compared against
it; with more than two variants you get a timeline (per-commit
attribution).

```bash
./wsb.py run                                   # baseline=HEAD, change=your working tree as-is
./wsb.py run --baseline 26b01b14f72b --change ee5eff663cbc
./wsb.py run --variants 2.2.21,ee5eff663cbc^,ee5eff663cbc,origin/master   # >2 variants: timeline
./wsb.py run --projects kotlinconf              # subset of projects
```

`--baseline A --change B` is just syntactic sugar for `--variants
A,B`. If both are given, `--variants` wins.

A variant can be specified in the following ways:

| spec                                    | meaning                                                                               |
|-----------------------------------------|---------------------------------------------------------------------------------------|
| `HEAD`, sha, `HEAD~3`, `origin/master`… | a Kotlin revision: linked worktree, installed to `~/.m2` as `<base>-<label>-SNAPSHOT` |
| `dirty` (or `.`)                        | the main Kotlin working tree as-is, uncommitted changes included (builds in place)    |
| `2.2.21`                                | a released version from Maven Central, nothing is built                               |
| `maven:2.5.255-kc-SNAPSHOT`             | an already-installed `~/.m2` snapshot                                                 |

Each variant gets a **label** (short sha by default, override with `--baseline-label` /
`--change-label` / `--labels`); the label pins the variant in
`~/.cache/wasm-size-bench/variants.json`, which makes every step resumable: re-running skips
installs, builds and measurements that are already done, and incremental Gradle builds make
re-installing a changed variant cheap.

Note: The Kotlin repo used for variants/worktrees is this harness's
parent dir!! Override with `WSB_KOTLIN`.

## Example: master vs Beta

The following compares how sizes in the wasm backend changes in the
compiler and stdlib from the 2.2.21 Beta release to current master,
with the projects' own library klibs held constant:

```bash
./wsb.py run --baseline 2.2.21 --change origin/master
```

The report printed at the end (and saved under `results/`) looks like:

```
## Summary (delta vs first variant)

| project | measurement | variant | raw | gzip -9 | brotli -q11 |
|-|-|-|-|-|-|
| kotlinconf | 2 backend-only | 2.2.21 | 6,105,518 | 1,705,086 | 1,265,735 |
|            |                | 683ff38277 | 5,216,472 (−14.56%) | 1,647,969 (−3.35%) | 1,210,603 (−4.36%) |
| kotlinconf | 3 as-shipped | 2.2.21 | 6,381,541 | 1,765,796 | 1,312,081 |
|            |              | 683ff38277 | 5,216,472 (−18.26%) | 1,647,969 (−6.67%) | 1,210,603 (−7.73%) |
```

To attribute the delta to individual commits, chain `--variants` into a timeline (first
entry = baseline, every later variant is reported against it):

```bash
./wsb.py run --variants 2.2.21,ee5eff663cbc^,ee5eff663cbc,origin/master
./wsb.py funcdiff <baseline-label> <variant-label> --project kotlinconf   # which code grew
```

A first run pays for one `./gradlew install` (~40–60 min) plus a build per project per
variant; re-runs against the same variants are minutes.

## Method

Per (project, variant), three measurements — sizes after `wasm-tools strip --all`:

1. **unoptimized** — compiler output before Binaryen (`compileSync/**/kotlin/*.wasm`).
2. **backend only** — the *same pinned Binaryen + args* (`binaryen_args.txt`, regenerated from
   `wasm/wasm.config/.../BinaryenConfig.kt` via `./wsb.py refresh-args`) run on every variant's
   unoptimized output. **The main number**: isolates backend changes from KGP toolchain changes.
3. **as shipped** — each KGP's own optimized output (`compileSync/**/optimized/*.wasm`).

For each: raw, `gzip -9`, `brotli -q 11`, plus section sizes/counts. Prefer the compressed
numbers — raw deltas exaggerate wins that are just string literals moving from the data
section to `wasm:js-string` import names.

Per-function attribution: `./wsb.py funcdiff <variantA> <variantB> --project kotlinconf`
(code-section bytes by function-name prefix; needs the `-g` name section, which the project
patches keep).

## Projects

Defined in `wsblib/projects.py` — add an entry and you're done (clone, patch, build, measure
all follow from it). Four are configured:

| project | what it is | notes |
|---|---|---|
| **compose-benchmarks** | `JetBrains/compose-multiplatform` @ `ad506cafef`, the `benchmarks/multiplatform` UI suite | the heaviest Compose scenes |
| **imageviewer** | same repo, `examples/imageviewer` (Ktor + image loading + canvas) | 2.3-era (CMP 1.10.0) — `min_kotlin = 2.3.0`, auto-skipped on variants ≤ 2.2 |
| **nav-cupcake** | same repo, `examples/nav_cupcake` (navigation sample, CMP 1.8.2) | small app; reuses the same clone |
| **kotlinconf** | `JetBrains/kotlinconf-app` @ `b3bb5c4a` (CMP 1.9.0, before metro), `shared` webApp | the real-world app |

Projects are pinned at **2.2-era revisions on purpose** (except imageviewer, which needs a
newer baseline): every variant's compiler — even a released one — must be able to read the
same prebuilt klibs the app's dependencies ship, so only the compiler differs between
variants. (For
the same reason the released baseline is 2.2.21, not 2.2.20: CMP 1.9.3 pulls the 2.2.21
stdlib.) `min_kotlin` on a project makes old-variant runs skip it (with a log line) instead of
failing, so e.g. the Beta-vs-master comparison above simply measures the other three.

Per-variant patches (all reverted with `git checkout` before every build, so they're
conditional and idempotent): set `kotlin = "<variant version>"` in
`gradle/libs.versions.toml`; add
`mavenLocal()` to kotlinconf's settings (upstream Compose already has it); Gradle wrapper
≥ 8.14 for master KGP; skip the `macosX64()` target; and `binaryenArgs` → `binaryenArguments`
after the 2.2.x line, keeping `-g` so the name section survives for `funcdiff`.

## Determinism

- projects pinned by sha, never a branch
- each variant installs under its own `-SNAPSHOT` version, so variants coexist in `~/.m2`
- `compileSync` outputs wiped before every build
- one pinned Binaryen version (`WSB_BINARYEN_VERSION`, default 133) and arg list for the
  backend-only measurement; the actual version used is recorded in every `measure.json`
- `measure.json` fingerprints inputs (sha256) so a rebuilt wasm invalidates its cached
  Binaryen pass and stripped copies

Caveat: compiler builds are only as deterministic as the Gradle build; rebuild-identity can
be checked via the sha256s recorded in `builds/<project>/<variant>/meta.json`.

## Layout

Everything lives under `WSB_HOME` (default `~/.cache/wasm-size-bench`, override with the env
var): `clones/` (project repos), `worktrees/` (Kotlin variants),
`builds/<project>/<variant>/` (compileSync copy + `build.log` + `meta.json` +
`measure.json` + `same-opt.wasm`), `results/<stamp>-<variants>/` (report.md +
results.json), `variants.json` (the variant registry).

## Subcommands

`run` does everything; the pieces exist for resuming and for multi-day attribution sessions:

```
wsb install SPEC [--labels l1,l2]    # build a compiler variant into ~/.m2
wsb build LABEL... [--projects ...] # build projects against a registered variant
wsb measure LABEL...                # measure built outputs
wsb compare L1 L2 [...]             # render + save the report
wsb funcdiff L1 L2 --project P      # per-function code-size diff
wsb refresh-args                    # re-pin Binaryen args from BinaryenConfig.kt
wsb status                          # variants / clones / builds / tools overview
wsb clean --worktrees|--clones|--builds|--results
```

## Requirements

`wasm-tools`, `brotli`, `git` on PATH; a Binaryen under `~/.gradle/binaryen/binaryen-version_*`
(any Kotlin/Wasm Gradle build puts one there) or `WSB_BINARYEN` pointing at a `wasm-opt`.
JDK etc. are whatever the Kotlin repo's `./gradlew` needs.

## Notes
- `wasm-objdump` (wabt) cannot parse current GC/exnref encodings — the harness uses
  `wasm-tools objdump`/`print` throughout.
