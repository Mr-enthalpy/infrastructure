#!/bin/sh
# Run NVIDIA's real nvidia-smi with a shim that does not let the tool chmod
# host GPU device nodes away from the broker-only 0660 root:citespec-gpu policy.
set -eu
shim=/usr/local/lib/citespec-resource/libcitespec-nvidia-chmod.so
if [ -r "$shim" ]; then
    LD_PRELOAD="$shim${LD_PRELOAD:+:$LD_PRELOAD}"
    export LD_PRELOAD
fi
exec /usr/bin/nvidia-smi.real "$@"
