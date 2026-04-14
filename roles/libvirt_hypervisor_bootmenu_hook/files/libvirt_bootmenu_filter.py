#!/usr/bin/env python3
"""libvirt qemu.d filter: inject <bootmenu enable='yes'/> under <os> when missing.

Invoked for the prepare begin phase; other phases pass stdin to stdout unchanged.
See https://libvirt.org/hooks.html
"""

import shutil
import sys
import xml.etree.ElementTree as ET
from typing import Optional


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _inject_bootmenu(data: str) -> str:
    root = ET.fromstring(data)
    os_el = None
    for child in root:
        if _local(child.tag) == "os":
            os_el = child
            break
    if os_el is None:
        return data

    type_idx: Optional[int] = None
    for i, child in enumerate(list(os_el)):
        ln = _local(child.tag)
        if ln == "type":
            type_idx = i
        if ln == "bootmenu" and child.get("enable") == "yes":
            return data

    for child in list(os_el):
        if _local(child.tag) == "bootmenu":
            os_el.remove(child)

    bootmenu = ET.Element("bootmenu")
    bootmenu.set("enable", "yes")
    insert_at = (type_idx + 1) if type_idx is not None else 0
    os_el.insert(insert_at, bootmenu)

    return ET.tostring(root, encoding="unicode")


def main() -> None:
    op = sys.argv[2] if len(sys.argv) > 2 else ""
    sub = sys.argv[3] if len(sys.argv) > 3 else ""

    if op == "prepare" and sub == "begin":
        data = sys.stdin.read()
        if not data.strip():
            return
        try:
            out = _inject_bootmenu(data)
        except ET.ParseError:
            out = data
        sys.stdout.write(out)
    else:
        shutil.copyfileobj(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
