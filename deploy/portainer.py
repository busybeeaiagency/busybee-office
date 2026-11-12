#!/usr/bin/env python3
"""Deploy the BUSY BEE office container on the homelab docker host through Portainer.

    python3 deploy/portainer.py status          # is it running, which ports
    python3 deploy/portainer.py create          # create + start (fails if it exists)
    python3 deploy/portainer.py recreate        # stop, delete, create, start (keeps the spec)
    python3 deploy/portainer.py stop|start|restart
    python3 deploy/portainer.py logs [tail]     # last N lines of container logs

The container runs python:3.14-slim (already on the host) with:
  /app   <- /opt/data/coder-workspaces/busybee-office   (read-only; this repo)
  /hermes<- busybee-hermes-data volume                 (read-only; kanban.db + creds)
  host port 9120 -> 9120, restart policy unless-stopped (survives reboots).

Secrets stay in /hermes/creds on the read-only mount; the app serves no credential.
"""
import json
import ssl
import sys
import time
import urllib.request

CRED = json.load(open("/opt/data/creds/portainer.json"))
BASE = f"https://{CRED['host']}:{CRED['port']}"
NAME = "busybee-office"
APP_HOST = "/var/lib/docker/volumes/busybee-hermes-data/_data/coder-workspaces/busybee-office"
HERMES_HOST = "/var/lib/docker/volumes/busybee-hermes-data/_data"
IMAGE = "python:3.14-slim"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def call(method, path, body=None, token=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:400]
    except Exception as e:
        return 0, repr(e)[:200]


def token():
    st, out = call("POST", "/api/auth", {"Username": CRED["username"], "Password": CRED["password"]})
    if st != 200:
        sys.exit(f"portainer auth failed: {st} {out}")
    return out["jwt"]


def find(tok):
    st, cts = call("GET", "/api/endpoints/3/docker/containers/json?all=true", token=tok)
    for ct in cts or []:
        names = [n.lstrip("/") for n in (ct.get("Names") or [])]
        if NAME in names:
            return ct["Id"], ct
    return None, None


