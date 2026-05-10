#!/usr/bin/env python3
"""libvirt qemu.d filter for prepare/begin and start/begin lifecycle events.

prepare/begin — injects <bootmenu enable='yes'/> into domain XML (existing logic).

start/begin   — writes BootNext=HTTP into the NVRAM instance file.
                This sub-op fires synchronously AFTER libvirt has finished all
                domain preparation (NVRAM copied from template) and BEFORE QEMU
                is launched.  libvirt waits for the hook to exit before starting
                QEMU, so there is no race with OVMF reading the NVRAM.

                Note: prepare/end is NOT emitted by libvirt 11.x for the QEMU
                driver; start/begin is the correct window for NVRAM modification.

                Only acts when:
                  - /etc/libvirt/hooks/http-boot-uri contains the Satellite URI
                  - the NVRAM instance has no local FilePath OS entry (fresh VM)

See https://libvirt.org/hooks.html
"""

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Optional

_HTTP_BOOT_URI_FILE = "/etc/libvirt/hooks/http-boot-uri"


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _get_nvram_path(xml_data: str) -> Optional[str]:
    """Extract NVRAM instance path from domain XML."""
    try:
        root = ET.fromstring(xml_data)
        for child in root:
            if _local(child.tag) == "os":
                for item in child:
                    if _local(item.tag) == "nvram":
                        return item.text
    except ET.ParseError:
        pass
    return None


# ---------------------------------------------------------------------------
# 1. Bootmenu injection (prepare/begin — modifies XML)
# ---------------------------------------------------------------------------

def _inject_bootmenu(root: ET.Element) -> None:
    """Inject <bootmenu enable='yes'/> into <os> if missing (in-place)."""
    os_el = None
    for child in root:
        if _local(child.tag) == "os":
            os_el = child
            break
    if os_el is None:
        return

    type_idx: Optional[int] = None
    for i, child in enumerate(list(os_el)):
        ln = _local(child.tag)
        if ln == "type":
            type_idx = i
        if ln == "bootmenu" and child.get("enable") == "yes":
            return  # already present

    for child in list(os_el):
        if _local(child.tag) == "bootmenu":
            os_el.remove(child)

    bootmenu = ET.Element("bootmenu")
    bootmenu.set("enable", "yes")
    insert_at = (type_idx + 1) if type_idx is not None else 0
    os_el.insert(insert_at, bootmenu)


# ---------------------------------------------------------------------------
# 2. NVRAM HTTP BootNext pre-seeding (start/begin — modifies NVRAM file)
# ---------------------------------------------------------------------------

def _get_http_boot_uri() -> str:
    try:
        with open(_HTTP_BOOT_URI_FILE) as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _has_local_os_entry(nvram_path: str) -> bool:
    """Return True if NVRAM already has a local EFI binary boot entry (installed OS)."""
    try:
        result = subprocess.run(
            ["virt-fw-vars", "-i", nvram_path, "--print"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            if "FilePath" in line and any(
                kw in line.lower()
                for kw in ("shimx64.efi", "grubx64.efi", "bootx64.efi")
            ):
                return True
        return False
    except Exception:
        return False


def _preseed_http_bootnext(nvram_path: str, http_uri: str) -> None:
    """Write BootNext=HTTP into the NVRAM instance (called at start/begin)."""
    try:
        subprocess.run(
            ["virt-fw-vars", "--inplace", nvram_path, "--set-boot-uri", http_uri],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        pass


def _handle_start_begin(domain_name: str, xml_data: str) -> None:
    """Preseed NVRAM at start/begin — NVRAM exists, QEMU not yet started."""
    http_uri = _get_http_boot_uri()
    if not http_uri:
        return

    nvram_path = _get_nvram_path(xml_data) if xml_data.strip() else None
    if not nvram_path:
        nvram_path = f"/var/lib/libvirt/qemu/nvram/{domain_name}_VARS.qcow2"

    if not os.path.exists(nvram_path):
        return  # not a UEFI domain

    if _has_local_os_entry(nvram_path):
        return  # installed OS — leave NVRAM alone

    _preseed_http_bootnext(nvram_path, http_uri)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    domain_name = sys.argv[1] if len(sys.argv) > 1 else ""
    op = sys.argv[2] if len(sys.argv) > 2 else ""
    sub = sys.argv[3] if len(sys.argv) > 3 else ""

    if op == "prepare" and sub == "begin":
        # Modify domain XML: inject bootmenu
        data = sys.stdin.read()
        if not data.strip():
            return
        try:
            root = ET.fromstring(data)
            _inject_bootmenu(root)
            sys.stdout.write(ET.tostring(root, encoding="unicode"))
        except ET.ParseError:
            sys.stdout.write(data)

    elif op == "start" and sub == "begin":
        # NVRAM already exists (libvirt set it up during prepare phase).
        # libvirt waits for this hook to exit before launching QEMU,
        # so writing BootNext here is race-free.
        data = sys.stdin.read()
        sys.stdout.write(data)  # pass XML through unchanged
        _handle_start_begin(domain_name, data)

    else:
        shutil.copyfileobj(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
