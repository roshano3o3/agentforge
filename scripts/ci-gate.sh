#!/usr/bin/env bash
# The CI release gate (.github/workflows/agentforge-gate.yml), as one script.
#
# CI's database starts empty, so in one job this:
#   1. builds two worker images from the PR checkout: the candidate (the PR's
#      examples/invoice_agent) and the baseline (the base branch's copy of
#      that directory, everything else identical);
#   2. migrates the DB, starts the API, publishes the dataset;
#   3. evaluates `invoice_agent.adapter:answer` with the baseline worker, sets
#      that run as the production baseline, then evaluates it again with the
#      candidate worker;
#   4. runs `agentforge gate` and writes the Markdown report for the PR comment.
#
# The agent's behavior comes from the code in each checkout
# (examples/invoice_agent/invoice_agent/config.py), never from a flag here.
# The dataset and the release policy come from the BASE branch, so a pull
# request can't loosen the gate that judges it.
#
# Env: BASE_DIR (base checkout), BASE_SHA, HEAD_SHA, PR_NUMBER,
#      AGENTFORGE_DATABASE_URL, AGENTFORGE_REDIS_URL, GITHUB_OUTPUT (optional).
# Writes .gate/comment.md and the gate's exit code (0 PASSED, 1 FAILED,
# 2 couldn't evaluate) to $GITHUB_OUTPUT as `exit_code`.
set -euo pipefail

API="http://127.0.0.1:8000"
export COLUMNS=200 PYTHONIOENCODING=utf-8
AF=(python -m agentforge_cli.main)
mkdir -p .gate
COMMENT=.gate/comment.md

finish() {  # finish <exit code> [message]
    echo "exit_code=$1" >> "${GITHUB_OUTPUT:-/dev/null}"
    if [ -n "${2:-}" ]; then
        printf '## AgentForge release gate: could not be evaluated\n\n%s\n' "$2" > "$COMMENT"
        echo "::error::$2"
    fi
    exit 0  # the workflow's last step fails the check from exit_code
}

for f in agentforge.yaml datasets/invoice_agent_v1.yaml examples/invoice_agent/invoice_agent/config.py; do
    [ -f "$BASE_DIR/$f" ] || finish 2 "The base branch has no \`$f\`, so there is no baseline or policy to gate against."
done

echo "::group::Build worker images (baseline agent from ${BASE_SHA:0:7}, candidate from ${HEAD_SHA:0:7})"
rm -rf .gate/agent-base
cp -r "$BASE_DIR/examples/invoice_agent" .gate/agent-base
docker build -q -f apps/worker/Dockerfile -t agentforge-worker:candidate .
docker build -q -f apps/worker/Dockerfile --build-arg INVOICE_AGENT_DIR=.gate/agent-base -t agentforge-worker:baseline .
echo "::endgroup::"

echo "::group::Migrate, start the API, publish the dataset"
(cd apps/api && python -m alembic upgrade head)
(cd apps/api && nohup python -m uvicorn agentforge_api.main:app --host 127.0.0.1 --port 8000 > ../../.gate/api.log 2>&1 &)
for _ in $(seq 1 60); do curl -sf "$API/health" > /dev/null && break; sleep 1; done
curl -sf "$API/health" > /dev/null || finish 2 "The AgentForge API did not start (see the job log)."
"${AF[@]}" dataset publish "$BASE_DIR/datasets/invoice_agent_v1.yaml" --api-url "$API"
echo "::endgroup::"

evaluate() {  # evaluate <baseline|candidate> <app version label> -> prints the run id
    local which=$1 label=$2 out
    docker run -d --name "agentforge-worker-$which" --network host \
        -e AGENTFORGE_DATABASE_URL -e AGENTFORGE_REDIS_URL "agentforge-worker:$which" > /dev/null
    if ! out=$("${AF[@]}" evaluate --app invoice-agent --app-version "$label" --dataset invoice-agent \
        --adapter invoice_agent.adapter:answer --api-url "$API" --poll-interval 0.5 --wait-timeout 300); then
        echo "$out" >&2
        docker logs --tail 50 "agentforge-worker-$which" >&2 || true
        docker rm -f "agentforge-worker-$which" > /dev/null
        return 1
    fi
    echo "$out" >&2
    docker rm -f "agentforge-worker-$which" > /dev/null
    grep -oE 'Submitted run [0-9a-f-]{36}' <<< "$out" | awk '{print $3}'
}

echo "::group::Evaluate the base branch's agent (${BASE_SHA:0:7})"
BASE_RUN=$(evaluate baseline "base@${BASE_SHA:0:7}") || finish 2 "Evaluating the base branch's agent failed."
"${AF[@]}" baseline set "$BASE_RUN" --env production --api-url "$API"
echo "::endgroup::"

echo "::group::Evaluate the PR's agent (${HEAD_SHA:0:7})"
CANDIDATE_RUN=$(evaluate candidate "pr-${PR_NUMBER}@${HEAD_SHA:0:7}") || finish 2 "Evaluating the PR's agent failed."
echo "::endgroup::"

set +e
"${AF[@]}" gate --candidate "$CANDIDATE_RUN" --baseline production --config "$BASE_DIR/agentforge.yaml" \
    --api-url "$API" --markdown "$COMMENT"
code=$?
set -e
if [ "$code" -eq 2 ]; then
    finish 2 "\`agentforge gate\` could not evaluate the policy (see the job log)."
fi
finish "$code"
