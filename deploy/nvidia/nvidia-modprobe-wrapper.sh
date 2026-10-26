#!/bin/sh
# Citespec hardening wrapper for nvidia-modprobe.
#
# NVIDIA tools call nvidia-modprobe even when the device nodes already exist;
# the stock helper can recreate them with 0666 root:root.  Let existing nodes
# stand and only invoke the real helper when a required node is missing.  The
# real helper is invoked as root only; the setuid/setgid bits are removed by
# the installer so an unprivileged member cannot create host GPU nodes.
set -u
if [ -e /dev/nvidiactl ] && [ -e /dev/nvidia0 ] && [ -e /dev/nvidia-uvm ]; then
    exit 0
fi
if [ "$(id -u)" -eq 0 ]; then
    /usr/bin/nvidia-modprobe.real "$@"
    rc=$?
    if [ "$rc" -eq 0 ] && [ -x /usr/local/libexec/citespec-resource/fix_gpu_permissions.sh ]; then
        /usr/local/libexec/citespec-resource/fix_gpu_permissions.sh || true
    fi
    exit "$rc"
fi
exec /usr/bin/nvidia-modprobe.real "$@"
