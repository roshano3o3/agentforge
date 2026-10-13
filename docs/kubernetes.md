# Kubernetes

`deploy/k8s/` is a kustomize base that runs the same stack as `docker compose up`:

- **Services:** Postgres, Redis, the API, the worker and the dashboard.
- **Seed:** the one-shot `agentforge demo seed`, as a Job.

It is verified on every push by [`.github/workflows/k8s.yml`](../.github/workflows/k8s.yml), which runs [`scripts/k8s-kind.sh`](../scripts/k8s-kind.sh) against a throwaway [kind](https://kind.sigs.k8s.io/) cluster. The latest result is in [results.md](results.md#kubernetes-kind).

## Why the worker is its own Deployment

The two halves of AgentForge have very different load.

- **The API** is light and steady. It validates requests, stores rows and queues run ids. Its cost doesn't depend on how big or slow an evaluation is.
- **The worker** does all the heavy work: it runs the agent once per test case (seconds per case for an LLM agent), runs every evaluator and writes every step and span. Its load grows with:
  - the number of runs queued;
  - their size (a 226-case stress set vs a 10-case dataset);
  - the agent's latency.

The architecture already separates them: the API never executes adapters, and the worker only talks to Redis and Postgres. Each run is a job on one Redis queue (arq), and any worker can take any job. So in Kubernetes the worker is a separate Deployment that scales on its own:

```bash
kubectl -n agentforge scale deployment/worker --replicas=6
```

More pods means more runs in parallel:

- **Per pod:** up to `AGENTFORGE_WORKER_MAX_JOBS` runs (default 4).
- **Within a run:** cases still run sequentially.
- **The API:** doesn't change.

The manifests start with 2 worker replicas and 1 API replica. A queue-length autoscaler would be the next step; none is included, because the CI cluster has no metrics pipeline to test it against.

## What's in `deploy/k8s/`

| File | What | Notes |
|---|---|---|
| `kustomization.yaml` | namespace `agentforge`, the resources, image tags | Images are built from this repo (nothing is published to a registry); `images:` maps `agentforge/{api,worker,web,seed}` to the tag you built (`:local`). |
| `config.yaml` | ConfigMap `agentforge-config` | Non-secret settings, the same variables as `docker-compose.yml` (Redis URL, tracing and redaction switches, OTLP endpoint, job timeout). |
| `secret.yaml` | Secret `agentforge-secrets` | Postgres user/password and the model-provider keys (empty). **The committed values are the local-dev defaults from `.env.example`**, so a throwaway cluster comes up with no extra step. Replace this Secret for anything else, and never commit real keys. |
| `postgres.yaml` | StatefulSet + PVC (2 Gi) + Service | One replica. Results are immutable (DB triggers) and must outlive the pod. Probes: `pg_isready`. |
| `redis.yaml` | Deployment + Service | The run queue. Ephemeral on purpose: results are in Postgres, never in Redis. Probes: `redis-cli ping`. |
| `api.yaml` | Deployment (1 replica, `Recreate`) + Service | An init container waits for Postgres, then the API applies migrations and serves. Probes: `GET /health`. |
| `worker.yaml` | Deployment (2 replicas) | No Service: it consumes the queue. Probes: [`python -m agentforge_worker.healthcheck`](../apps/worker/agentforge_worker/healthcheck.py) (Redis `PING` + Postgres `SELECT 1` from *this* pod; arq's own `--check` reads a queue-wide key and can't tell which pod is unhealthy). 60 s termination grace. |
| `web.yaml` | Deployment + Service | The dashboard's production build. Probes: `GET /runs`. |
| `seed-job.yaml` | Job | `agentforge demo seed`: it waits for the API, loads the demo through it, and the workers execute the runs. It loads only into an empty database, so re-running it does nothing. |

Every container has CPU/memory requests and limits. They are sized for the fixture agents: a worker running an LLM agent mostly waits on the network, while one running a heavy in-process agent may need more.

## Run it

On kind, the same as CI:

```bash
scripts/k8s-kind.sh                 # build images, create cluster "agentforge", apply, wait, check, delete the cluster
KEEP_CLUSTER=1 scripts/k8s-kind.sh  # ...and keep it running
```

On any cluster, once the four images are available to it:

```bash
kubectl apply -k deploy/k8s
kubectl -n agentforge wait --for=condition=complete job/seed --timeout=600s
kubectl -n agentforge port-forward svc/api 8000:8000 &
kubectl -n agentforge port-forward svc/web 3000:3000 &
# open http://127.0.0.1:3000/runs
```

The dashboard's pages call the API **from the browser**, at the address baked into the web image at build time (`NEXT_PUBLIC_API_URL`, default `http://127.0.0.1:8000`). So both ports are forwarded to localhost. To serve it differently, rebuild the web image with another `NEXT_PUBLIC_API_URL` and add the dashboard's origin to the API's `AGENTFORGE_CORS_ORIGINS`.

## Limits

- **No Ingress, on purpose.** AgentForge has no authentication, so the manifests expose nothing outside the cluster; access is `kubectl port-forward`. Putting an Ingress in front needs real auth first (see [limitations.md](limitations.md)).
- **One API replica.** The API runs migrations at startup, and two pods starting together would race on them. Scaling the API out needs migrations moved to a Job (or an init step with a lock) first.
- **Single-node Postgres and Redis.** They're fine for a demo or a team's evaluation cluster, but they aren't HA. For production use a managed Postgres, and point `POSTGRES_HOST` and the Secret at it.
- **A worker pod stopped mid-run fails that run** (arq cancels it; it is marked failed, not resumed). Scale down when the queue is idle, or accept re-running.
- **Tested on kind only** (in CI and on one Windows machine). Storage classes, image registries and network policies on a real cluster aren't covered by these manifests.
- **The images run as root** (Python and Node base images). Adding `runAsNonRoot` needs a user in each Dockerfile, which hasn't been done yet.
