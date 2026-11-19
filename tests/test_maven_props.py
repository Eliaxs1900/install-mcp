"""Auditoría #123 (propiedades de Maven que escriben fuera de las raíces) y #106 (project_dir con ~)."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.backends.jvm import GradleBackend, MavenBackend
from watcher_mcp.config import Config


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    (h / "work" / "proj").mkdir(parents=True)
    (h / "work" / "proj" / "pom.xml").write_text("<project/>")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


def _maven(home: Path, **opts) -> MavenBackend:
    cfg = Config(state_dir=home.parent / "state", allowed_backends=["maven"], allowed_roots=[home / "work"],
                 backends={"maven": {"mvn_path": "/bin/echo", **opts}})
    return MavenBackend(cfg)


def _v(mvn: MavenBackend, home: Path, props: list[str], goals=("dependency:tree",)):
    return mvn.validate({"project_dir": str(home / "work" / "proj"), "goals": list(goals), "properties": props})


@pytest.mark.parametrize("prop", [
    "outputFile=/Users/yo/.zshrc",            # la reproducción de la auditoría
    "mdep.outputFile=out.txt",
    "outputDirectory=x",
    "maven.repo.local=/tmp/repo",
    "project.build.directory=/tmp/t",
    "altDeploymentRepository=id::default::file:///x",
    "argLine=-javaagent:/tmp/evil.jar",
    "maven.ext.class.path=/tmp/x.jar",
    "user.home=/tmp", "java.io.tmpdir=/x",
    "OUTPUTFILE=x",                           # sin distinguir mayúsculas
])
def test_dangerous_properties_rejected(home, prop):
    with pytest.raises(ValueError, match="propiedad no permitida"):
        _v(_maven(home), home, [prop])


@pytest.mark.parametrize("prop", ["skipTests=true", "maven.surefire.debug=true", "revision=1.2.3",
                                  "maven.test.skip=true", "test=FooTest", "maven.compiler.release=21",
                                  "repo.url=https://repo.example.org/maven2"])
def test_harmless_properties_accepted(home, prop):
    _v(_maven(home), home, [prop])


def test_tilde_values_fail_the_shape_check(home):
    with pytest.raises(ValueError, match="no válido"):
        _v(_maven(home), home, ["outputFile=~/.zshrc"])


def test_leading_dash_value_rejected(home):
    with pytest.raises(ValueError, match="empezar por '-'"):
        _v(_maven(home), home, ["foo=-javaagent:/x.jar"])


def test_path_values_go_through_roots_and_protected_zones(home):
    mvn = _maven(home)
    _v(mvn, home, ["foo.bar=" + str(home / "work" / "ok.txt")])           # dentro de las raíces
    _v(mvn, home, ["foo.bar=sub/ok.txt"])                                  # relativo: contra el proyecto
    for bad in ["/etc/passwd", "../../../x", str(home / "work" / ".ssh" / "k")]:
        with pytest.raises(ValueError, match="ruta de la propiedad"):
            _v(mvn, home, ["foo.bar=" + bad])


def test_properties_allow_opens_a_key_but_still_checks_the_path(home):
    mvn = _maven(home, properties_allow=["maven.repo.local"])
    _v(mvn, home, [f"maven.repo.local={home / 'work' / 'repo'}"])
    with pytest.raises(ValueError, match="ruta de la propiedad"):
        _v(mvn, home, ["maven.repo.local=/tmp/repo"])


def test_artifacts_mode_checks_properties_too(home):
    mvn = _maven(home)
    with pytest.raises(ValueError, match="propiedad no permitida"):
        mvn.validate({"artifacts": ["org.slf4j:slf4j-api:2.0.16"], "properties": ["outputFile=x"]})
    with pytest.raises(ValueError, match="ruta relativa"):
        mvn.validate({"artifacts": ["org.slf4j:slf4j-api:2.0.16"], "properties": ["foo=a/b"]})


def test_gradle_has_no_property_parameter(home):
    assert "properties" not in GradleBackend.Params.model_fields  # sin -D/-P: nada que validar


# ------------------------------------------------------------------ #106: ~ en project_dir
def test_tilde_project_dir_finds_wrapper_pom_and_jars(home):
    proj = home / "work" / "proj"
    (proj / "mvnw").write_text("#!/bin/sh\n")
    (proj / "mvnw").chmod(0o755)
    (proj / "target").mkdir()
    (proj / "target" / "a.jar").write_bytes(b"")
    mvn = _maven(home)
    spec = mvn.validate({"project_dir": "~/work/proj", "goals": ["package"], "use_wrapper": "always"})
    assert spec.project_dir == str(proj)
    assert mvn.plan(spec).steps[0].argv[0] == str(proj / "mvnw")
    assert mvn.result_info(spec)["built"] == ["target/a.jar"]
    assert mvn.result_info(spec)["project_dir"] == str(proj)


def test_tilde_project_dir_gradle(home):
    proj = home / "work" / "g"
    proj.mkdir()
    (proj / "settings.gradle").write_text("")
    (proj / "gradlew").write_text("#!/bin/sh\n")
    cfg = Config(state_dir=home.parent / "state", allowed_backends=["gradle"], allowed_roots=[home / "work"])
    g = GradleBackend(cfg)
    spec = g.validate({"project_dir": "~/work/g", "use_wrapper": "always"})
    assert spec.project_dir == str(proj)
    assert g.plan(spec).steps[0].argv[0] == str(proj / "gradlew")


def test_relative_project_dir_still_rejected(home):
    with pytest.raises(ValueError, match="absoluta"):
        _maven(home).validate({"project_dir": "work/proj"})
