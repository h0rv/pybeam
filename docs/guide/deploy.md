# Deploy and connect nodes

Deploy a PyBEAM app like any other Python program: install the locked project and
start the file that calls `pybeam.run`. Each running process is one node. A node
works alone; you only need the settings below to connect several of them.

## Connect nodes with one DNS name

Give every node the same two environment variables:

```sh
PYBEAM_CLUSTER=app.internal
PYBEAM_COOKIE=<long random secret>
```

`PYBEAM_CLUSTER` is a DNS name that resolves to the address of every node. At
startup and then every five seconds, each node looks the name up and connects to
every address it returns. Compose services, Kubernetes headless Services and
Fly.io `.internal` names already work this way; on plain VMs, add one DNS record
or `/etc/hosts` line per VM. IPv6 names work too.

- Nodes connect over TCP port 4370. Open it only on a private network.
- A peer that knows the cookie can run any code on the node. Store
  `PYBEAM_COOKIE` as a secret and generate it with `openssl rand -base64 32`.
- If the name does not resolve within the startup timeout (30 seconds by
  default), `pybeam.run` fails instead of starting an unconnected node.
- A stopped node drops out when its connection closes. A replacement connects on
  the next lookup.

Connecting nodes does not share or move work. Shared state, counters, actors and
background services live on the node that created them, and a stopped node loses
its in-memory state. Keep data that must survive in your database.

## Choose where to run

Run one node per VM or container. Start with one node; add a second when you need
another to keep serving while one restarts.

| Route | Boundary around a node | Restarts after a crash | Fits |
| --- | --- | --- | --- |
| VM with systemd | Own kernel and machine | systemd `Restart=` | A few long-lived servers |
| Docker Compose | Shared host kernel | Docker restart policy | One host, or trying several nodes locally |
| Kubernetes | Shared node kernel, pod limits | Deployment controller | Teams that already run Kubernetes |
| Fly.io | Own microVM per Machine | Fly Machines | Small apps without managing servers |
| NixOS | Own kernel and machine | systemd `Restart=` | VMs managed with Nix |

Inside a node, tasks share one OS process; an [isolated task](execution.md) runs
in its own process, so a native crash there stops only that task. A container or
VM is the next boundary: when a node crashes or runs out of memory, the other
nodes keep running. Containers share the host kernel; VMs and Fly Machines do not.
Each node is a separate process with its own memory, so every extra node adds
that baseline again.

The routes below assume a uv project with `pyproject.toml`, `uv.lock`, and an
`app.py` that calls `pybeam.run` and runs until it is stopped:

```python
# app.py
import asyncio

import pybeam


async def main() -> None:
    print("Serving until stopped", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run app.py
Serving until stopped
```

## VM with systemd

Install uv on each VM, copy the project to `/srv/app`, and create an `app` user
that owns it. Save the shared settings in `/etc/app/cluster.env`, readable only by
root:

```sh
PYBEAM_CLUSTER=app.internal
PYBEAM_COOKIE=<long random secret>
```

Save the service as `/etc/systemd/system/app.service`:

```ini
[Unit]
Description=app
Wants=network-online.target
After=network-online.target

[Service]
User=app
WorkingDirectory=/srv/app
EnvironmentFile=/etc/app/cluster.env
ExecStart=/usr/local/bin/uv run --frozen --no-dev app.py
Restart=on-failure
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

Without a private DNS zone, list every VM under the same name in `/etc/hosts`:

```text
10.0.0.11 app.internal
10.0.0.12 app.internal
```

```console
$ sudo ufw allow from 10.0.0.0/24 to any port 4370 proto tcp
$ sudo systemctl enable --now app
```

## Docker

```dockerfile
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
CMD ["/app/.venv/bin/python", "app.py"]
```

Compose resolves a service name to every replica, so the service name is the
cluster name:

```yaml
# compose.yaml
services:
  app:
    build: .
    restart: on-failure
    deploy:
      replicas: 3
    environment:
      PYBEAM_CLUSTER: app
      PYBEAM_COOKIE: ${PYBEAM_COOKIE:?set PYBEAM_COOKIE}
```

```console
$ PYBEAM_COOKIE="$(openssl rand -base64 32)" docker compose up --build
```

## Kubernetes

Use the Docker image above. A headless Service resolves to every pod;
`publishNotReadyAddresses` lets a starting pod find its peers before it is ready.

```console
$ kubectl create secret generic app-cookie --from-literal=cookie="$(openssl rand -base64 32)"
$ kubectl apply -f app.yaml
```

```yaml
# app.yaml
apiVersion: v1
kind: Service
metadata:
  name: app-nodes
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: app
  ports:
    - name: nodes
      port: 4370
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      terminationGracePeriodSeconds: 15
      containers:
        - name: app
          image: registry.example.com/app:1
          env:
            - name: PYBEAM_CLUSTER
              value: app-nodes
            - name: PYBEAM_COOKIE
              valueFrom:
                secretKeyRef:
                  name: app-cookie
                  key: cookie
```

Add a NetworkPolicy that admits port 4370 only from pods labelled `app: app`.

## Fly.io

Use the Docker image above. Fly.io resolves `<app>.internal` to every started
Machine over its private IPv6 network.

```toml
# fly.toml
app = "my-app"

[env]
  PYBEAM_CLUSTER = "my-app.internal"
```

```console
$ fly secrets set PYBEAM_COOKIE="$(openssl rand -base64 32)"
$ fly deploy
$ fly scale count 2
```

## NixOS

Import this module, copy the project to `/srv/app`, and put
`PYBEAM_COOKIE=<long random secret>` in `/run/keys/app-cookie.env` with your
secrets tool. `nix-ld` lets the runtime bundled in the `python-beam` wheel run on
NixOS. Replace `ens4` with your private interface.

```nix
# app.nix
{ pkgs, ... }:
{
  programs.nix-ld.enable = true;
  networking.firewall.interfaces.ens4.allowedTCPPorts = [ 4370 ];

  users.users.app = {
    isSystemUser = true;
    group = "app";
    home = "/srv/app";
    createHome = true;
  };
  users.groups.app = { };

  systemd.services.app = {
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    environment = {
      PYBEAM_CLUSTER = "app.internal";
      UV_PYTHON = "${pkgs.python314}/bin/python3.14";
      UV_PYTHON_DOWNLOADS = "never";
    };
    serviceConfig = {
      User = "app";
      WorkingDirectory = "/srv/app";
      EnvironmentFile = "/run/keys/app-cookie.env";
      ExecStart = "${pkgs.uv}/bin/uv run --frozen --no-dev app.py";
      Restart = "on-failure";
      TimeoutStopSec = 15;
    };
  };
}
```
