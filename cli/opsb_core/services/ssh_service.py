"""SSH configuration parser and managed host block writer (`~/.ssh/config`)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def parse_ssh_hosts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    ssh_path = Path.home() / ".ssh" / "config"
    if ssh_path.is_file():
        current: dict[str, Any] | None = None
        for raw_line in ssh_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            key, val = parts[0].lower(), parts[1].strip()
            if key == "host":
                if current and current.get("alias") and "*" not in current["alias"]:
                    hosts.append(current)
                current = {
                    "alias": val.split()[0],
                    "hostname": "",
                    "port": 22,
                    "user": "root",
                    "identity_file": "",
                    "source": "ssh_config",
                }
            elif current is not None:
                if key == "hostname":
                    current["hostname"] = val
                elif key == "port" and val.isdigit():
                    current["port"] = int(val)
                elif key == "user":
                    current["user"] = val
                elif key == "identityfile":
                    current["identity_file"] = val
        if current and current.get("alias") and "*" not in current["alias"]:
            hosts.append(current)

    by_alias = {h["alias"]: h for h in hosts}
    for ch in cfg.get("custom_ssh_hosts") or []:
        if isinstance(ch, dict) and ch.get("alias"):
            by_alias[ch["alias"]] = {**ch, "source": ch.get("source", "opsb_config")}

    active_alias = cfg.get("npu_ssh_host", "npu")
    out = list(by_alias.values())
    for h in out:
        h["is_active"] = h["alias"] == active_alias
    out.sort(key=lambda h: (not h["is_active"], "npu" not in h["alias"].lower(), h["alias"]))
    return out


def upsert_ssh_config_block(entry: dict[str, Any]) -> None:
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    ssh_cfg = ssh_dir / "config"
    existing = ssh_cfg.read_text(encoding="utf-8", errors="replace") if ssh_cfg.is_file() else ""

    alias = entry["alias"]
    begin_tag = f"# BEGIN OPSB HOST {alias}"
    end_tag = f"# END OPSB HOST {alias}"
    lines = [
        begin_tag,
        f"Host {alias}",
        f"    HostName {entry['hostname']}",
        f"    Port {int(entry.get('port') or 22)}",
        f"    User {entry.get('user') or 'developer'}",
    ]
    if entry.get("identity_file"):
        lines.append(f"    IdentityFile {entry['identity_file']}")
    lines.append(end_tag)
    block = "\n".join(lines) + "\n"

    pattern = re.compile(re.escape(begin_tag) + r".*?" + re.escape(end_tag) + r"\n?", re.DOTALL)
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
    else:
        sep = "\n" if (existing and not existing.endswith("\n")) else ""
        updated = existing + sep + "\n" + block
    ssh_cfg.write_text(updated, encoding="utf-8")
    os.chmod(ssh_cfg, 0o600)
