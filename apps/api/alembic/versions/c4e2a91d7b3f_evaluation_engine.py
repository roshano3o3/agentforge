"""evaluation engine: worker-executed runs, metric scores, run immutability

Revision ID: c4e2a91d7b3f
Revises: b17cf04aecaf
Create Date: 2026-09-30 18:00:00

* runstatus gains 'pending', resultstatus gains 'timeout'.
* test_cases gain expected_answer_contains / expected_answer_regex (added
  columns only -- ADD COLUMN fires no row triggers, so published versions
  stay frozen).
* evaluation_runs: evaluator_name/evaluator_version -> `evaluators` (a list
  of pinned name@version), plus adapter config, timeout, latency budget,
  error message, stored aggregates, started_at.
* evaluation_results: per-evaluator score/evidence move to a new
  metric_scores table (one row per evaluator per case); results gain
  citations, token usage, model, error_type; latency_ms becomes a float.
* Phase 1 data is carried over: each Phase 1 result's score/evidence becomes
  a heuristic_context_precision@1.0.0 metric_scores row (labeled
  fixture-based when the run was local-deterministic), and a Phase 1 run
  left 'running' (the CLI died mid-run) is marked failed, since no worker
  will ever pick it up.
* DB-level immutability, same pattern as dataset versions: once a run is
  completed or failed, its row can't be updated or deleted and its results
  and metric scores can't be inserted, updated, or deleted.

SQLite note: every column change here is a native ALTER TABLE, never an
Alembic batch operation -- a batch rebuild of test_cases would silently drop
the dataset immutability triggers created by b17cf04aecaf.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e2a91d7b3f"
down_revision: str | None = "b17cf04aecaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PG_TRIGGERS_UP = [
    """
    CREATE OR REPLACE FUNCTION agentforge_block_finished_run_mutation()
    RETURNS TRIGGER AS $$
    BEGIN
        IF OLD.status::text IN ('completed', 'failed') THEN
            RAISE EXCEPTION 'evaluation run % is % and immutable', OLD.id, OLD.status
                USING ERRCODE = '23000';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_run_mutation
    BEFORE UPDATE OR DELETE ON evaluation_runs
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_run_mutation();
    """,
    """
    CREATE OR REPLACE FUNCTION agentforge_block_finished_run_result_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT status::text INTO v_status FROM evaluation_runs
        WHERE id = COALESCE(NEW.evaluation_run_id, OLD.evaluation_run_id);
        IF v_status IN ('completed', 'failed') THEN
            RAISE EXCEPTION 'evaluation results are immutable once their run is completed or failed'
                USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_run_result_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON evaluation_results
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_run_result_mutation();
    """,
    """
    CREATE OR REPLACE FUNCTION agentforge_block_finished_run_metric_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT r.status::text INTO v_status
        FROM evaluation_results er JOIN evaluation_runs r ON r.id = er.evaluation_run_id
        WHERE er.id = COALESCE(NEW.evaluation_result_id, OLD.evaluation_result_id);
        IF v_status IN ('completed', 'failed') THEN
            RAISE EXCEPTION 'metric scores are immutable once their run is completed or failed'
                USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_run_metric_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON metric_scores
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_run_metric_mutation();
    """,
]

_PG_TRIGGERS_DOWN = [
    "DROP TRIGGER IF EXISTS trg_block_finished_run_metric_mutation ON metric_scores;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_run_metric_mutation();",
    "DROP TRIGGER IF EXISTS trg_block_finished_run_result_mutation ON evaluation_results;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_run_result_mutation();",
    "DROP TRIGGER IF EXISTS trg_block_finished_run_mutation ON evaluation_runs;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_run_mutation();",
]

_FINISHED = "('completed', 'failed')"
_RUN_MSG = "evaluation runs are immutable once completed or failed"
_RESULT_MSG = "evaluation results are immutable once their run is completed or failed"
_METRIC_MSG = "metric scores are immutable once their run is completed or failed"
_RESULT_RUN_STATUS = "(SELECT status FROM evaluation_runs WHERE id = {row}.evaluation_run_id)"
_METRIC_RUN_STATUS = (
    "(SELECT r.status FROM evaluation_results er JOIN evaluation_runs r ON r.id = er.evaluation_run_id "
    "WHERE er.id = {row}.evaluation_result_id)"
)


def _sqlite_trigger(name: str, when: str, table: str, condition: str, message: str) -> str:
    return (
        f"CREATE TRIGGER {name} BEFORE {when} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, '{message}'); END;"
    )


_SQLITE_TRIGGERS = [
    ("trg_block_finished_run_update", "UPDATE", "evaluation_runs", f"OLD.status IN {_FINISHED}", _RUN_MSG),
    ("trg_block_finished_run_delete", "DELETE", "evaluation_runs", f"OLD.status IN {_FINISHED}", _RUN_MSG),
    (
        "trg_block_finished_result_insert",
        "INSERT",
        "evaluation_results",
        f"{_RESULT_RUN_STATUS.format(row='NEW')} IN {_FINISHED}",
        _RESULT_MSG,
    ),
    (
        "trg_block_finished_result_update",
        "UPDATE",
        "evaluation_results",
        f"{_RESULT_RUN_STATUS.format(row='OLD')} IN {_FINISHED}",
        _RESULT_MSG,
    ),
    (
        "trg_block_finished_result_delete",
        "DELETE",
        "evaluation_results",
        f"{_RESULT_RUN_STATUS.format(row='OLD')} IN {_FINISHED}",
        _RESULT_MSG,
    ),
    (
        "trg_block_finished_metric_insert",
        "INSERT",
        "metric_scores",
        f"{_METRIC_RUN_STATUS.format(row='NEW')} IN {_FINISHED}",
        _METRIC_MSG,
    ),
    (
        "trg_block_finished_metric_update",
        "UPDATE",
        "metric_scores",
        f"{_METRIC_RUN_STATUS.format(row='OLD')} IN {_FINISHED}",
        _METRIC_MSG,
    ),
    (
        "trg_block_finished_metric_delete",
        "DELETE",
        "metric_scores",
        f"{_METRIC_RUN_STATUS.format(row='OLD')} IN {_FINISHED}",
        _METRIC_MSG,
    ),
]


def _json_literal(dialect: str, literal: str) -> str:
    return f"CAST('{literal}' AS JSON)" if dialect == "postgresql" else f"'{literal}'"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"evaluation engine migration not implemented for dialect '{dialect}'")

    # -- enum values (Postgres only; SQLite enums are plain strings) ----------
    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'pending' BEFORE 'running'")
            op.execute("ALTER TYPE resultstatus ADD VALUE IF NOT EXISTS 'timeout'")

    # -- test_cases: answer assertions -----------------------------------------
    op.add_column(
        "test_cases",
        sa.Column("expected_answer_contains", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("test_cases", sa.Column("expected_answer_regex", sa.String(), nullable=True))

    # -- evaluation_runs ---------------------------------------------------------
    op.add_column("evaluation_runs", sa.Column("evaluators", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    for column in (
        sa.Column("adapter_type", sa.String(20), nullable=True),
        sa.Column("adapter_target", sa.String(2000), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("max_latency_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("aggregates", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("evaluation_runs", column)

    if dialect == "postgresql":
        op.execute(
            "UPDATE evaluation_runs SET evaluators = json_build_array(evaluator_name || '@' || evaluator_version)"
        )
    else:
        op.execute("UPDATE evaluation_runs SET evaluators = json_array(evaluator_name || '@' || evaluator_version)")
    op.execute(
        "UPDATE evaluation_runs SET status = 'failed', completed_at = created_at, "
        "error_message = 'Phase 1 client-side run that never completed "
        "(marked failed by the evaluation engine migration)' "
        "WHERE status = 'running'"
    )

    # -- evaluation_results + metric_scores -------------------------------------
    for column in (
        sa.Column("citations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("error_type", sa.String(200), nullable=True),
    ):
        op.add_column("evaluation_results", column)
    if dialect == "postgresql":
        # SQLite's dynamic typing already stores floats in an INTEGER column.
        op.alter_column(
            "evaluation_results",
            "latency_ms",
            type_=sa.Float(),
            existing_type=sa.Integer(),
            postgresql_using="latency_ms::double precision",
        )

    op.create_table(
        "metric_scores",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(36), nullable=False),
        sa.Column("evaluator_name", sa.String(100), nullable=False),
        sa.Column("evaluator_version", sa.String(50), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metric_scores_evaluation_result_id", "metric_scores", ["evaluation_result_id"])
    op.create_index("ix_evaluation_results_evaluation_run_id", "evaluation_results", ["evaluation_run_id"])

    new_id = "gen_random_uuid()::text" if dialect == "postgresql" else "lower(hex(randomblob(16)))"
    op.execute(
        f"""
        INSERT INTO metric_scores
            (id, evaluation_result_id, evaluator_name, evaluator_version, score, value, unit,
             passed, reason, evidence, labels, created_at)
        SELECT {new_id}, er.id, r.evaluator_name, r.evaluator_version, er.score, NULL, NULL,
               er.passed, 'carried over from a Phase 1 client-side run', er.evidence,
               CASE WHEN r.provider_type = 'local-deterministic'
                    THEN {_json_literal(dialect, '["fixture-based"]')}
                    ELSE {_json_literal(dialect, "[]")} END,
               er.created_at
        FROM evaluation_results er JOIN evaluation_runs r ON r.id = er.evaluation_run_id
        WHERE er.status = 'ok' AND er.score IS NOT NULL
        """
    )

    op.drop_column("evaluation_results", "score")
    op.drop_column("evaluation_results", "evidence")
    op.drop_column("evaluation_runs", "evaluator_name")
    op.drop_column("evaluation_runs", "evaluator_version")

    # -- immutability triggers (last: the backfill above writes finished runs) --
    if dialect == "postgresql":
        for stmt in _PG_TRIGGERS_UP:
            op.execute(stmt)
    else:
        for name, when, table, condition, message in _SQLITE_TRIGGERS:
            op.execute(_sqlite_trigger(name, when, table, condition, message))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    worker_runs = bind.execute(
        sa.text("SELECT COUNT(*) FROM evaluation_runs WHERE adapter_type IS NOT NULL")
    ).scalar_one()
    if worker_runs:
        raise RuntimeError(
            f"cannot downgrade: {worker_runs} worker-executed evaluation run(s) exist, and the previous "
            "schema can't represent multi-evaluator results, timeouts, or pending runs. Delete them first."
        )

    if dialect == "postgresql":
        for stmt in _PG_TRIGGERS_DOWN:
            op.execute(stmt)
    else:
        for name, *_ in _SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {name};")

    op.add_column("evaluation_runs", sa.Column("evaluator_name", sa.String(100), nullable=True))
    op.add_column("evaluation_runs", sa.Column("evaluator_version", sa.String(50), nullable=True))
    first = "evaluators->>0" if dialect == "postgresql" else "json_extract(evaluators, '$[0]')"
    if dialect == "postgresql":
        op.execute(
            f"UPDATE evaluation_runs SET evaluator_name = split_part({first}, '@', 1), "
            f"evaluator_version = split_part({first}, '@', 2)"
        )
    else:
        op.execute(
            f"UPDATE evaluation_runs SET evaluator_name = substr({first}, 1, instr({first}, '@') - 1), "
            f"evaluator_version = substr({first}, instr({first}, '@') + 1)"
        )
    if dialect == "postgresql":
        # SQLite can't add NOT NULL without a batch table rebuild; the restored
        # legacy columns stay nullable there.
        op.alter_column("evaluation_runs", "evaluator_name", existing_type=sa.String(100), nullable=False)
        op.alter_column("evaluation_runs", "evaluator_version", existing_type=sa.String(50), nullable=False)

    op.add_column("evaluation_results", sa.Column("score", sa.Float(), nullable=True))
    op.add_column(
        "evaluation_results", sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
    )
    op.execute(
        "UPDATE evaluation_results SET "
        "score = (SELECT m.score FROM metric_scores m WHERE m.evaluation_result_id = evaluation_results.id), "
        "evidence = COALESCE((SELECT m.evidence FROM metric_scores m "
        "WHERE m.evaluation_result_id = evaluation_results.id), evidence)"
    )
    op.drop_index("ix_evaluation_results_evaluation_run_id", table_name="evaluation_results")
    op.drop_index("ix_metric_scores_evaluation_result_id", table_name="metric_scores")
    op.drop_table("metric_scores")

    if dialect == "postgresql":
        op.alter_column(
            "evaluation_results",
            "latency_ms",
            type_=sa.Integer(),
            existing_type=sa.Float(),
            postgresql_using="round(latency_ms)::integer",
        )
    for column in ("error_type", "model", "output_tokens", "input_tokens", "citations"):
        op.drop_column("evaluation_results", column)
    for column in (
        "started_at",
        "aggregates",
        "error_message",
        "max_latency_ms",
        "timeout_seconds",
        "adapter_target",
        "adapter_type",
        "evaluators",
    ):
        op.drop_column("evaluation_runs", column)
    op.drop_column("test_cases", "expected_answer_regex")
    op.drop_column("test_cases", "expected_answer_contains")
    # Postgres can't drop enum values; 'pending'/'timeout' stay in the types
    # (unused) and the upgrade adds them with IF NOT EXISTS.
