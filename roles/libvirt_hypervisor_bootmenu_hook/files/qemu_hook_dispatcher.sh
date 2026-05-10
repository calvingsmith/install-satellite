#!/bin/bash
# Dispatcher: libvirt calls /etc/libvirt/hooks/qemu; this script fans out to qemu.d/*.
# Each hook receives the current data on stdin and its stdout becomes stdin for the next hook.
# Arguments ($@) are passed through unchanged: <domain> <operation> <sub-operation> [extra-arg]
DATA=$(cat)
for hook in /etc/libvirt/hooks/qemu.d/*; do
    [ -x "$hook" ] || continue
    DATA=$(printf '%s' "$DATA" | "$hook" "$@")
done
printf '%s' "$DATA"
