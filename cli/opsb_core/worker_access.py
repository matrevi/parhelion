"""Bind a managed worker to its own loopback MCP route and keep SSH in the daemon."""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from . import worker_sandbox
from .drivers.base import resolve_workspace_state_dir


def relay_file(workspace, agent_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent_id):
        raise ValueError("Invalid worker identity")
    return resolve_workspace_state_dir(workspace).parent / "agents" / agent_id / "relay.json"


def worker_token(secret, workspace, agent_id):
    identity = json.dumps(["mcp/worker", str(workspace.resolve()), agent_id]).encode()
    return hmac.digest(secret.encode(), identity, "sha256").hex()


def prepare_worker_command(cmd, env, workspace, agent_id):
    """Bind one worker to its own route and wrap its process in the worker sandbox profile.

    The relay carries the *scoped* token, which only authenticates ``/v1/mcp/worker/<id>`` for
    this workspace, so a worker cannot reach another seat's route or the orchestrator route.
    The profile adds the judge credential stores to the SSH and daemon-token denies
    (``worker_sandbox.read_deny_paths``) for the whole process tree, the MCP server included.
    """
    state = json.loads(config.DAEMON_STATE_PATH.read_text())
    path = relay_file(workspace, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as file:
        json.dump({"port": state["port"], "token": worker_token(state["token"], workspace, agent_id)}, file)
    if sys.platform != "darwin":
        raise RuntimeError("Managed worker SSH isolation requires macOS")
    extra = [("subpath", str(Path.home() / ".ssh")),
             ("literal", str(config.DAEMON_STATE_PATH.resolve()))]
    profile = worker_sandbox.seatbelt_profile(extra=extra)
    profile += ['(deny network-outbound (remote tcp "*:22"))', '(deny process-exec (regex #"/(ssh|scp|sftp)$"))']
    env.pop("SSH_AUTH_SOCK", None)
    return [worker_sandbox.SANDBOX_EXEC, "-p", "\n".join(profile), *cmd]


def forward_worker_request(workspace, agent_id, payload):
    state = json.loads(relay_file(workspace, agent_id).read_text())
    query = urllib.parse.urlencode({"workspace": str(workspace)})
    url = f"http://127.0.0.1:{int(state['port'])}/v1/mcp/worker/{agent_id}?{query}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "X-Opsb-Token": state["token"]})
    with urllib.request.urlopen(req, timeout=None) as response:
        body = response.read()
    return json.loads(body) if body else None
