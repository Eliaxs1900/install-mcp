"""Backends JVM: ``maven`` y ``gradle``.

* ``maven``: descargar artefactos al repositorio local (``dependency:get``) o ejecutar
  objetivos de un proyecto (``dependency:go-offline``, ``package``, ``test``…).
* ``gradle``: tareas de un proyecto (``build``, ``test``, ``assemble``…).

Los objetivos/tareas van en una allowlist (no se acepta texto libre) y siempre en modo
no interactivo (``-B``/``--console=plain``). Si el proyecto trae wrapper (``mvnw``,
``gradlew``) se usa por defecto, como haría una persona.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .base import Backend, CommandStep, Plan, Probe
from .common import LimitParams, check_all, existing_dir, tool_path

# groupId:artifactId:version[:tipo[:clasificador]]; el tipo admite guiones (maven-plugin, test-jar).
_GAV = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+(:[a-z0-9][a-z0-9-]*(:[A-Za-z0-9_.-]+)?)?$")
GAV_FORMAT = "groupId:artifactId:version[:tipo[:clasificador]] (p. ej. org.apache.maven.plugins:maven-shade-plugin:3.6.0:maven-plugin)"
_PROFILE = re.compile(r"^[A-Za-z0-9_.!-]{1,64}$")
_PROP = re.compile(r"^[A-Za-z0-9_.-]{1,64}=[A-Za-z0-9_.,:/@+-]{0,200}$")
MAVEN_GOALS = (
    "clean", "validate", "compile", "test-compile", "test", "package", "verify", "install",
    "dependency:go-offline", "dependency:resolve", "dependency:tree", "dependency:sources",
)
GRADLE_TASKS = ("clean", "assemble", "build", "test", "check", "jar", "dependencies", "installDist", "classes")
TEST_GOALS = {"test", "package", "verify", "install"}  # objetivos de Maven que ejecutan tests

# Resumen de surefire/failsafe: "Tests run: 61, Failures: 0, Errors: 0, Skipped: 0" (los de cada
# clase llevan además ", Time elapsed: … - in Clase" y no se suman).
_SUREFIRE = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(.*)$")
_TEST_FAIL = re.compile(r"^(?:\[ERROR\]\s+)?(?P<name>\S.*?)\s+(?:--\s+)?Time elapsed:.*<<< (?P<kind>FAILURE|ERROR)!")
_MVN_ERROR = re.compile(r"^\[ERROR\] (Failed to execute goal .*|.*could not be resolved.*|.*BUILD FAILURE.*)$")
_DOWNLOADED = re.compile(r"Downloaded from [^:]+: \S+/([^/\s]+)\s")
# Gradle: "61 tests completed, 2 failed, 1 skipped" y "FooTest > bar() FAILED"
_GRADLE_SUMMARY = re.compile(r"(\d+) tests? completed(?:, (\d+) failed)?(?:, (\d+) skipped)?")
_GRADLE_FAIL = re.compile(r"^(?P<name>\S.*? > .*?) FAILED$")


def _tests_report(run: int, failures: int, errors: int, skipped: int, failed: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"run": run, "failures": failures, "errors": errors, "skipped": skipped,
                           "passed": max(run - failures - errors - skipped, 0)}
    if failed:
        out["failed_tests"] = failed[:20]
    return out


def _tests_headline(t: dict[str, Any]) -> str:
    bad = t["failures"] + t["errors"]
    text = f"tests: {t['run']} ejecutados, {bad} fallidos" + (f", {t['skipped']} omitidos" if t["skipped"] else "")
    if t.get("failed_tests"):
        text += " (" + ", ".join(t["failed_tests"][:3]) + ("…" if len(t["failed_tests"]) > 3 else "") + ")"
    return text


def _pom_coords(d: Path) -> str | None:
    try:
        root = ET.parse(d / "pom.xml").getroot()
    except (OSError, ET.ParseError):
        return None
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}

    def find(tag: str) -> str | None:
        own = root.findtext(f"m:{tag}", namespaces=ns) if ns else root.findtext(tag)
        if own:
            return own
        parent = f"m:parent/m:{tag}" if ns else f"parent/{tag}"
        return root.findtext(parent, namespaces=ns) if ns else root.findtext(parent)

    return f"{find('groupId') or '?'}:{find('artifactId') or '?'}:{find('version') or '?'}"


def _jars(d: Path) -> list[str]:
    return sorted(p.name for p in d.glob("*.jar")) if d.is_dir() else []


def _wrapper(d: Path, name: str, use: str) -> str | None:
    w = d / name
    if use == "never" or not w.exists():
        if use == "always":
            raise ValueError(f"use_wrapper='always' pero no existe {w}")
        return None
    return str(w)


class MavenParams(LimitParams):
    artifacts: list[str] = Field(
        default_factory=list, max_length=50,
        description="Artefactos 'groupId:artifactId:version[:tipo[:clasificador]]' a descargar a ~/.m2.",
    )
    project_dir: str | None = Field(None, description="Proyecto Maven (directorio con pom.xml).")
    goals: list[str] = Field(
        default_factory=lambda: ["dependency:go-offline"], max_length=8,
        description=f"Solo en modo proyecto. Permitidos: {', '.join(MAVEN_GOALS)}.",
    )
    skip_tests: bool = False
    offline: bool = False
    profiles: list[str] = Field(default_factory=list, max_length=10)
    properties: list[str] = Field(default_factory=list, max_length=20, description="'clave=valor' -> -Dclave=valor.")
    use_wrapper: Literal["auto", "always", "never"] = "auto"


class MavenBackend(Backend[MavenParams]):
    name = "maven"
    description = "Maven: artefactos al repositorio local o objetivos de un proyecto (go-offline, package, test…)."
    Params = MavenParams
    default_stall_timeout_s = 600.0

    def availability(self) -> tuple[bool, str]:
        return True, "ok (mvn en PATH o mvnw en el proyecto)"

    def check(self, spec: MavenParams) -> None:
        if bool(spec.artifacts) == bool(spec.project_dir):
            raise ValueError("indica 'artifacts' o 'project_dir'")
        for a in spec.artifacts:
            if a.startswith("-") or not _GAV.match(a):
                raise ValueError(f"artefacto no válido: {a!r}; formato {GAV_FORMAT}")
        check_all(spec.profiles, _PROFILE, what="perfil")
        check_all(spec.properties, _PROP, what="propiedad")
        bad = [g for g in spec.goals if g not in MAVEN_GOALS]
        if bad:
            raise ValueError(f"objetivos no permitidos: {bad}; permitidos: {list(MAVEN_GOALS)}")
        if spec.project_dir:
            d = existing_dir(spec.project_dir, self.config, what="project_dir")
            if not (d / "pom.xml").exists():
                raise ValueError(f"{d} no tiene pom.xml")
        self._mvn(spec)

    def _mvn(self, spec: MavenParams) -> str:
        if spec.project_dir:
            w = _wrapper(Path(spec.project_dir), "mvnw", spec.use_wrapper)
            if w:
                return w
        mvn = tool_path("mvn", self.options)
        if not mvn:
            raise ValueError("no se encuentra mvn en PATH (ni mvnw en el proyecto)")
        return mvn

    def plan(self, spec: MavenParams) -> Plan:
        mvn = self._mvn(spec)
        # En modo artifacts se quita -ntp: así el log dice qué se descargó ("Downloaded from …").
        # En -B (batch) Maven no imprime barras de progreso, solo una línea por fichero.
        base = [mvn, "-B", "-Dstyle.color=never"] + ([] if spec.artifacts else ["-ntp"])
        base += [f"-D{p}" for p in spec.properties]
        if spec.offline:
            base.append("-o")
        if spec.artifacts:
            steps = [
                CommandStep(f"get {a}", [*base, "dependency:get", f"-Dartifact={a}"]) for a in spec.artifacts
            ]
            return Plan(summary=f"maven: descargar {', '.join(spec.artifacts)}", steps=steps,
                        lock_key="maven:repo", **spec.limits())
        d = existing_dir(spec.project_dir or "", self.config, what="project_dir")
        argv = [*base, "-f", str(d / "pom.xml")]
        if spec.skip_tests:
            argv.append("-DskipTests")
        if spec.profiles:
            argv += ["-P", ",".join(spec.profiles)]
        return Plan(
            summary=f"maven {' '.join(spec.goals)} en {d}",
            steps=[CommandStep(" ".join(spec.goals), [*argv, *spec.goals], cwd=str(d))],
            lock_key=f"path:{d}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: MavenParams, probe: Probe) -> dict[str, str]:
        """Solo lo que de verdad queda instalado en ~/.m2: artefactos descargados o ``install``."""
        repo = Path.home() / ".m2" / "repository"
        if spec.artifacts:
            out = {}
            for a in spec.artifacts:
                g, art, v = a.split(":")[:3]
                out[f"{g}:{art}"] = v if (repo / g.replace(".", "/") / art / v).is_dir() else "NO ENCONTRADO"
            return out
        if "install" in spec.goals and (coords := _pom_coords(Path(spec.project_dir or ""))):
            g, art, v = coords.split(":")
            return {f"{g}:{art}": f"{v} (en ~/.m2)"}
        return {}  # test/package/verify… no instalan nada: el proyecto y lo construido van en info

    def result_info(self, spec: MavenParams) -> dict[str, Any]:
        if not spec.project_dir:
            return {"repository": str(Path.home() / ".m2" / "repository")}
        d = Path(spec.project_dir)
        info: dict[str, Any] = {"project_dir": str(d), "goals": spec.goals}
        if coords := _pom_coords(d):
            info["project"] = coords
        if built := _jars(d / "target"):
            info["built"] = [f"target/{j}" for j in built[:10]]
        return info

    def summarize(self, spec: MavenParams, output: list[str], *, ok: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"report": {}}
        if spec.artifacts:
            files = [m.group(1) for line in output if (m := _DOWNLOADED.search(line))]
            out["report"]["downloads"] = {"count": len(files), "files": files[:50],
                                          "already_present": not files and ok}
            out["headline"] = (f"{len(files)} ficheros descargados" if files
                               else "ya estaba todo en ~/.m2" if ok else "")
        elif TEST_GOALS & set(spec.goals) and not spec.skip_tests:
            totals = [0, 0, 0, 0]
            found = False
            for line in output:
                m = _SUREFIRE.search(line)
                if m and "Time elapsed" not in m.group(5):
                    found = True
                    totals = [a + int(b) for a, b in zip(totals, m.groups()[:4], strict=True)]
            failed = list(dict.fromkeys(
                re.sub(r"^\[ERROR\]\s+", "", m.group("name")) for line in output
                if (m := _TEST_FAIL.search(line)) and not m.group("name").startswith("Tests run:")
            ))
            if found:
                tests = _tests_report(*totals, failed)
                out["report"]["tests"] = tests
                out["headline"] = _tests_headline(tests)
        if not ok:
            err = next((m.group(1) for line in output if (m := _MVN_ERROR.match(line.strip()))
                        and "BUILD FAILURE" not in line), None)
            if err:
                out["error_detail"] = err[:300]
            text = "\n".join(output[-400:])
            if spec.offline and ("has not been downloaded from it before" in text or "offline mode" in text):
                out["hint"] = ("faltan dependencias en ~/.m2 y offline=true impide bajarlas: repite con "
                               "offline=false o descárgalas antes con el modo artifacts")
        return out


class GradleParams(LimitParams):
    project_dir: str = Field(..., description="Proyecto Gradle (directorio con build.gradle[.kts] o settings.gradle).")
    tasks: list[str] = Field(
        default_factory=lambda: ["build"], min_length=1, max_length=8,
        description=f"Permitidas: {', '.join(GRADLE_TASKS)} (también ':modulo:tarea').",
    )
    exclude_test: bool = Field(False, description="-x test")
    offline: bool = False
    use_wrapper: Literal["auto", "always", "never"] = "auto"


class GradleBackend(Backend[GradleParams]):
    name = "gradle"
    description = "Gradle: tareas de un proyecto (build, test, assemble…) con el wrapper si existe."
    Params = GradleParams
    default_stall_timeout_s = 600.0

    def availability(self) -> tuple[bool, str]:
        return True, "ok (gradle en PATH o gradlew en el proyecto)"

    def check(self, spec: GradleParams) -> None:
        d = existing_dir(spec.project_dir, self.config, what="project_dir")
        if not any((d / f).exists() for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
            raise ValueError(f"{d} no parece un proyecto Gradle")
        for t in spec.tasks:
            if t.split(":")[-1] not in GRADLE_TASKS or not re.fullmatch(r"(:[A-Za-z0-9_-]+)*:?[A-Za-z]+", t):
                raise ValueError(f"tarea no permitida: {t!r}; permitidas: {list(GRADLE_TASKS)}")
        self._gradle(spec)

    def _gradle(self, spec: GradleParams) -> str:
        w = _wrapper(Path(spec.project_dir), "gradlew", spec.use_wrapper)
        if w:
            return w
        g = tool_path("gradle", self.options)
        if not g:
            raise ValueError("no se encuentra gradle en PATH (ni gradlew en el proyecto)")
        return g

    def plan(self, spec: GradleParams) -> Plan:
        d = existing_dir(spec.project_dir, self.config, what="project_dir")
        argv = [self._gradle(spec), "--console=plain", "--no-daemon", "-p", str(d)]
        if spec.offline:
            argv.append("--offline")
        if spec.exclude_test:
            argv += ["-x", "test"]
        return Plan(
            summary=f"gradle {' '.join(spec.tasks)} en {d}",
            steps=[CommandStep(" ".join(spec.tasks), [*argv, *spec.tasks], cwd=str(d), env={"TERM": "dumb"})],
            lock_key=f"path:{d}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: GradleParams, probe: Probe) -> dict[str, str]:
        return {}  # Gradle construye, no instala: lo construido va en info["built"]

    def result_info(self, spec: GradleParams) -> dict[str, Any]:
        d = Path(spec.project_dir)
        info: dict[str, Any] = {"project_dir": str(d), "tasks": spec.tasks}
        if built := _jars(d / "build" / "libs"):
            info["built"] = [f"build/libs/{j}" for j in built[:10]]
        return info

    def summarize(self, spec: GradleParams, output: list[str], *, ok: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"report": {}}
        summary = next((m for line in reversed(output) if (m := _GRADLE_SUMMARY.search(line))), None)
        failed = list(dict.fromkeys(m.group("name") for line in output if (m := _GRADLE_FAIL.match(line.strip()))))
        if summary:
            run, fails, skipped = (int(x or 0) for x in summary.groups())
            tests = _tests_report(run, fails, 0, skipped, failed)
            out["report"]["tests"] = tests
            out["headline"] = _tests_headline(tests)
        elif failed:
            out["report"]["tests"] = {"failed_tests": failed[:20]}
        if not ok:
            err = next((line.strip() for line in output if line.strip().startswith(("* What went wrong", "> Task"))
                        and "FAILED" in line), None)
            what = next((output[i + 1].strip() for i, line in enumerate(output[:-1])
                         if line.strip().startswith("* What went wrong")), None)
            if what or err:
                out["error_detail"] = (what or err or "")[:300]
        return out
