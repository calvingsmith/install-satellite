#!/usr/bin/env python3
"""libvirt qemu.d filter for prepare/begin and start/begin lifecycle events.

prepare/begin — injects <bootmenu enable='yes'/> into domain XML (existing logic).

start/begin   — writes BootNext=HTTP into the NVRAM instance file for fresh VMs,
                or reorders BootOrder to prefer disk when an installed OS entry exists.

See https://libvirt.org/hooks.html
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional

from virt.firmware.bootcfg.bootcfg import VarStoreEfiBootConfig

_HTTP_BOOT_URI_FILE = "/etc/libvirt/hooks/http-boot-uri"
_NO_HTTPBOOT_FLAG_DIR = "/etc/libvirt/hooks"
_NVRAM_DIR = "/var/lib/libvirt/qemu/nvram"


def _no_httpboot_flag(domain_name: str) -> str:
    return os.path.join(_NO_HTTPBOOT_FLAG_DIR, f"no-httpboot-{domain_name}")


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _default_nvram_path(domain_name: str) -> str:
    return os.path.join(_NVRAM_DIR, f"{domain_name}_VARS.qcow2")


def _get_nvram_path(xml_data: str, domain_name: str = "") -> Optional[str]:
    if xml_data.strip():
        try:
            root = ET.fromstring(xml_data)
            for child in root:
                if _local(child.tag) == "os":
                    for item in child:
                        if _local(item.tag) == "nvram":
                            return item.text
        except ET.ParseError:
            pass
    if domain_name:
        return _default_nvram_path(domain_name)
    return None


def _inject_bootmenu(root: ET.Element) -> None:
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
            return

    for child in list(os_el):
        if _local(child.tag) == "bootmenu":
            os_el.remove(child)

    bootmenu = ET.Element("bootmenu")
    bootmenu.set("enable", "yes")
    insert_at = (type_idx + 1) if type_idx is not None else 0
    os_el.insert(insert_at, bootmenu)


def _remove_network_boot_devices(root: ET.Element) -> bool:
    """Drop <boot dev='network'/> once the guest is provisioned (prevents OVMF re-adding PXE/HTTP entries)."""
    os_el = None
    for child in root:
        if _local(child.tag) == "os":
            os_el = child
            break
    if os_el is None:
        return False

    removed = False
    for child in list(os_el):
        if _local(child.tag) == "boot" and child.get("dev") == "network":
            os_el.remove(child)
            removed = True
    return removed


def _prepare_domain_xml(domain_name: str, data: str) -> str:
    if not data.strip():
        return data
    try:
        root = ET.fromstring(data)
        _inject_bootmenu(root)
        if os.path.exists(_no_httpboot_flag(domain_name)):
            _remove_network_boot_devices(root)
        return ET.tostring(root, encoding="unicode")
    except ET.ParseError:
        return data


def _get_http_boot_uri() -> str:
    try:
        with open(_HTTP_BOOT_URI_FILE, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _has_local_os_entry(nvram_path: str) -> bool:
    if not os.path.exists(nvram_path):
        return False
    try:
        cfg = VarStoreEfiBootConfig(nvram_path)
        if not cfg.varstore or not cfg.varlist:
            return False
        return any(_is_preferred_disk_entry(cfg, nr) for nr in cfg.bentr.keys())
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        return False


def _entry_title(cfg: VarStoreEfiBootConfig, nr: int) -> str:
    entry = cfg.bentr.get(nr)
    return str(entry.title).lower() if entry and entry.title else ""


def _entry_path(cfg: VarStoreEfiBootConfig, nr: int) -> str:
    entry = cfg.bentr.get(nr)
    return str(entry.devicepath).lower() if entry and entry.devicepath else ""


def _is_network_entry(cfg: VarStoreEfiBootConfig, nr: int) -> bool:
    title = _entry_title(cfg, nr)
    path = _entry_path(cfg, nr)
    if any(token in title for token in ("pxe", "ipxe", "tftp", "http", "uefi:ipv4", "uefi:ipv6")):
        return True
    return "uri()" in path


def _is_netboot_hook_entry(cfg: VarStoreEfiBootConfig, nr: int) -> bool:
    title = _entry_title(cfg, nr)
    return "netboot" in title or "grubx64-httpboot" in title


def _is_preferred_disk_entry(cfg: VarStoreEfiBootConfig, nr: int) -> bool:
    if _is_network_entry(cfg, nr):
        return False
    title = _entry_title(cfg, nr)
    path = _entry_path(cfg, nr)
    if any(token in path for token in ("shimx64.efi", "grubx64.efi", "bootx64.efi")):
        return True
    return any(
        token in title
        for token in ("red hat", "shim", "uefi os", "fedora", "rocky", "debian", "qemu hard", "q35", "hd(")
    )


def prefer_disk_boot_nvram(nvram_path: str) -> dict[str, Any]:
    """Reorder guest NVRAM BootOrder to prefer disk over network/HTTP entries."""
    if not os.path.exists(nvram_path):
        return {"changed": False, "reason": "missing_nvram", "path": nvram_path}

    cfg = VarStoreEfiBootConfig(nvram_path)
    if not cfg.varstore or not cfg.varlist:
        return {"changed": False, "reason": "unreadable_nvram", "path": nvram_path}

    bootorder_before = [f"{nr:04X}" for nr in cfg.blist]
    changed = False
    has_disk = any(_is_preferred_disk_entry(cfg, nr) for nr in cfg.bentr.keys())

    for nr in list(cfg.bentr.keys()):
        title = _entry_title(cfg, nr)
        delete_entry = False
        if _is_netboot_hook_entry(cfg, nr):
            delete_entry = True
        elif has_disk and _is_network_entry(cfg, nr):
            delete_entry = True
        elif _is_network_entry(cfg, nr) and any(token in title for token in ("ipv6", "pxev6", "httpv6")):
            delete_entry = True
        if delete_entry:
            cfg.remove_entry(nr)
            if cfg.varlist.get(f"Boot{nr:04X}"):
                cfg.varlist.delete(f"Boot{nr:04X}")
            changed = True

    new_blist: list[int] = []
    for nr in cfg.blist:
        if _is_preferred_disk_entry(cfg, nr) and nr not in new_blist:
            new_blist.append(nr)
    for nr in cfg.blist:
        if nr not in new_blist:
            new_blist.append(nr)

    if new_blist != cfg.blist:
        cfg.blist = new_blist
        cfg.blist_updated = True
        changed = True

    if cfg.bnext is not None and (
        _is_network_entry(cfg, cfg.bnext) or _is_netboot_hook_entry(cfg, cfg.bnext)
    ):
        cfg.bnext = None
        cfg.bnext_updated = True
        changed = True

    if not changed:
        return {
            "changed": False,
            "path": nvram_path,
            "bootorder_before": bootorder_before,
            "bootorder_after": bootorder_before,
        }

    if cfg.blist_updated:
        boot_order_var = cfg.varlist.get("BootOrder")
        if not boot_order_var:
            boot_order_var = cfg.varlist.create("BootOrder")
        boot_order_var.set_boot_order(cfg.blist)

    if cfg.bnext_updated:
        if cfg.bnext is None:
            cfg.varlist.delete("BootNext")
        else:
            boot_next_var = cfg.varlist.get("BootNext")
            if not boot_next_var:
                boot_next_var = cfg.varlist.create("BootNext")
            boot_next_var.set_boot_next(cfg.bnext)

    cfg.varstore.write_varstore(nvram_path, cfg.varlist)
    bootorder_after = [f"{nr:04X}" for nr in cfg.blist]
    return {
        "changed": True,
        "path": nvram_path,
        "bootorder_before": bootorder_before,
        "bootorder_after": bootorder_after,
    }


def _preseed_http_bootnext(nvram_path: str, http_uri: str) -> None:
    try:
        subprocess.run(
            ["virt-fw-vars", "--inplace", nvram_path, "--set-boot-uri", http_uri],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _handle_start_begin(domain_name: str, xml_data: str) -> None:
    nvram_path = _get_nvram_path(xml_data, domain_name)
    if not nvram_path or not os.path.exists(nvram_path):
        return

    has_os = _has_local_os_entry(nvram_path)

    if has_os:
        prefer_disk_boot_nvram(nvram_path)
        return

    http_uri = _get_http_boot_uri()
    if not http_uri:
        return
    if os.path.exists(_no_httpboot_flag(domain_name)):
        return

    _preseed_http_bootnext(nvram_path, http_uri)


def _ready_marker_path(domain_name: str, ready_dir: str = "/run/provision-demo") -> str:
    return os.path.join(ready_dir, f"{domain_name}.ready")


def _remove_network_boot_from_domain_xml(domain_name: str) -> bool:
    """Drop <boot dev='network'/> from a stopped guest domain definition."""
    try:
        result = subprocess.run(
            ["virsh", "dumpxml", domain_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        root = ET.fromstring(result.stdout)
        if not _remove_network_boot_devices(root):
            return False
        xml_out = ET.tostring(root, encoding="unicode")
        define = subprocess.run(
            ["virsh", "define", "/dev/stdin"],
            input=xml_out,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return define.returncode == 0
    except (ET.ParseError, OSError, subprocess.SubprocessError):
        return False


def prefer_disk_boot_for_domain(
    domain_name: str,
    *,
    grace_seconds: int = 90,
    retry_interval: int = 30,
    max_retries: int = 120,
    ready_dir: str = "/run/provision-demo",
) -> dict[str, Any]:
    """Stop guest, wait for OS NVRAM entry, fix boot order, and restart."""
    ready_path = _ready_marker_path(domain_name, ready_dir)
    try:
        os.makedirs(ready_dir, mode=0o755, exist_ok=True)
        if os.path.exists(ready_path):
            os.remove(ready_path)
    except OSError:
        pass

    if grace_seconds > 0:
        time.sleep(grace_seconds)

    nvram_path = _default_nvram_path(domain_name)
    has_os = False
    for attempt in range(max_retries):
        subprocess.run(["virsh", "destroy", domain_name], capture_output=True, timeout=30, check=False)
        time.sleep(2)
        has_os = _has_local_os_entry(nvram_path)
        if has_os:
            break
        if attempt + 1 < max_retries:
            time.sleep(retry_interval)

    if not has_os:
        return {
            "changed": False,
            "domain": domain_name,
            "reason": "no_local_os_entry",
            "path": nvram_path,
        }

    nvram_result = prefer_disk_boot_nvram(nvram_path)
    xml_changed = _remove_network_boot_from_domain_xml(domain_name)
    try:
        open(_no_httpboot_flag(domain_name), "a", encoding="utf-8").close()
        os.chmod(_no_httpboot_flag(domain_name), 0o644)
    except OSError:
        pass

    start = subprocess.run(["virsh", "start", domain_name], capture_output=True, timeout=30, check=False)
    try:
        with open(ready_path, "w", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())}\n")
    except OSError:
        pass

    return {
        "changed": bool(nvram_result.get("changed") or xml_changed or start.returncode == 0),
        "domain": domain_name,
        "nvram": nvram_result,
        "xml_network_boot_removed": xml_changed,
        "started": start.returncode == 0,
        "ready_marker": ready_path,
    }


def _cli_fix_domain(domain_name: str) -> int:
    nvram_path = _default_nvram_path(domain_name)
    result = prefer_disk_boot_nvram(nvram_path)
    print(json.dumps(result))
    return 0 if result.get("changed") or result.get("reason") != "unreadable_nvram" else 1


def _cli_prefer_disk_boot_for_domain(args: argparse.Namespace) -> int:
    result = prefer_disk_boot_for_domain(
        args.prefer_disk_boot_for_domain,
        grace_seconds=args.grace,
        retry_interval=args.retry_interval,
        max_retries=args.max_retries,
        ready_dir=args.ready_dir,
    )
    print(json.dumps(result))
    if result.get("reason") == "no_local_os_entry":
        return 1
    return 0


def _cli_check_local_os(domain_name: str) -> int:
    nvram_path = _default_nvram_path(domain_name)
    has_os = _has_local_os_entry(nvram_path)
    print(json.dumps({"domain": domain_name, "has_local_os": has_os, "path": nvram_path}))
    return 0 if has_os else 1


def _cli_main() -> int:
    parser = argparse.ArgumentParser(description="libvirt boot hook and NVRAM helpers")
    parser.add_argument("--fix-nvram-for-domain", metavar="DOMAIN")
    parser.add_argument("--check-local-os", metavar="DOMAIN")
    parser.add_argument("--prefer-disk-boot-for-domain", metavar="DOMAIN")
    parser.add_argument("--grace", type=int, default=90, help="Seconds to wait after build_exited before probing NVRAM")
    parser.add_argument("--retry-interval", type=int, default=30, help="Seconds between NVRAM probes while kickstart runs")
    parser.add_argument("--max-retries", type=int, default=120, help="Maximum NVRAM probe attempts")
    parser.add_argument("--ready-dir", default="/run/provision-demo", help="Directory for .ready marker files")
    parser.add_argument("domain", nargs="?", default="")
    parser.add_argument("op", nargs="?", default="")
    parser.add_argument("sub", nargs="?", default="")
    args, _unknown = parser.parse_known_args()

    if args.fix_nvram_for_domain:
        return _cli_fix_domain(args.fix_nvram_for_domain)
    if args.check_local_os:
        return _cli_check_local_os(args.check_local_os)
    if args.prefer_disk_boot_for_domain:
        return _cli_prefer_disk_boot_for_domain(args)

    domain_name = args.domain
    op = args.op
    sub = args.sub

    if op == "prepare" and sub == "begin":
        data = sys.stdin.read()
        sys.stdout.write(_prepare_domain_xml(domain_name, data))
        return 0

    if op == "start" and sub == "begin":
        data = sys.stdin.read()
        sys.stdout.write(data)
        _handle_start_begin(domain_name, data)
        return 0

    shutil.copyfileobj(sys.stdin, sys.stdout)
    return 0


def main() -> None:
    raise SystemExit(_cli_main())


if __name__ == "__main__":
    main()
