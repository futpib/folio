#!/usr/bin/env python3
"""Run IB Gateway with a private zond protocol and an invisible display."""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-home", type=Path, default=Path("/usr/share/ib-tws"))
    parser.add_argument("--settings-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=["paper", "live"], required=True)
    parser.add_argument("--api-port", type=int, required=True)
    parser.add_argument(
        "--agent-jar",
        type=Path,
        default=Path(__file__).resolve().parent / "build/zond-ibgateway.jar",
    )
    args = parser.parse_args()
    if os.environ.get("ZOND_ADAPTER_PROTOCOL") != "1":
        parser.error("configure this executable as a zondd credential adapter")
    if not 1 <= args.api_port <= 65535 or not args.settings_dir.is_absolute():
        parser.error("an absolute settings directory and valid API port are required")
    java = args.gateway_home / "jre/bin/java"
    if not java.is_file() or not args.agent_jar.is_file():
        parser.error("install IB Gateway and run build.sh first")
    args.settings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        "/usr/bin/xvfb-run",
        "-a",
        "-e",
        "/dev/null",
        "-s",
        "-screen 0 1280x800x24 -nolisten tcp",
        str(java),
        "-Xmx768m",
        "-Duser.language=en",
        "-Duser.country=US",
        f"-javaagent:{args.agent_jar.resolve()}={args.mode}:{args.api_port}",
        "-cp",
        str(args.gateway_home / "jars/*"),
    ]
    for module in [
        "java.base/java.util",
        "java.base/java.util.concurrent",
        "java.desktop/java.awt",
        "java.desktop/java.awt.dnd",
        "java.desktop/javax.swing",
        "java.desktop/javax.swing.event",
        "java.desktop/javax.swing.plaf.basic",
        "java.desktop/javax.swing.table",
        "java.desktop/sun.awt",
        "java.desktop/sun.swing",
        "jdk.management/com.sun.management.internal",
    ]:
        command.append(f"--add-opens={module}=ALL-UNNAMED")
    command += [
        "--add-exports=java.base/sun.util=ALL-UNNAMED",
        "--add-exports=java.desktop/sun.awt.X11=ALL-UNNAMED",
        "ibgateway.GWClient",
        str(args.settings_dir),
    ]
    os.chdir(args.gateway_home / "jars")
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
