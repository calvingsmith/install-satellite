#!/usr/bin/env python3
"""HTTP receiver for Foreman build_exited webhooks → hypervisor disk-boot fix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

_active_domains: dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


def _extract_hostname(payload: dict[str, Any]) -> str:
    for key in ("hostname", "host", "name", "fqdn"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return _extract_hostname(nested)
    return ""


def _domain_defined(domain: str) -> bool:
    result = subprocess.run(
        ["virsh", "dominfo", domain],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


def _run_fix(
    domain: str,
    script_path: str,
    grace: int,
    retry_interval: int,
    max_retries: int,
    ready_dir: str,
) -> None:
    try:
        subprocess.run(
            [
                sys.executable,
                script_path,
                "--prefer-disk-boot-for-domain",
                domain,
                "--grace",
                str(grace),
                "--retry-interval",
                str(retry_interval),
                "--max-retries",
                str(max_retries),
                "--ready-dir",
                ready_dir,
            ],
            check=False,
        )
    finally:
        with _active_lock:
            _active_domains.pop(domain, None)


class PreferDiskBootHandler(BaseHTTPRequestHandler):
    script_path: str = "/etc/libvirt/hooks/qemu.d/50-bootmenu"
    grace_seconds: int = 30
    retry_interval: int = 15
    max_retries: int = 20
    ready_dir: str = "/run/provision-demo"
    shared_secret: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _authorized(self) -> bool:
        if not self.shared_secret:
            return True
        return self.headers.get("X-Provision-Demo-Secret", "") == self.shared_secret

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path.rstrip("/") != "/prefer-disk-boot":
            self.send_error(404, "Not Found")
            return
        if not self._authorized():
            self.send_error(403, "Forbidden")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        hostname = _extract_hostname(payload if isinstance(payload, dict) else {})
        if not hostname:
            self.send_error(400, "Missing hostname")
            return
        if not _domain_defined(hostname):
            self.send_error(404, "Unknown libvirt domain")
            return

        with _active_lock:
            existing = _active_domains.get(hostname)
            if existing is not None and existing.is_alive():
                body = json.dumps({"accepted": True, "hostname": hostname, "deduplicated": True}).encode("utf-8")
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            worker = threading.Thread(
                target=_run_fix,
                args=(
                    hostname,
                    self.script_path,
                    self.grace_seconds,
                    self.retry_interval,
                    self.max_retries,
                    self.ready_dir,
                ),
                daemon=True,
                name=f"prefer-disk-boot-{hostname}",
            )
            _active_domains[hostname] = worker
            worker.start()

        body = json.dumps({"accepted": True, "hostname": hostname}).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Foreman build_exited webhook receiver for libvirt disk boot")
    parser.add_argument("--bind", default="192.168.122.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--script", default="/etc/libvirt/hooks/qemu.d/50-bootmenu")
    parser.add_argument("--grace", type=int, default=30)
    parser.add_argument("--retry-interval", type=int, default=15)
    parser.add_argument("--max-retries", type=int, default=20)
    parser.add_argument("--ready-dir", default="/run/provision-demo")
    parser.add_argument("--shared-secret", default=os.environ.get("PROVISION_DEMO_WEBHOOK_SECRET", ""))
    args = parser.parse_args()

    PreferDiskBootHandler.script_path = args.script
    PreferDiskBootHandler.grace_seconds = args.grace
    PreferDiskBootHandler.retry_interval = args.retry_interval
    PreferDiskBootHandler.max_retries = args.max_retries
    PreferDiskBootHandler.ready_dir = args.ready_dir
    PreferDiskBootHandler.shared_secret = args.shared_secret

    server = ThreadingHTTPServer((args.bind, args.port), PreferDiskBootHandler)
    sys.stderr.write(
        "prefer_disk_boot_webhook listening on http://%s:%s/prefer-disk-boot\n" % (args.bind, args.port)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
