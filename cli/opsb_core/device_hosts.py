"""Which device host runs a problem's tests (4.3.2).

One host was the whole device pool until the 4.3 sprint showed its single die 100% occupied with
14 workers. ``npu_ssh_hosts`` in ``~/.parhelion/config.json`` lists every device host (ssh aliases);
empty or absent means ``npu_ssh_host`` alone, the earlier behaviour.

A problem is pinned to one host instead of being spread run by run: the gate compares a candidate
with the board SHA case by case, and two devices are two measurement instruments. Pinning keeps a
problem's runs on one device; ``scheduler.estimator`` additionally pairs runs only within a host.
The pin is the problem's position in the workspace roster modulo the number of hosts, so it is
stable across processes and restarts; ``npu_host_by_problem`` overrides it per problem.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_HOST = "npu"


def device_hosts(cfg: dict[str, Any]) -> list[str]:
    """Every configured device host, the primary ``npu_ssh_host`` first, without duplicates."""
    primary = str(cfg.get("npu_ssh_host") or DEFAULT_HOST)
    hosts = [primary]
    for host in cfg.get("npu_ssh_hosts") or []:
        alias = str(host or "").strip()
        if alias and alias not in hosts:
            hosts.append(alias)
    return hosts


def _roster(workspace: Path) -> list[str]:
    try:
        from .services.contest_service import resolve_workspace_meta

        return [str(p.get("title") or "") for p in resolve_workspace_meta(workspace).get("problems") or []]
    except Exception:  # an unreadable roster falls back to the problem name's own order
        return []


def host_for(cfg: dict[str, Any], workspace: Path | str, problem: str) -> str:
    """The device host that runs ``problem``'s tests."""
    hosts = device_hosts(cfg)
    if len(hosts) == 1:
        return hosts[0]
    pinned = (cfg.get("npu_host_by_problem") or {}).get(problem)
    if pinned in hosts:
        return str(pinned)
    roster = [t.lower() for t in _roster(Path(workspace))]
    name = str(problem or "").lower()
    if name in roster:
        return hosts[roster.index(name) % len(hosts)]
    return hosts[sum(name.encode()) % len(hosts)]


def assignment(cfg: dict[str, Any], workspace: Path | str) -> dict[str, str]:
    """``{problem: host}`` for the whole roster (status and logging)."""
    return {title: host_for(cfg, workspace, title) for title in _roster(Path(workspace)) if title}
