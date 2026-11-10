"""Benchmark projects: the extension point of the harness.

Each Project pins a real-world app at one revision, describes how to build its production
wasm with a given compiler variant, and which files carry the size results. Projects are pinned
at *old* revisions on purpose: every variant's compiler (even released ones) must be able to read
the same prebuilt klibs the app's dependencies ship.

To add a project, append a Project(...) to PROJECTS. Fields:

  name            short id used on the CLI (--projects)
  repo            git URL
  revision        pinned commit sha (determinism: never a branch)
  clone_dir       directory name under $WSB_HOME/clones
  subdir          gradle build root, relative to the repo root ("" = repo root)
  module          gradle module that produces the wasm
  tasks           gradle tasks producing unoptimized + optimized production wasm
  version_files   TOML files whose `kotlin = "..."` line is set to the variant's version
  patches         Patch list applied per variant (see Patch); all patched files are reset
                  with `git checkout --` before each build, so patches may be conditional
  wipe            paths (relative to subdir, "{module}" expanded) removed before each build
  artifacts       measurement -> glob (relative to the collected compileSync/ copy) for the
                  wasm files to measure; largest non-excluded match wins
  artifact_exclude  substrings; files whose name contains one are ignored (e.g. skiko)

Patch.when selects the variants a patch applies to: "always", "kgp-modern" (KGP newer than the
2.2.x line) or "kgp-legacy" (2.2.x and older) — the binaryenArgs -> binaryenArguments rename
in KGP makes this the one variant-dependent edit.

Version substitution is regex-based: `version_re`/`version_replace` (defaults cover the usual
`kotlin = "..."` version-catalog line; imageviewer uses a gradle.properties-style
`kotlin.version=...` instead). `min_kotlin` skips the project for variants older than that
version (e.g. 2.3-era examples can't build on a 2.2.x Beta baseline) — the other projects
still cover old baselines.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Patch:
    file: str
    when: str = "always"
    find: Optional[str] = None
    replace: Optional[str] = None
    append: Optional[str] = None  # appended to the file instead of a find/replace


@dataclass
class Project:
    name: str
    repo: str
    revision: str
    clone_dir: str
    subdir: str
    module: str
    tasks: List[str]
    version_files: List[str]
    patches: List[Patch]
    wipe: List[str]
    artifacts: dict
    artifact_exclude: List[str] = field(default_factory=lambda: ["skiko"])
    version_re: str = r'(?m)^kotlin\s*=\s*"[^"]*"'
    version_replace: str = 'kotlin = "{version}"'
    min_kotlin: str = ""

    def patched_files(self):
        return sorted({p.file for p in self.patches} | set(self.version_files))


# Keep the name section in the KGP-optimized output so per-function analysis works.
# Appended (not inlined) because most projects don't configure BinaryenExec upstream.
_BINARYEN_G_MODERN = (
    "\n// wasm size benchmark: keep the name section\n"
    "tasks.withType<org.jetbrains.kotlin.gradle.targets.wasm.binaryen.BinaryenExec>().configureEach {\n"
    "    binaryenArguments.add(\"-g\")\n"
    "}\n"
)
_BINARYEN_G_LEGACY = _BINARYEN_G_MODERN.replace("binaryenArguments", "binaryenArgs")

# Projects pinned at 2.2-era revisions (every variant's compiler must read the same klibs):
_WASM_ARGS = {
    "unoptimized": "compileSync/**/productionExecutable/kotlin/*.wasm",
    "as-shipped": "compileSync/**/productionExecutable/optimized/*.wasm",
}
_COMPOSE_REPO = "https://github.com/JetBrains/compose-multiplatform.git"
_COMPOSE_PIN = "ad506cafef2cbc7cc47844632177da35fcf22e7d"  # CMP 1.9.3, 2.2-era klibs


PROJECTS = [
    Project(
        name="compose-benchmarks",
        repo=_COMPOSE_REPO,
        revision=_COMPOSE_PIN,
        clone_dir="compose-multiplatform",
        subdir="benchmarks/multiplatform",
        module="benchmarks",
        tasks=[":benchmarks:wasmJsProductionExecutableCompileSync",
               ":benchmarks:compileProductionExecutableKotlinWasmJsOptimize"],
        version_files=["gradle/libs.versions.toml"],
        patches=[
            # master KGP requires Gradle >= 8.14 (harmless for the 2.2-era baseline variant)
            Patch(file="gradle/wrapper/gradle-wrapper.properties",
                  find="gradle-8.13-bin.zip", replace="gradle-8.14-bin.zip"),
            # skip the macosX64 target (not needed, avoids its Kotlin/Native toolchain)
            Patch(file="benchmarks/build.gradle.kts",
                  find="listOf(\n        macosX64(),\n        macosArm64()",
                  replace="listOf(\n        macosArm64()"),
            # keep the name section for per-function analysis; property renamed after 2.2.x
            Patch(file="benchmarks/build.gradle.kts",
                  when="kgp-modern",
                  find='binaryenArgs.add("-g")', replace='binaryenArguments.add("-g")'),
        ],
        wipe=["{module}/build/compileSync", "build/compileSync"],
        artifacts=dict(_WASM_ARGS),
    ),
    Project(
        name="imageviewer",
        repo=_COMPOSE_REPO,
        revision=_COMPOSE_PIN,  # reuses the compose-multiplatform clone
        clone_dir="compose-multiplatform",
        subdir="examples/imageviewer",
        module="webApp",
        tasks=[":webApp:wasmJsProductionExecutableCompileSync",
               ":webApp:compileProductionExecutableKotlinWasmJsOptimize"],
        # this example is 2.3-era (CMP 1.10.0): only builds on modern variants
        version_files=["gradle.properties"],
        version_re=r"(?m)^kotlin\.version=.*$",
        version_replace="kotlin.version={version}",
        min_kotlin="2.3.0",
        patches=[
            Patch(file="gradle/wrapper/gradle-wrapper.properties",
                  find="gradle-8.13-bin.zip", replace="gradle-8.14-bin.zip"),
            # resolve the locally installed compiler snapshot (settings + dependency repos)
            Patch(file="settings.gradle.kts",
                  find="pluginManagement {\n    repositories {\n",
                  replace="pluginManagement {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="build.gradle.kts",
                  find="allprojects {\n    repositories {\n",
                  replace="allprojects {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="webApp/build.gradle.kts", when="kgp-modern",
                  append=_BINARYEN_G_MODERN),
            Patch(file="webApp/build.gradle.kts", when="kgp-legacy",
                  append=_BINARYEN_G_LEGACY),
        ],
        wipe=["{module}/build/compileSync", "build/compileSync"],
        artifacts=dict(_WASM_ARGS),
    ),
    Project(
        name="nav-cupcake",
        repo=_COMPOSE_REPO,
        revision=_COMPOSE_PIN,  # reuses the compose-multiplatform clone
        clone_dir="compose-multiplatform",
        subdir="examples/nav_cupcake",
        module="composeApp",
        tasks=[":composeApp:wasmJsProductionExecutableCompileSync",
               ":composeApp:compileProductionExecutableKotlinWasmJsOptimize"],
        version_files=["gradle/libs.versions.toml"],
        patches=[
            Patch(file="gradle/wrapper/gradle-wrapper.properties",
                  find="gradle-8.13-bin.zip", replace="gradle-8.14-bin.zip"),
            Patch(file="settings.gradle.kts",
                  find="pluginManagement {\n    repositories {\n",
                  replace="pluginManagement {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="settings.gradle.kts",
                  find="dependencyResolutionManagement {\n    repositories {\n",
                  replace="dependencyResolutionManagement {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="composeApp/build.gradle.kts", when="kgp-modern",
                  append=_BINARYEN_G_MODERN),
            Patch(file="composeApp/build.gradle.kts", when="kgp-legacy",
                  append=_BINARYEN_G_LEGACY),
        ],
        wipe=["{module}/build/compileSync", "build/compileSync"],
        artifacts=dict(_WASM_ARGS),
    ),
    Project(
        name="kotlinconf",
        repo="https://github.com/JetBrains/kotlinconf-app.git",
        revision="b3bb5c4a0c0b1ef167c63cfc8b9a6b74d1332b88",  # CMP 1.9.0, before metro
        clone_dir="kotlinconf-app",
        subdir="",
        module="shared",
        tasks=[":shared:wasmJsProductionExecutableCompileSync",
               ":shared:compileProductionExecutableKotlinWasmJsOptimize"],
        version_files=["gradle/libs.versions.toml"],
        patches=[
            # resolve the locally installed compiler snapshot
            Patch(file="settings.gradle.kts",
                  find="pluginManagement {\n    repositories {\n",
                  replace="pluginManagement {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="settings.gradle.kts",
                  find="dependencyResolutionManagement {\n    repositories {\n",
                  replace="dependencyResolutionManagement {\n    repositories {\n        mavenLocal()\n"),
            Patch(file="shared/build.gradle.kts", when="kgp-modern", append=_BINARYEN_G_MODERN),
            Patch(file="shared/build.gradle.kts", when="kgp-legacy", append=_BINARYEN_G_LEGACY),
        ],
        wipe=["{module}/build/compileSync", "build/compileSync"],
        artifacts=dict(_WASM_ARGS),
    ),
]

BY_NAME = {p.name: p for p in PROJECTS}


def get(name):
    from . import config
    p = BY_NAME.get(name)
    if not p:
        config.die(f"unknown project '{name}' (known: {', '.join(sorted(BY_NAME))})")
    return p
