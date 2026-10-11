#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
gateway_home=${1:-/usr/share/ib-tws}
mkdir -p build/classes
javac --release 17 -cp "$gateway_home/jars/*" -d build/classes src/*.java
printf 'Premain-Class: zond.ibgateway.LoginAgent\n' > build/manifest.mf
jar --create --file build/zond-ibgateway.jar --manifest build/manifest.mf -C build/classes .
