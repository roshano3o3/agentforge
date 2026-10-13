#!/usr/bin/env bash
# Deploy AgentForge to a throwaway kind cluster and check the demo works: the CI workflow
# (.github/workflows/k8s.yml) runs exactly this, and so can you (Docker + kind + kubectl on PATH).
#
#   scripts/k8s-kind.sh            # build images, create the cluster, apply deploy/k8s, wait, check
#   KEEP_CLUSTER=1 scripts/k8s-kind.sh   # leave the cluster up afterwards (kind delete cluster --name agentforge)
#
# Steps: build the four images from this checkout (tag :local, the tag deploy/k8s/kustomization.yaml uses), load
# them into kind (nothing is pulled from a registry), `kubectl apply -k deploy/k8s`, wait for every Deployment
# and for the seed Job to complete, then port-forward and check the API's /health, that /runs holds the seeded
# runs (all completed), and that the dashboard serves /runs.
set -euo pipefail

CLUSTER=${CLUSTER:-agentforge}
NS=agentforge
KIND=${KIND:-kind}
API_PORT=${API_PORT:-18000}
WEB_PORT=${WEB_PORT:-13000}
EXPECTED_RUNS=7  # agentforge demo seed: rag v1, invoice v1 x2, v2 x2, PR #4 agent x2

cd "$(dirname "$0")/.."

echo "::group::Build images"
docker build -q -f apps/api/Dockerfile -t agentforge/api:local .
docker build -q -f apps/worker/Dockerfile -t agentforge/worker:local --build-arg AGENTFORGE_GIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)" .
docker build -q -f cli/Dockerfile -t agentforge/seed:local .
docker build -q -t agentforge/web:local apps/web
echo "::endgroup::"

echo "::group::Create the kind cluster and load the images"
if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    "$KIND" create cluster --name "$CLUSTER" --wait 120s
fi
for image in api worker seed web; do
    "$KIND" load docker-image --name "$CLUSTER" "agentforge/$image:local"
done
echo "::endgroup::"

cleanup() {
    jobs -p | xargs -r kill 2>/dev/null || true
    if [ "${KEEP_CLUSTER:-0}" != "1" ]; then "$KIND" delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

dump() {
    echo "::group::Diagnostics"
    kubectl -n "$NS" get all,pvc -o wide || true
    kubectl -n "$NS" describe pods | tail -n 200 || true
    for target in deployment/api deployment/worker deployment/web job/seed; do
        echo "--- logs $target"; kubectl -n "$NS" logs "$target" --all-containers --tail=80 || true
    done
    echo "::endgroup::"
}

echo "::group::Apply deploy/k8s"
kubectl apply -k deploy/k8s
echo "::endgroup::"

echo "::group::Wait for the stack and the seed Job"
if ! kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s \
    || ! kubectl -n "$NS" rollout status deployment/redis --timeout=300s \
    || ! kubectl -n "$NS" rollout status deployment/api --timeout=300s \
    || ! kubectl -n "$NS" rollout status deployment/worker --timeout=300s \
    || ! kubectl -n "$NS" rollout status deployment/web --timeout=300s \
    || ! kubectl -n "$NS" wait --for=condition=complete job/seed --timeout=600s; then
    dump
    exit 1
fi
kubectl -n "$NS" logs job/seed
kubectl -n "$NS" get pods -o wide
echo "::endgroup::"

echo "::group::Check /health, /runs and the dashboard"
kubectl -n "$NS" port-forward svc/api "$API_PORT:8000" >/dev/null 2>&1 &
kubectl -n "$NS" port-forward svc/web "$WEB_PORT:3000" >/dev/null 2>&1 &
for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$WEB_PORT/runs" >/dev/null 2>&1 && break
    sleep 1
done
health=$(curl -fsS "http://127.0.0.1:$API_PORT/health")
echo "GET /health -> $health"
[ "$health" = '{"status":"ok"}' ] || { dump; exit 1; }
runs=$(curl -fsS "http://127.0.0.1:$API_PORT/runs")
summary=$(RUNS="$runs" python3 - <<'PY'
import json, os
runs = json.loads(os.environ["RUNS"])
for r in runs:
    agg = r["aggregates"] or {}
    print(f"  {r['application_name']}@{r['application_version']} on {r['dataset_name']}: {r['status']}, pass_rate={agg.get('pass_rate')}")
print(len(runs), sum(r["status"] == "completed" for r in runs))
PY
)
printf 'GET /runs ->\n%s\n' "$(printf '%s\n' "$summary" | sed '$d')"
read -r total completed <<<"$(printf '%s\n' "$summary" | tail -n 1)"
[ "$total" = "$EXPECTED_RUNS" ] && [ "$completed" = "$EXPECTED_RUNS" ] || { echo "expected $EXPECTED_RUNS completed runs"; dump; exit 1; }
web=$(curl -fsS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$WEB_PORT/runs")
echo "GET dashboard /runs -> $web"
[ "$web" = "200" ] || { dump; exit 1; }
echo "::endgroup::"
echo "kind deployment OK: $completed/$EXPECTED_RUNS seeded runs completed, /health ok, dashboard 200"
