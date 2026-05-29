#!/usr/bin/env bash
# Two-phase UEFI BootOrder cleanup for OVMF/libvirt provisioning labs.
#
# Phase 1 – DELETE all IPv6 PXE boot entries.
#   OVMF's DHCPv6 probing takes ~60 s when there is no DHCPv6 server.
#   Removing these entries eliminates that wait on every subsequent boot and re-provision
#   (provided libvirt/Satellite preserves the NVRAM file across delete/recreate cycles,
#   which fog-libvirt does by calling virDomainUndefine without VIR_DOMAIN_UNDEFINE_NVRAM).
#
# Phase 2 – REORDER remaining BootOrder to prefer disk/OS before network/HTTP/PXE.
#   Prevents the installed system from looping back into HTTP/PXE boot.
#
# No-op on BIOS systems, missing efibootmgr, or if already correct.
# Mirrors templates/prov_template_snippet_soe_uefi_prefer_disk_boot.j2.
set -euo pipefail

command -v efibootmgr >/dev/null 2>&1 || exit 0
[[ -d /sys/firmware/efi ]] || exit 0

list=$(efibootmgr 2>/dev/null) || exit 0

declare -A title
while IFS= read -r line; do
  if [[ $line =~ ^Boot([0-9A-Fa-f]+)\*?[[:space:]]+(.+)$ ]]; then
    title["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
  fi
done < <(printf '%s\n' "$list" | grep -E '^Boot[0-9A-Fa-f]+')

is_network() {
  local t=${1,,}
  [[ $t =~ (pxe|ipxe) ]] && return 0
  [[ $t =~ tftp ]] && return 0
  [[ $t =~ http ]] && return 0
  [[ $t =~ uefi:ipv(4|6) ]] && return 0
  return 1
}

# Phase 1: delete IPv6 PXE entries
_ipv6_deleted=0
for id in "${!title[@]}"; do
  t="${title[$id]}"
  tl="${t,,}"
  if [[ $tl =~ ipv6|pxev6|httpv6 ]] && is_network "$tl"; then
    efibootmgr -b "$id" -B 2>/dev/null && _ipv6_deleted=1 || true
  fi
done

if [[ $_ipv6_deleted -eq 1 ]]; then
  list=$(efibootmgr 2>/dev/null) || exit 0
  unset title; declare -A title
  while IFS= read -r line; do
    if [[ $line =~ ^Boot([0-9A-Fa-f]+)\*?[[:space:]]+(.+)$ ]]; then
      title["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
    fi
  done < <(printf '%s\n' "$list" | grep -E '^Boot[0-9A-Fa-f]+')
fi

is_netboot_hook_entry() {
  local t=${1,,}
  [[ $t =~ netboot|grubx64-httpboot ]] && return 0
  return 1
}

# Phase 2: delete libvirt-hook netboot entries (BootNext target for HTTP reprovision)
_netboot_deleted=0
for id in "${!title[@]}"; do
  t="${title[$id]}"
  if is_netboot_hook_entry "$t"; then
    efibootmgr -b "$id" -B 2>/dev/null && _netboot_deleted=1 || true
  fi
done

if [[ $_netboot_deleted -eq 1 ]]; then
  list=$(efibootmgr 2>/dev/null) || exit 0
  unset title; declare -A title
  while IFS= read -r line; do
    if [[ $line =~ ^Boot([0-9A-Fa-f]+)\*?[[:space:]]+(.+)$ ]]; then
      title["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
    fi
  done < <(printf '%s\n' "$list" | grep -E '^Boot[0-9A-Fa-f]+')
fi

# Phase 2b: when a disk OS entry exists, remove all network/HTTP/PXE entries so OVMF
# cannot loop back into provisioning after install (OVMF may recreate them on failed boots).
_has_disk=0
for id in "${!title[@]}"; do
  if is_preferred_os_disk "${title[$id]}"; then
    _has_disk=1
    break
  fi
done
if [[ $_has_disk -eq 1 ]]; then
  _net_deleted=0
  for id in "${!title[@]}"; do
    if is_network "${title[$id],,}"; then
      efibootmgr -b "$id" -B 2>/dev/null && _net_deleted=1 || true
    fi
  done
  if [[ $_net_deleted -eq 1 ]]; then
    list=$(efibootmgr 2>/dev/null) || exit 0
    unset title; declare -A title
    while IFS= read -r line; do
      if [[ $line =~ ^Boot([0-9A-Fa-f]+)\*?[[:space:]]+(.+)$ ]]; then
        title["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
      fi
    done < <(printf '%s\n' "$list" | grep -E '^Boot[0-9A-Fa-f]+')
  fi
fi

# Phase 3: reorder — disk/OS first, then network
bline=$(printf '%s\n' "$list" | grep -E '^BootOrder:' || true)
[[ -n "$bline" ]] || exit 0
order_str=${bline#BootOrder:}
order_str=${order_str//[[:space:]]/}
[[ -n "$order_str" ]] || exit 0
IFS=',' read -r -a cur <<< "$order_str"

is_preferred_os_disk() {
  local t=${1,,}
  is_network "$t" && return 1
  [[ $t =~ (red[[:space:]]hat|shim|uefi[[:space:]]os) ]] && return 0
  [[ $t =~ (fedora|rocky|debian) ]] && return 0
  [[ $t =~ (qemu[[:space:]]hard|q35|hd\() ]] && return 0
  return 1
}

new=()
for id in "${cur[@]}"; do
  t="${title[$id]-}"
  [[ -n $t ]] || continue
  if is_preferred_os_disk "$t"; then
    new+=("$id")
  fi
done
for id in "${cur[@]}"; do
  for x in "${new[@]}"; do [[ $x == "$id" ]] && continue 2; done
  new+=("$id")
done

new_str=$(IFS=,; echo "${new[*]}")
_changed=0
if [[ "$order_str" != "$new_str" ]]; then
  efibootmgr -o "$new_str" && _changed=1 || true
  list=$(efibootmgr 2>/dev/null) || exit 0
fi

# Phase 4: clear BootNext when it targets network/HTTP/netboot (overrides BootOrder on reboot)
bnext_line=$(printf '%s\n' "$list" | grep -E '^BootNext:' || true)
if [[ -n "$bnext_line" ]]; then
  bnext_id=${bnext_line#BootNext:}
  bnext_id=${bnext_id//[[:space:]]/}
  bnext_title="${title[$bnext_id]-}"
  if [[ -n "$bnext_id" ]] && { is_network "${bnext_title,,}" || is_netboot_hook_entry "$bnext_title"; }; then
    efibootmgr --delete-bootnext 2>/dev/null && _changed=1 || true
    echo "cleared BootNext ($bnext_id: $bnext_title)"
  fi
fi

if [[ $_changed -eq 1 ]]; then
  efibootmgr
fi
