#!/usr/bin/env bash
# Enforce the host GPU device policy: 0660 root:citespec-gpu, caps directory 0750.
#
# NVIDIA's devices are not managed by udev on this host (there is no matching
# /sys/dev/char entry), and nvidia-smi/nvidia-modprobe recreate or chmod them
# to 0666 root:root.  This script is run at boot before the broker, and by the
# nvidia-modprobe fallback after a real device creation.
set -euo pipefail

if ! getent group citespec-gpu >/dev/null 2>&1; then
    echo "citespec-gpu group is missing" >&2
    exit 1
fi

shopt -s nullglob
devices=()
for node in /dev/nvidia* /dev/nvidia-caps/*; do
    [ -e "$node" ] || continue
    # /dev/nvidia-caps itself is a directory; it must remain traversable by
    # root only, while the capability character devices follow the 0660 rule.
    [ -d "$node" ] && continue
    devices+=("$node")
done
if [ ${#devices[@]} -eq 0 ] && [ ! -d /dev/nvidia-caps ]; then
    # Driver not loaded yet; the fallback wrapper will run again on creation.
    exit 0
fi

if [ -d /dev/nvidia-caps ]; then
    chgrp citespec-gpu /dev/nvidia-caps
    chmod 0750 /dev/nvidia-caps
fi
if [ ${#devices[@]} -gt 0 ]; then
    chgrp citespec-gpu "${devices[@]}"
    chmod 0660 "${devices[@]}"
fi
