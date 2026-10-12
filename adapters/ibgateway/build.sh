#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
gateway_home=${1:-/usr/share/ib-tws}
mkdir -p build
staging=$(mktemp -d build/.agent-XXXXXX)
trap 'rm -rf -- "$staging"' EXIT
javac --release 17 -cp "$gateway_home/jars/*" -d "$staging/classes" src/*.java
printf 'Premain-Class: zond.ibgateway.LoginAgent\n' > "$staging/manifest.mf"
jar --create --file "$staging/zond-ibgateway.jar" --manifest "$staging/manifest.mf" -C "$staging/classes" .
mv -f -- "$staging/zond-ibgateway.jar" build/zond-ibgateway.jar
