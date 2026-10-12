from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parents[1] / "adapters/ibgateway"
spec = importlib.util.spec_from_file_location("gateway_launch", ADAPTER / "launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    adapter = tmp_path / "adapter with spaces"
    (adapter / "src").mkdir(parents=True)
    shutil.copy(ADAPTER / "build.sh", adapter)
    gateway = tmp_path / "gateway with spaces"
    (gateway / "jre/bin").mkdir(parents=True)
    (gateway / "jre/bin/java").touch()
    (gateway / "jars").mkdir()
    monkeypatch.setattr(launch, "ADAPTER", adapter)
    monkeypatch.setenv("ZOND_ADAPTER_PROTOCOL", "1")
    monkeypatch.chdir(tmp_path)
    launches = []
    monkeypatch.setattr(launch.os, "execv", lambda executable, argv: launches.append(argv))

    def start(*extra):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(ADAPTER / "launch.py"),
                "--gateway-home",
                str(gateway),
                "--settings-dir",
                str(tmp_path / "settings"),
                "--mode",
                "paper",
                "--api-port",
                "14002",
                *extra,
            ],
        )
        launch.main()

    return adapter, start, launches


requires_jdk = pytest.mark.skipif(
    not shutil.which("javac") or not shutil.which("jar"), reason="requires a JDK"
)


@requires_jdk
def test_startup_builds_missing_and_changed_sources_without_stale_classes(checkout):
    adapter, start, launches = checkout
    source = adapter / "src/LoginAgent.java"
    source.write_text('public class LoginAgent { public String version = "original"; }')
    old_time = source.stat().st_mtime_ns
    removed = adapter / "src/Removed.java"
    removed.write_text("public class Removed {}")
    jar = adapter / "build/zond-ibgateway.jar"
    assert not jar.exists()
    start()
    with zipfile.ZipFile(jar) as built:
        assert b"original" in built.read("LoginAgent.class")
        assert "Removed.class" in built.namelist()
    source.write_text('public class LoginAgent { public String version = "updated"; }')
    os.utime(source, ns=(old_time, old_time))
    removed.unlink()
    start()
    with zipfile.ZipFile(jar) as built:
        assert b"updated" in built.read("LoginAgent.class")
        assert "Removed.class" not in built.namelist()
    assert len(launches) == 2
    assert f"-javaagent:{jar}=paper:14002" in launches[-1]
    assert not list(jar.parent.glob(".agent-*"))


@requires_jdk
def test_failed_build_preserves_jar_and_does_not_launch_stale_agent(checkout, capfd):
    adapter, start, launches = checkout
    source = adapter / "src/LoginAgent.java"
    source.write_text("public class LoginAgent {}")
    start()
    jar = adapter / "build/zond-ibgateway.jar"
    previous = jar.read_bytes()
    launches.clear()
    source.write_text("this does not compile")
    with pytest.raises(SystemExit) as failure:
        start()
    assert failure.value.code != 0
    assert launches == []
    assert jar.read_bytes() == previous
    assert not list(jar.parent.glob(".agent-*"))
    output = capfd.readouterr()
    assert output.out == ""
    assert "could not build the Gateway adapter" in output.err


def test_explicit_agent_jar_skips_build_and_build_output_stays_off_protocol(checkout, capfd):
    adapter, start, launches = checkout
    jar = adapter / "prebuilt.jar"
    jar.write_bytes(b"prebuilt")
    build = adapter / "build.sh"
    build.write_text("#!/bin/sh\nprintf 'build diagnostic\\n'\nexit 1\n")
    start("--agent-jar", str(jar))
    assert jar.read_bytes() == b"prebuilt"
    assert f"-javaagent:{jar}=paper:14002" in launches[-1]
    launches.clear()
    with pytest.raises(SystemExit):
        start()
    assert launches == []
    output = capfd.readouterr()
    assert output.out == ""
    assert "build diagnostic" in output.err