def spec(tok):
    """The container definition. Built from the live one when it exists (so nothing drifts)."""
    cid, ct = find(tok)
    if cid:
        st, ins = call("GET", f"/api/endpoints/3/docker/containers/{cid}/json", token=tok)
        cfg = ins["Config"]
        host = ins["HostConfig"]
        old_env = [e for e in cfg.get("Env", []) if not e.startswith(("OFFICE_", "PYTHONUNBUFFERED="))]
        old_mounts = [m for m in ins.get("Mounts", []) if m.get("Destination") not in ("/app", "/hermes")]
        return {
            "Image": cfg["Image"],
            "Env": old_env + [
                "PYTHONUNBUFFERED=1", "OFFICE_PORT=9120", "OFFICE_DB=/hermes/kanban.db",
                "OFFICE_HERMES_DIR=/hermes", "OFFICE_BUILD=busybee-office-1",
            ],
            "Cmd": ["python3", "-m", "server.office"],
            "WorkingDir": "/app",
            "ExposedPorts": {"9120/tcp": {}},
            "HostConfig": {
                "PortBindings": {"9120/tcp": [{"HostPort": "9120"}]},
                "Binds": [
                    f"{HERMES_HOST}:/hermes:ro",
                    f"{APP_HOST}:/app:ro",
                ] + [f"{m['Source']}:{m['Destination']}:{m['Mode']}" for m in old_mounts],
                "NetworkMode": "agency-net",
                "RestartPolicy": {"Name": "unless-stopped"},
                "LogConfig": host.get("LogConfig") or {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            },
            "Healthcheck": {"Test": ["CMD", "python3", "-c",
                                      "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9120/healthz',timeout=3)"],
                            "Interval": 60000000000, "Timeout": 5000000000, "Retries": 3},
        }
    return {
        "Image": IMAGE,
        "Env": ["PYTHONUNBUFFERED=1", "OFFICE_PORT=9120", "OFFICE_DB=/hermes/kanban.db",
                "OFFICE_HERMES_DIR=/hermes", "OFFICE_BUILD=busybee-office-1"],
        "Cmd": ["python3", "-m", "server.office"],
        "WorkingDir": "/app",
        "ExposedPorts": {"9120/tcp": {}},
        "HostConfig": {
            "PortBindings": {"9120/tcp": [{"HostPort": "9120"}]},
            "Binds": [f"{HERMES_HOST}:/hermes:ro", f"{APP_HOST}:/app:ro"],
            "NetworkMode": "agency-net",
            "RestartPolicy": {"Name": "unless-stopped"},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
        },
        "Healthcheck": {"Test": ["CMD", "python3", "-c",
                                  "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9120/healthz',timeout=3)"],
                        "Interval": 60000000000, "Timeout": 5000000000, "Retries": 3},
    }


def wait_running(cid, tok, seconds=40):
    for _ in range(seconds):
        st, ins = call("GET", f"/api/endpoints/3/docker/containers/{cid}/json", token=tok)
        state = (ins or {}).get("State") or {}
        if state.get("Running"):
            return True, state
        if state.get("ExitCode") not in (0, None):
            return False, state
        time.sleep(1)
    return False, {}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    tok = token()
    cid, ct = find(tok)
    if cmd == "status":
        if not cid:
            print("not deployed")
            return
        st, ins = call("GET", f"/api/endpoints/3/docker/containers/{cid}/json", token=tok)
        ins = ins if isinstance(ins, dict) else {}
        state = ins["State"]
        # Portainer's inspect proxy reports HostPort here; docker's own inspect uses PublicPort.
        ports = sorted({p.get("PublicPort") or p.get("HostPort") for p in
                        (ins.get("NetworkSettings", {}).get("Ports", {}) or {}).get("9120/tcp", [])})
        print(f"{NAME}: {state.get('Status')} health={state.get('Health', {}).get('Status')} "
              f"ports={ports} restarts={state.get('RestartCount')} "
              f"restart_policy={ins['HostConfig'].get('RestartPolicy', {}).get('Name')}")
        return
    if cmd == "logs":
        tail = sys.argv[2] if len(sys.argv) > 2 else "40"
        if not cid:
            sys.exit("not deployed")
        raw = urllib.request.urlopen(
            urllib.request.Request(f"{BASE}/api/endpoints/3/docker/containers/{cid}/logs?stdout=1&stderr=1&tail={tail}",
                                   headers={"Authorization": "Bearer " + tok}), timeout=30, context=CTX).read()
        # docker multiplex stream: 8-byte header (type + pad + 4-byte length) per frame
        out, i = bytearray(), 0
        while i + 8 <= len(raw):
            n = int.from_bytes(raw[i + 4:i + 8], "big")
            out += raw[i + 8:i + 8 + n]
            i += 8 + n
        sys.stdout.write(out.decode("utf-8", "replace")[-6000:])
        return
    if cmd in ("stop", "start", "restart"):
        if not cid:
            sys.exit("not deployed")
        st, out = call("POST", f"/api/endpoints/3/docker/containers/{cid}/{cmd}?detach=true", token=tok)
        print(cmd, st, out if st >= 400 else "")
        return
    if cmd in ("create", "recreate"):
        if cmd == "recreate" and cid:
            call("POST", f"/api/endpoints/3/docker/containers/{cid}/stop?t=5", token=tok)
            call("DELETE", f"/api/endpoints/3/docker/containers/{cid}?force=1&v=1", token=tok)
            time.sleep(2)
        body = spec(tok)
        st, out = call("POST", f"/api/endpoints/3/docker/containers/create?name={NAME}", body, token=tok)
        if st not in (200, 201):
            sys.exit(f"create failed: {st} {out}")
        cid = out["Id"]
        st, out = call("POST", f"/api/endpoints/3/docker/containers/{cid}/start?detach=true", token=tok)
        if st not in (200, 204, 304):
            sys.exit(f"start failed: {st} {out}")
        ok, state = wait_running(cid, tok)
        health = "n/a"
        try:
            with urllib.request.urlopen("http://127.0.0.1:9120/healthz", timeout=8, context=CTX) as r:
                health = f"{r.status} {r.read().decode()[:80]}"
        except Exception as e:
            health = type(e).__name__
        print(f"created {cid[:12]} running={ok} healthz={health} status={state.get('Status')}")
        return
    sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
