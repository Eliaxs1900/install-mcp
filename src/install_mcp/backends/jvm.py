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

_GAV = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+(:[a-z0-9]+(:[A-Za-z0-9_.-]+)?)?$")
_PROFILE = re.compile(r"^[A-Za-z0-9_.!-]{1,64}$")
_PROP = re.compile(r"^[A-Za-z0-9_.-]{1,64}=[A-Za-z0-9_.,:/@+-]{0,200}$")
MAVEN_GOALS = (
    "clean", "validate", "compile", "test-compile", "test", "package", "verify", "install",
    "dependency:go-offline", "dependency:resolve", "dependency:tree", "dependency:sources",
)
GRADLE_TASKS = ("clean", "assemble", "build", "test", "check", "jar", "dependencies", "installDist", "classes")


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
        check_all(spec.artifacts, _GAV, what="artefacto")
        check_all(spec.profiles, _PROFILE, what="perfil")
        check_all(spec.properties, _PROP, what="propiedad")
        bad = [g for g in spec.goals if g not in MAVEN_GOALS]
        if bad:
            raise ValueError(f"objetivos no permitidos: {bad}; permitidos: {list(MAVEN_GOALS)}")
        if spec.project_dir:
            d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
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
        base = [mvn, "-B", "-ntp", "-Dstyle.color=never"]
        base += [f"-D{p}" for p in spec.properties]
        if spec.offline:
            base.append("-o")
        if spec.artifacts:
            steps = [
                CommandStep(f"get {a}", [*base, "dependency:get", f"-Dartifact={a}"]) for a in spec.artifacts
            ]
            return Plan(summary=f"maven: descargar {', '.join(spec.artifacts)}", steps=steps,
                        lock_key="maven:repo", **spec.limits())
        d = existing_dir(spec.project_dir or "", self.config.roots(), what="project_dir")
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
        if spec.artifacts:
            repo = Path.home() / ".m2" / "repository"
            out = {}
            for a in spec.artifacts:
                g, art, v = a.split(":")[:3]
                out[f"{g}:{art}"] = v if (repo / g.replace(".", "/") / art / v).is_dir() else "NO ENCONTRADO"
            return out
        d = Path(spec.project_dir or "")
        out: dict[str, str] = {}
        try:
            root = ET.parse(d / "pom.xml").getroot()
            ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            find = (lambda t: root.findtext(f"m:{t}", namespaces=ns)) if ns else root.findtext
            out[f"{find('groupId') or '?'}:{find('artifactId')}"] = find("version") or "?"
        except (OSError, ET.ParseError):
            pass
        jars = sorted(p.name for p in (d / "target").glob("*.jar")) if (d / "target").is_dir() else []
        if jars:
            out["target"] = ", ".join(jars[:5])
        return out

    def result_info(self, spec: MavenParams) -> dict[str, Any]:
        return {"project_dir": spec.project_dir} if spec.project_dir else {"repository": str(Path.home() / ".m2")}


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
        d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
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
        d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
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
        libs = Path(spec.project_dir) / "build" / "libs"
        jars = sorted(p.name for p in libs.glob("*.jar")) if libs.is_dir() else []
        return {"build/libs": ", ".join(jars[:5])} if jars else {}

    def result_info(self, spec: GradleParams) -> dict[str, Any]:
        return {"project_dir": spec.project_dir}
