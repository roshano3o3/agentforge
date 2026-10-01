#!/usr/bin/env bash
# The CI release gate (.github/workflows/agentforge-gate.yml), as one script.
#
# CI's database starts empty, so in one job this:
#   1. builds two worker images from the PR checkout: the candidate (the PR's
#      examples/invoice_agent) and the baseline (the base branch's copy of
#      that directory, everything else identical);
#   2. migrates the DB, starts the API, publishes the datasets;
#   3. evaluates `invoice_agent.adapter:answer` with the baseline worker and
#      then the candidate worker, on two datasets:
#        - the trajectory dataset (invoice_agent_v1.yaml), gated by `release_policy`
#          (the baseline run is set as the `production` baseline);
#        - the adversarial dataset (invoice_agent_safety_v1.yaml), gated by
#          `safety_policy` against the baseline's safety run;
#   4. runs `agentforge gate` once per dataset and writes one combined
#      Markdown report for the PR comment. The gate fails if either fails.
#
# The agent's behavior comes from the code in each checkout
# (examples/invoice_agent/invoice_agent/config.py), never from a flag here.
# The datasets and the policies come from the BASE branch, so a pull request
# can't loosen the gate that judges it. If the base branch has no safety
# dataset or safety_policy yet, only the trajectory gate runs, and the
# comment says so.
#
# Env: BASE_DIR (base checkout), BASE_SHA, HEAD_SHA, PR_NUMBER,
#      AGENTFORGE_DATABASE_URL, AGENTFORGE_REDIS_URL, GITHUB_OUTPUT (optional).
# Writes .gate/comment.md and the combined exit code (0 PASSED, 1 FAILED,
# 2 couldn't evaluate) to $GITHUB_OUTPUT as `exit_code`.
set -euo pipefail

API="http://127.0.0.1:8000"
export COLUMNS=200 PYTHONIOENCODING=utf-8
AF=(python -m agentforge_cli.main)
mkdir -p .gate
COMMENT=.gate/comment.md
SAFETY_DATASET=datasets/invoice_agent_safety_v1.yaml

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
SAFETY=1
if [ ! -f "$BASE_DIR/$SAFETY_DATASET" ] || ! grep -q '^safety_policy:' "$BASE_DIR/agentforge.yaml"; then
    SAFETY=0
fi

echo "::group::Build worker images (baseline agent from ${BASE_SHA:0:7}, candidate from ${HEAD_SHA:0:7})"
rm -rf .gate/agent-base
cp -r "$BASE_DIR/examples/invoice_agent" .gate/agent-base
docker build -q -f apps/worker/Dockerfile -t agentforge-worker:candidate .
docker build -q -f apps/worker/Dockerfile --build-arg INVOICE_AGENT_DIR=.gate/agent-base -t agentforge-worker:baseline .
echo "::endgroup::"

echo "::group::Migrate, start the API, publish the datasets"
(cd apps/api && python -m alembic upgrade head)
(cd apps/api && nohup python -m uvicorn agentforge_api.main:app --host 127.0.0.1 --port 8000 > ../../.gate/api.log 2>&1 &)
for _ in $(seq 1 60); do curl -sf "$API/health" > /dev/null && break; sleep 1; done
curl -sf "$API/health" > /dev/null || finish 2 "The AgentForge API did not start (see the job log)."
"${AF[@]}" dataset publish "$BASE_DIR/datasets/invoice_agent_v1.yaml" --api-url "$API"
if [ "$SAFETY" -eq 1 ]; then
    "${AF[@]}" dataset publish "$BASE_DIR/$SAFETY_DATASET" --api-url "$API"
fi
echo "::endgroup::"

evaluate() {  # evaluate <baseline|candidate> <app version label> <dataset name> -> prints the run id
    local which=$1 label=$2 dataset=$3 out
    docker run -d --name "agentforge-worker-$which" --network host \
        -e AGENTFORGE_DATABASE_URL -e AGENTFORGE_REDIS_URL "agentforge-worker:$which" > /dev/null
    if ! out=$("${AF[@]}" evaluate --app invoice-agent --app-version "$label" --dataset "$dataset" \
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
BASE_RUN=$(evaluate baseline "base@${BASE_SHA:0:7}" invoice-agent) || finish 2 "Evaluating the base branch's agent failed."
"${AF[@]}" baseline set "$BASE_RUN" --env production --api-url "$API"
if [ "$SAFETY" -eq 1 ]; then
    BASE_SAFETY_RUN=$(evaluate baseline "base@${BASE_SHA:0:7}" invoice-agent-safety) \
        || finish 2 "Evaluating the base branch's agent on the safety dataset failed."
fi
echo "::endgroup::"

echo "::group::Evaluate the PR's agent (${HEAD_SHA:0:7})"
CANDIDATE_RUN=$(evaluate candidate "pr-${PR_NUMBER}@${HEAD_SHA:0:7}" invoice-agent) \
    || finish 2 "Evaluating the PR's agent failed."
if [ "$SAFETY" -eq 1 ]; then
    CANDIDATE_SAFETY_RUN=$(evaluate candidate "pr-${PR_NUMBER}@${HEAD_SHA:0:7}" invoice-agent-safety) \
        || finish 2 "Evaluating the PR's agent on the safety dataset failed."
fi
echo "::endgroup::"

set +e
"${AF[@]}" gate --candidate "$CANDIDATE_RUN" --baseline production --config "$BASE_DIR/agentforge.yaml" \
    --api-url "$API" --markdown .gate/trajectory.md --title "Trajectory dataset (invoice-agent, release_policy)"
code=$?
safety_code=0
if [ "$SAFETY" -eq 1 ]; then
    "${AF[@]}" gate --candidate "$CANDIDATE_SAFETY_RUN" --baseline "$BASE_SAFETY_RUN" --policy safety_policy \
        --config "$BASE_DIR/agentforge.yaml" --api-url "$API" --markdown .gate/safety.md \
        --title "Safety dataset (invoice-agent-safety, safety_policy)"
    safety_code=$?
fi
set -e
if [ "$code" -eq 2 ] || [ "$safety_code" -eq 2 ]; then
    finish 2 "\`agentforge gate\` could not evaluate a policy (see the job log)."
fi
overall=$(( code > safety_code ? code : safety_code ))
verdict=$([ "$overall" -eq 0 ] && echo PASSED || echo FAILED)
{
    echo "## AgentForge release gate: $verdict"
    echo
    cat .gate/trajectory.md
    if [ "$SAFETY" -eq 1 ]; then
        cat .gate/safety.md
        echo "Safety thresholds: with 5 cases per attack category, one failing case is a 20-point drop (10 points" \
            "for the 10 pooled injection cases), so the regression limits mean no regressions."
    else
        echo "_Safety gate skipped: the base branch has no \`$SAFETY_DATASET\` or no \`safety_policy\`._"
    fi
    echo
    echo "All checks are arithmetic over persisted run aggregates; none is an LLM judgment."
    echo
    echo "**RELEASE GATE: $verdict**"
} > "$COMMENT"
finish "$overall"
