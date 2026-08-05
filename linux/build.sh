#!/bin/bash
# Build the OST native probe (N1, watch-only) and the native hook (N2, active).
# Run this once on the Linux machine.
set -e
cd "$(dirname "$0")"

gcc -shared -fPIC -O2 -o ost_native.so      ost_native_probe.c -ldl -lpthread
gcc -shared -fPIC -O2 -o ost_native_hook.so ost_native_hook.c  -ldl -lpthread

chmod +x ost-run.sh
echo "built:"
echo "  $(pwd)/ost_native.so        (N1 probe, watch-only)"
echo "  $(pwd)/ost_native_hook.so   (N2 hook, active)"
echo
echo "ost-run.sh preloads the N2 hook. Set the game's Steam launch options to:"
echo "  $(pwd)/ost-run.sh %command%"
