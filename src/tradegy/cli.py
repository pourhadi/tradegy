"""tradegy CLI — operator entrypoints for the feature pipeline."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from dateutil.relativedelta import relativedelta
from rich.console import Console
from rich.table import Table

from tradegy import config
from tradegy.audit.basic import audit_source
from tradegy.auto_generation import (
    AnthropicHypothesisGenerator,
    AnthropicVariantGenerator,
    AutoTestOrchestrator,
    StubVariantGenerator,
    format_cost_line,
    list_hypotheses,
    load_hypothesis,
)
from tradegy.auto_generation.generators import GenerationContext
from tradegy.auto_generation.hypothesis import hypotheses_dir
from tradegy.auto_generation.records import read_records
from tradegy.evidence import (
    build_packet,
    read_packet,
    signing_mode,
    write_packet,
)
from tradegy.evidence.packet import verify_packet
from tradegy.features import transforms  # noqa: F401  — register transforms
from tradegy.features.engine import compute_feature
from tradegy.harness import (
    CPCVConfig,
    CostModel,
    WalkForwardConfig,
    run_backtest,
    run_cpcv,
    run_walk_forward,
)
from tradegy.ingest.csv_databento import ingest_databento_csv
from tradegy.ingest.csv_es import ingest_csv
from tradegy.ingest.csv_sierra import ingest_sierra_csv
from tradegy.registry.api import find_features, get_feature, value_at
from tradegy.registry.loader import load_data_source, load_feature
from tradegy.specs import load_spec
from tradegy.strategies import auxiliary_classes  # noqa: F401  — register
from tradegy.strategies import classes  # noqa: F401  — register
from tradegy.validate.no_lookahead import audit_no_lookahead
from tradegy.validate.reproducibility import check_reproducibility

app = typer.Typer(help="Tradegy feature pipeline.", no_args_is_help=True)
registry_app = typer.Typer(help="Registry queries.", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
live_app = typer.Typer(help="Live adapters (parity contract).", no_args_is_help=True)
app.add_typer(live_app, name="live")
console = Console()


@app.command()
def ingest(
    csv_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option(help="data source id")],
    input_tz: Annotated[str, typer.Option(help="IANA tz of CSV timestamps")] = "UTC",
) -> None:
    """Ingest a CSV for an admitted data source (Stage 1).

    Dispatches on `source.ingest.format`:
      * sierra_chart_csv → ingest_sierra_csv (multi-column timestamp, OHLCV).
      * databento_ohlcv_csv → ingest_databento_csv (per-contract OHLCV with
        no-lookahead front-month roll).
      * generic_csv (or omitted ingest spec) → ingest_csv (ts/price/size).
    """
    source = load_data_source(source_id)
    fmt = source.ingest.format if source.ingest is not None else "generic_csv"
    if fmt == "sierra_chart_csv":
        result = ingest_sierra_csv(csv_path, source, input_tz=input_tz)
    elif fmt == "databento_ohlcv_csv":
        result = ingest_databento_csv(csv_path, source)
    elif fmt == "generic_csv":
        result = ingest_csv(csv_path, source, input_tz=input_tz)
    else:
        raise typer.BadParameter(f"unknown ingest format {fmt!r}")
    console.print(
        f"[green]ingested[/] {result.rows_in} rows ({result.duplicates_dropped} dedup'd) "
        f"into {len(result.partitions_written)} partitions"
    )
    console.print(f"  coverage: {result.coverage_start} → {result.coverage_end}")
    console.print(f"  batch_id: {result.batch_id}")


@app.command()
def audit(
    source_id: Annotated[str, typer.Argument()],
    max_gap_seconds: Annotated[Optional[float], typer.Option(
        help="override the source's max_inactivity_seconds (default: per-source)"
    )] = None,
) -> None:
    """Run Stage 2 (basic) audit for an ingested source.

    The gap-tolerance threshold is read from the source registry's
    `max_inactivity_seconds` field; pass `--max-gap-seconds` to override
    for one run. Falls back to 60s if neither is set.
    """
    source = load_data_source(source_id)
    report = audit_source(source, max_gap_seconds=max_gap_seconds)
    console.print(
        f"[bold]{source_id}[/] rows={report.row_count} "
        f"unique_ts={report.deduplicated_count} "
        f"coverage=[{report.coverage_start} → {report.coverage_end}]"
    )
    if not report.findings:
        console.print("[green]no findings[/]")
        return
    table = Table(title="audit findings")
    table.add_column("severity")
    table.add_column("code")
    table.add_column("message")
    for f in report.findings:
        color = {"CRITICAL": "red", "HIGH": "bright_red", "MEDIUM": "yellow"}.get(
            f.severity, "white"
        )
        table.add_row(f"[{color}]{f.severity}[/]", f.code, f.message)
    console.print(table)
    if report.has_critical:
        raise typer.Exit(code=2)


@app.command("compute-feature")
def compute_feature_cmd(feature_id: Annotated[str, typer.Argument()]) -> None:
    """Materialize a registered feature (Stage 4 + 7)."""
    result = compute_feature(feature_id)
    console.print(
        f"[green]computed[/] {result.feature_id}@{result.feature_version}: "
        f"{result.rows} rows → {result.out_path}"
    )
    if result.rows:
        console.print(
            f"  coverage: {result.coverage_start} → {result.coverage_end}"
        )


@app.command()
def validate(
    feature_id: Annotated[str, typer.Argument()],
    samples: Annotated[int, typer.Option()] = 200,
    seed: Annotated[int, typer.Option()] = 0,
) -> None:
    """Run no-lookahead audit + reproducibility check (Stage 6)."""
    nl = audit_no_lookahead(feature_id, samples=samples, seed=seed)
    rep = check_reproducibility(feature_id)
    if nl.passed:
        console.print(f"[green]no-lookahead PASS[/] {nl.matches}/{nl.samples}")
    else:
        console.print(
            f"[red]no-lookahead FAIL[/] {nl.matches}/{nl.samples} matched"
        )
        for m in nl.mismatches[:10]:
            console.print(f"  {m}")
    if rep.passed:
        console.print(f"[green]reproducibility PASS[/] {rep.rows_compared} rows")
    else:
        console.print(
            f"[red]reproducibility FAIL[/] {rep.mismatches} mismatches "
            f"of {rep.rows_compared} rows"
        )
    if not (nl.passed and rep.passed):
        raise typer.Exit(code=3)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@registry_app.command("get")
def registry_get(
    feature_id: Annotated[str, typer.Argument()],
    version: Annotated[Optional[str], typer.Option()] = None,
    start: Annotated[Optional[str], typer.Option(help="ISO 8601 (Z OK)")] = None,
    end: Annotated[Optional[str], typer.Option(help="ISO 8601 (Z OK)")] = None,
    as_of: Annotated[Optional[str], typer.Option(help="ISO 8601 (Z OK)")] = None,
    limit: Annotated[int, typer.Option()] = 20,
) -> None:
    """Q1 — feature retrieval with availability_latency pre-applied."""
    df = get_feature(
        feature_id,
        version=version,
        start=_parse_iso(start),
        end=_parse_iso(end),
        as_of=_parse_iso(as_of),
    )
    console.print(f"{df.height} rows (showing first {min(limit, df.height)})")
    console.print(df.head(limit))


@registry_app.command("value-at")
def registry_value_at(
    feature_id: Annotated[str, typer.Argument()],
    ts: Annotated[str, typer.Argument(help="ISO 8601 (Z OK)")],
    version: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """Q5 — audit-trail value lookup."""
    res = value_at(feature_id, _parse_iso(ts), version=version)
    if res is None:
        console.print("[yellow]no value available at or before that time[/]")
        raise typer.Exit(code=1)
    console.print(json.dumps({k: str(v) for k, v in res.items()}, indent=2))


@registry_app.command("list")
def registry_list(
    cadence: Annotated[Optional[str], typer.Option()] = None,
    max_latency_seconds: Annotated[Optional[int], typer.Option()] = None,
) -> None:
    feats = find_features(cadence=cadence, max_latency_seconds=max_latency_seconds)
    table = Table(title="features")
    table.add_column("id")
    table.add_column("version")
    table.add_column("cadence")
    table.add_column("latency_s")
    table.add_column("state")
    for f in feats:
        table.add_row(
            f.id,
            f.version,
            f.cadence,
            str(f.availability_latency_seconds),
            f.lifecycle_state,
        )
    console.print(table)


@registry_app.command("show")
def registry_show(feature_id: Annotated[str, typer.Argument()]) -> None:
    f = load_feature(feature_id)
    console.print_json(f.model_dump_json())


@live_app.command("test-connection")
def live_test_connection(source_id: Annotated[str, typer.Argument()]) -> None:
    """Connect via the source's live adapter, qualify the contract, report
    health. Does NOT subscribe — useful to verify TWS reachability and the
    contract resolution path without exercising the (currently stubbed)
    subscribe body.
    """
    import asyncio

    from tradegy.live import get_live_adapter

    source = load_data_source(source_id)
    if source.live is None:
        console.print(f"[red]source {source_id!r} has no `live` adapter declared[/]")
        raise typer.Exit(code=2)
    adapter = get_live_adapter(source.live.adapter)

    async def _run() -> dict:
        await adapter.connect()
        try:
            contract = adapter.qualify(source.live)  # type: ignore[attr-defined]
            return adapter.health() | {"qualified": str(contract)}
        finally:
            await adapter.disconnect()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — surface the underlying error
        console.print(f"[red]connection failed:[/] {exc!r}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(result, default=str))


@app.command()
def backtest(
    spec_id: Annotated[str, typer.Argument(help="strategy spec id; resolves to strategies/<id>.yaml")],
    start: Annotated[Optional[str], typer.Option(help="ISO 8601")] = None,
    end: Annotated[Optional[str], typer.Option(help="ISO 8601")] = None,
    tick_size: Annotated[float, typer.Option(help="instrument tick size")] = 0.25,
    slippage_ticks: Annotated[float, typer.Option(help="adverse slippage per side, in ticks")] = 0.5,
    commission_round_trip: Annotated[float, typer.Option(help="commission per contract round trip")] = 1.50,
) -> None:
    """Run a single-spec single-window backtest (Phase 3A: `single` mode).

    Reads strategies/<spec_id>.yaml, resolves all class references,
    materializes bars + features for the spec's instrument, and runs the
    deterministic state-machine driver to produce trades + aggregate
    stats.
    """
    spec_path = config.strategy_specs_dir() / f"{spec_id}.yaml"
    spec = load_spec(spec_path)
    cost = CostModel(
        tick_size=tick_size,
        slippage_ticks_per_side=slippage_ticks,
        commission_per_contract_round_trip=commission_round_trip,
    )
    result = run_backtest(
        spec,
        start=_parse_iso(start),
        end=_parse_iso(end),
        cost=cost,
    )
    s = result.stats
    console.print(
        f"[bold]{result.spec_id}@{result.spec_version}[/]  "
        f"bars={result.total_bars}  "
        f"window=[{result.coverage_start} → {result.coverage_end}]"
    )
    if s is None or s.total_trades == 0:
        console.print("[yellow]no trades[/]")
        return
    table = Table(title="aggregate stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total_trades", f"{s.total_trades}")
    table.add_row("expectancy_R", f"{s.expectancy_R:+.4f}")
    table.add_row("total_pnl", f"{s.total_pnl:+.2f}")
    table.add_row("total_pnl_R", f"{s.total_pnl_R:+.2f}")
    table.add_row("win_rate", f"{s.win_rate:.1%}")
    table.add_row("avg_win_R", f"{s.avg_win_R:+.3f}")
    table.add_row("avg_loss_R", f"{s.avg_loss_R:+.3f}")
    table.add_row("profit_factor", f"{s.profit_factor:.2f}")
    table.add_row("avg_holding_bars", f"{s.avg_holding_bars:.1f}")
    table.add_row("per_trade_sharpe", f"{s.sharpe:.3f}")
    table.add_row("max_drawdown", f"{s.max_drawdown:.2f}")
    console.print(table)
    _write_run_evidence(
        spec=spec,
        spec_id=spec_id,
        run_type="backtest",
        cost=cost,
        coverage_start=result.coverage_start,
        coverage_end=result.coverage_end,
        payload={
            "stats": {
                "total_trades": s.total_trades,
                "expectancy_R": s.expectancy_R,
                "total_pnl": s.total_pnl,
                "total_pnl_R": s.total_pnl_R,
                "win_rate": s.win_rate,
                "avg_win_R": s.avg_win_R,
                "avg_loss_R": s.avg_loss_R,
                "profit_factor": s.profit_factor,
                "avg_holding_bars": s.avg_holding_bars,
                "sharpe": s.sharpe,
                "max_drawdown": s.max_drawdown,
            },
            "total_bars": result.total_bars,
            "sessions_traversed": result.sessions_traversed,
        },
    )


def _cost_model_dict(cost: CostModel) -> dict[str, float]:
    return {
        "tick_size": float(cost.tick_size),
        "slippage_ticks_per_side": float(cost.slippage_ticks_per_side),
        "commission_per_contract_round_trip": float(
            cost.commission_per_contract_round_trip
        ),
    }


def _write_run_evidence(
    *,
    spec,
    spec_id: str,
    run_type: str,
    cost: CostModel,
    coverage_start: datetime,
    coverage_end: datetime,
    payload: dict,
) -> None:
    """Build, sign, and persist the evidence packet for a harness run."""
    spec_path = config.strategy_specs_dir() / f"{spec_id}.yaml"
    packet = build_packet(
        spec_id=spec.metadata.id,
        spec_version=spec.metadata.version,
        spec_path=spec_path,
        run_type=run_type,  # type: ignore[arg-type]
        cost_model=_cost_model_dict(cost),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        payload=payload,
    )
    out_path = write_packet(packet)
    mode = signing_mode()
    style = "green" if mode == "HMAC-SHA256" else "yellow"
    console.print(
        f"[{style}]evidence[/] {mode}  → {out_path.relative_to(config.repo_root())}"
    )


def _evaluate_holdout(
    spec,
    *,
    holdout_start: datetime,
    holdout_end: datetime,
    reference_sharpe: float,
    cost: CostModel,
) -> tuple[bool, str, float]:
    """Run a single backtest over the held-out trailing window and return
    (passed, message, holdout_sharpe).

    Holdout gate per `07_auto_generation.md:165`: holdout Sharpe must be
    ≥ 0.5 × the reference (walk-forward / CPCV) Sharpe.
    """
    result = run_backtest(
        spec,
        start=holdout_start,
        end=holdout_end,
        cost=cost,
    )
    holdout_sharpe = result.stats.sharpe if result.stats is not None else 0.0
    threshold = 0.5 * reference_sharpe
    if reference_sharpe <= 0:
        passed = False
        msg = (
            f"reference sharpe {reference_sharpe:+.3f} is ≤ 0; holdout "
            "gate cannot pass without prior-stage edge"
        )
    else:
        passed = holdout_sharpe >= threshold
        msg = (
            f"holdout sharpe {holdout_sharpe:+.3f} "
            f"{'≥' if passed else '<'} 0.5 × reference "
            f"({reference_sharpe:+.3f}) = {threshold:+.3f}"
        )
    return passed, msg, holdout_sharpe


@app.command("walk-forward")
def walk_forward_cmd(
    spec_id: Annotated[str, typer.Argument(help="strategy spec id")],
    train_years: Annotated[float, typer.Option(help="train window length")] = 3.0,
    test_years: Annotated[float, typer.Option(help="test (OOS) window length")] = 1.0,
    roll_years: Annotated[float, typer.Option(help="roll step between windows")] = 1.0,
    coverage_start: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's first ts")] = None,
    coverage_end: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's last ts")] = None,
    holdout_months: Annotated[int, typer.Option(
        help="trailing months reserved untouched as holdout. After "
             "walk-forward completes on [coverage_start, coverage_end - "
             "holdout_months), a single backtest runs on the held-out "
             "window and is gated at 0.5× the avg OOS Sharpe per "
             "07_auto_generation.md:165. 0 disables holdout."
    )] = 0,
    tick_size: Annotated[float, typer.Option()] = 0.25,
    slippage_ticks: Annotated[float, typer.Option()] = 0.5,
    commission_round_trip: Annotated[float, typer.Option()] = 1.50,
) -> None:
    """Run rolling walk-forward validation. Same parameters in both halves
    of every window — exposes overfitting and regime fragility.

    Gate (per 07_auto_generation.md:171): avg OOS Sharpe must be ≥ 50%
    of avg in-sample Sharpe, AND in-sample Sharpe must be positive.

    With --holdout-months > 0, after the walk-forward gate, a separate
    backtest runs on the trailing held-out window and is gated at 0.5×
    the avg OOS Sharpe per 07_auto_generation.md:165. The held-out
    window is reserved from all walk-forward folds (point-in-time
    correct: no fold ever sees data inside the holdout).
    """
    spec_path = config.strategy_specs_dir() / f"{spec_id}.yaml"
    spec = load_spec(spec_path)
    cost = CostModel(
        tick_size=tick_size,
        slippage_ticks_per_side=slippage_ticks,
        commission_per_contract_round_trip=commission_round_trip,
    )
    cfg = WalkForwardConfig(
        train_years=train_years, test_years=test_years, roll_years=roll_years,
    )

    # Default coverage to the bar feature's full range when not given.
    cs = _parse_iso(coverage_start)
    ce = _parse_iso(coverage_end)
    if cs is None or ce is None:
        from tradegy.harness.data import load_bar_stream
        bars = load_bar_stream(spec.market_scope.instrument)
        if cs is None:
            cs = bars.row(0, named=True)["ts_utc"]
        if ce is None:
            ce = bars.row(-1, named=True)["ts_utc"]

    # Reserve the trailing N months as the holdout window. The walk-
    # forward gate runs on [cs, ce - N months); the holdout backtest
    # runs on [ce - N months, ce] AFTER the gate decision.
    holdout_start: Optional[datetime] = None
    holdout_end: Optional[datetime] = None
    if holdout_months > 0:
        holdout_end = ce
        holdout_start = ce - relativedelta(months=holdout_months)
        ce = holdout_start

    summary = run_walk_forward(
        spec,
        coverage_start=cs,
        coverage_end=ce,
        config=cfg,
        cost=cost,
    )
    console.print(
        f"[bold]{summary.spec_id}@{summary.spec_version}[/]  "
        f"windows={len(summary.windows)}  "
        f"coverage=[{summary.coverage_start} → {summary.coverage_end}]  "
        f"config={cfg.train_years}y/{cfg.test_years}y/{cfg.roll_years}y"
    )
    table = Table(title="walk-forward windows")
    table.add_column("#")
    table.add_column("train")
    table.add_column("test")
    table.add_column("IS sharpe", justify="right")
    table.add_column("OOS sharpe", justify="right")
    table.add_column("IS trades", justify="right")
    table.add_column("OOS trades", justify="right")
    for w in summary.windows:
        is_s = w.in_sample.stats if w.in_sample else None
        oos_s = w.out_of_sample.stats if w.out_of_sample else None
        table.add_row(
            str(w.index),
            f"{w.train_start.date()}→{w.train_end.date()}",
            f"{w.test_start.date()}→{w.test_end.date()}",
            f"{is_s.sharpe:+.3f}" if is_s else "—",
            f"{oos_s.sharpe:+.3f}" if oos_s else "—",
            f"{is_s.total_trades}" if is_s else "—",
            f"{oos_s.total_trades}" if oos_s else "—",
        )
    console.print(table)
    agg = Table(title="aggregate")
    agg.add_column("metric")
    agg.add_column("value", justify="right")
    agg.add_row("avg in-sample sharpe", f"{summary.avg_in_sample_sharpe:+.3f}")
    agg.add_row("avg OOS sharpe", f"{summary.avg_oos_sharpe:+.3f}")
    agg.add_row("worst-window OOS sharpe", f"{summary.worst_window_oos_sharpe:+.3f}")
    agg.add_row("avg in-sample trades", f"{summary.avg_in_sample_trades:.1f}")
    agg.add_row("avg OOS trades", f"{summary.avg_oos_trades:.1f}")
    agg.add_row(
        "gate",
        ("[green]PASS[/]" if summary.passed else f"[red]FAIL[/] — {summary.fail_reason}"),
    )
    console.print(agg)

    holdout_payload: dict | None = None
    holdout_passed: bool | None = None
    if holdout_start is not None and holdout_end is not None and summary.passed:
        console.print(
            f"\n[bold]Holdout backtest[/]  window=[{holdout_start} → "
            f"{holdout_end}]  reference avg OOS sharpe="
            f"{summary.avg_oos_sharpe:+.3f}"
        )
        holdout_passed, msg, holdout_sharpe = _evaluate_holdout(
            spec,
            holdout_start=holdout_start,
            holdout_end=holdout_end,
            reference_sharpe=summary.avg_oos_sharpe,
            cost=cost,
        )
        ho = Table(title="holdout gate")
        ho.add_column("metric")
        ho.add_column("value", justify="right")
        ho.add_row("holdout sharpe", f"{holdout_sharpe:+.3f}")
        ho.add_row("reference (avg OOS) sharpe", f"{summary.avg_oos_sharpe:+.3f}")
        ho.add_row(
            "gate",
            "[green]PASS[/]" if holdout_passed else f"[red]FAIL[/] — {msg}",
        )
        console.print(ho)
        holdout_payload = {
            "window_start": holdout_start.isoformat(),
            "window_end": holdout_end.isoformat(),
            "sharpe": holdout_sharpe,
            "reference_sharpe": summary.avg_oos_sharpe,
            "passed": holdout_passed,
            "message": msg,
        }

    _write_run_evidence(
        spec=spec,
        spec_id=spec_id,
        run_type="walk_forward",
        cost=cost,
        coverage_start=summary.coverage_start,
        coverage_end=holdout_end if holdout_end is not None else summary.coverage_end,
        payload={
            "config": {
                "train_years": cfg.train_years,
                "test_years": cfg.test_years,
                "roll_years": cfg.roll_years,
            },
            "windows": [
                {
                    "index": w.index,
                    "train_start": w.train_start.isoformat(),
                    "train_end": w.train_end.isoformat(),
                    "test_start": w.test_start.isoformat(),
                    "test_end": w.test_end.isoformat(),
                    "in_sample_sharpe": (
                        w.in_sample.stats.sharpe if w.in_sample else None
                    ),
                    "in_sample_trades": (
                        w.in_sample.stats.total_trades if w.in_sample else 0
                    ),
                    "oos_sharpe": (
                        w.out_of_sample.stats.sharpe if w.out_of_sample else None
                    ),
                    "oos_trades": (
                        w.out_of_sample.stats.total_trades if w.out_of_sample else 0
                    ),
                }
                for w in summary.windows
            ],
            "avg_in_sample_sharpe": summary.avg_in_sample_sharpe,
            "avg_oos_sharpe": summary.avg_oos_sharpe,
            "worst_window_oos_sharpe": summary.worst_window_oos_sharpe,
            "wf_gate_passed": summary.passed,
            "wf_gate_fail_reason": summary.fail_reason,
            "holdout": holdout_payload,
            "holdout_gate_passed": holdout_passed,
        },
    )

    if not summary.passed:
        raise typer.Exit(code=4)
    if holdout_passed is False:
        raise typer.Exit(code=5)


@app.command("cpcv")
def cpcv_cmd(
    spec_id: Annotated[str, typer.Argument(help="strategy spec id")],
    n_folds: Annotated[int, typer.Option(help="equal-width folds over coverage")] = 10,
    k_test_folds: Annotated[int, typer.Option(help="test folds per path; total paths = C(N, k)")] = 2,
    purge_days: Annotated[float, typer.Option(help="forward-compatible; no-op until fitting is added")] = 0.0,
    embargo_days: Annotated[float, typer.Option(help="forward-compatible; no-op until fitting is added")] = 0.0,
    median_sharpe_threshold: Annotated[float, typer.Option(help="gate threshold per doc 05:343")] = 0.8,
    max_pct_paths_negative: Annotated[float, typer.Option(help="gate threshold per doc 05:343")] = 0.20,
    coverage_start: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's first ts")] = None,
    coverage_end: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's last ts")] = None,
    holdout_months: Annotated[int, typer.Option(
        help="trailing months reserved untouched as holdout. After CPCV "
             "completes on [coverage_start, coverage_end - holdout_months), "
             "a single backtest runs on the held-out window and is gated "
             "at 0.5× the CPCV median Sharpe per "
             "07_auto_generation.md:165. 0 disables holdout."
    )] = 0,
    tick_size: Annotated[float, typer.Option()] = 0.25,
    slippage_ticks: Annotated[float, typer.Option()] = 0.5,
    commission_round_trip: Annotated[float, typer.Option()] = 1.50,
) -> None:
    """Run combinatorial purged cross-validation on a spec.

    Builds C(n_folds, k_test_folds) paths, runs the strategy on each
    path's test folds with frozen parameters, concatenates trades per
    path, and reports the cross-path Sharpe distribution. Gate per
    doc 05:343 (median Sharpe ≥ threshold AND pct paths negative ≤
    max).

    With --holdout-months > 0, after the CPCV gate, a separate backtest
    runs on the trailing held-out window. The held-out window is
    excluded from all CPCV folds (point-in-time correct).
    """
    spec_path = config.strategy_specs_dir() / f"{spec_id}.yaml"
    spec = load_spec(spec_path)
    cost = CostModel(
        tick_size=tick_size,
        slippage_ticks_per_side=slippage_ticks,
        commission_per_contract_round_trip=commission_round_trip,
    )
    cfg = CPCVConfig(
        n_folds=n_folds,
        k_test_folds=k_test_folds,
        purge_days=purge_days,
        embargo_days=embargo_days,
        median_sharpe_threshold=median_sharpe_threshold,
        max_pct_paths_negative=max_pct_paths_negative,
    )
    cs = _parse_iso(coverage_start)
    ce = _parse_iso(coverage_end)
    if cs is None or ce is None:
        from tradegy.harness.data import load_bar_stream
        bars = load_bar_stream(spec.market_scope.instrument)
        if cs is None:
            cs = bars.row(0, named=True)["ts_utc"]
        if ce is None:
            ce = bars.row(-1, named=True)["ts_utc"]

    holdout_start: Optional[datetime] = None
    holdout_end: Optional[datetime] = None
    if holdout_months > 0:
        holdout_end = ce
        holdout_start = ce - relativedelta(months=holdout_months)
        ce = holdout_start

    summary = run_cpcv(
        spec,
        coverage_start=cs,
        coverage_end=ce,
        config=cfg,
        cost=cost,
    )
    console.print(
        f"[bold]{summary.spec_id}@{summary.spec_version}[/]  "
        f"folds={cfg.n_folds}  k_test={cfg.k_test_folds}  "
        f"paths={len(summary.paths)}  "
        f"coverage=[{summary.coverage_start} → {summary.coverage_end}]"
    )
    table = Table(title="CPCV path Sharpes")
    table.add_column("#")
    table.add_column("test folds")
    table.add_column("trades", justify="right")
    table.add_column("sharpe", justify="right")
    table.add_column("expectancy_R", justify="right")
    for p in summary.paths:
        s = p.stats
        table.add_row(
            str(p.index),
            ",".join(str(i) for i in p.test_fold_indices),
            f"{s.total_trades}" if s else "—",
            f"{s.sharpe:+.3f}" if s else "—",
            f"{s.expectancy_R:+.3f}" if s else "—",
        )
    console.print(table)
    agg = Table(title="aggregate")
    agg.add_column("metric")
    agg.add_column("value", justify="right")
    agg.add_row("paths with trades", f"{summary.paths_with_trades}/{len(summary.paths)}")
    agg.add_row("median Sharpe", f"{summary.median_sharpe:+.3f}")
    agg.add_row("IQR Sharpe", f"{summary.iqr_sharpe:.3f}")
    agg.add_row("pct paths negative", f"{summary.pct_paths_negative:.1%}")
    agg.add_row(
        "gate",
        ("[green]PASS[/]" if summary.passed else f"[red]FAIL[/] — {summary.fail_reason}"),
    )
    console.print(agg)

    holdout_payload: dict | None = None
    holdout_passed: bool | None = None
    if holdout_start is not None and holdout_end is not None and summary.passed:
        console.print(
            f"\n[bold]Holdout backtest[/]  window=[{holdout_start} → "
            f"{holdout_end}]  reference (median CPCV) sharpe="
            f"{summary.median_sharpe:+.3f}"
        )
        holdout_passed, msg, holdout_sharpe = _evaluate_holdout(
            spec,
            holdout_start=holdout_start,
            holdout_end=holdout_end,
            reference_sharpe=summary.median_sharpe,
            cost=cost,
        )
        ho = Table(title="holdout gate")
        ho.add_column("metric")
        ho.add_column("value", justify="right")
        ho.add_row("holdout sharpe", f"{holdout_sharpe:+.3f}")
        ho.add_row("reference (median CPCV) sharpe", f"{summary.median_sharpe:+.3f}")
        ho.add_row(
            "gate",
            "[green]PASS[/]" if holdout_passed else f"[red]FAIL[/] — {msg}",
        )
        console.print(ho)
        holdout_payload = {
            "window_start": holdout_start.isoformat(),
            "window_end": holdout_end.isoformat(),
            "sharpe": holdout_sharpe,
            "reference_sharpe": summary.median_sharpe,
            "passed": holdout_passed,
            "message": msg,
        }

    _write_run_evidence(
        spec=spec,
        spec_id=spec_id,
        run_type="cpcv",
        cost=cost,
        coverage_start=summary.coverage_start,
        coverage_end=holdout_end if holdout_end is not None else summary.coverage_end,
        payload={
            "config": {
                "n_folds": cfg.n_folds,
                "k_test_folds": cfg.k_test_folds,
                "purge_days": cfg.purge_days,
                "embargo_days": cfg.embargo_days,
                "median_sharpe_threshold": cfg.median_sharpe_threshold,
                "max_pct_paths_negative": cfg.max_pct_paths_negative,
            },
            "paths": [
                {
                    "index": p.index,
                    "test_fold_indices": list(p.test_fold_indices),
                    "trades": p.stats.total_trades if p.stats else 0,
                    "sharpe": p.stats.sharpe if p.stats else None,
                    "expectancy_R": p.stats.expectancy_R if p.stats else None,
                }
                for p in summary.paths
            ],
            "median_sharpe": summary.median_sharpe,
            "iqr_sharpe": summary.iqr_sharpe,
            "pct_paths_negative": summary.pct_paths_negative,
            "paths_with_trades": summary.paths_with_trades,
            "cpcv_gate_passed": summary.passed,
            "cpcv_gate_fail_reason": summary.fail_reason,
            "holdout": holdout_payload,
            "holdout_gate_passed": holdout_passed,
        },
    )

    if not summary.passed:
        raise typer.Exit(code=5)
    if holdout_passed is False:
        raise typer.Exit(code=6)


@app.command("validate-evidence")
def validate_evidence_cmd(
    path: Annotated[Path, typer.Argument(
        help="evidence packet path (data/evidence/*.json)",
        exists=True, dir_okay=False,
    )],
) -> None:
    """Verify the signature on an evidence packet.

    Per `13_governance_process.md`: promotion to `live` tier requires
    HMAC-signed evidence (TRADEGY_EVIDENCE_KEY set when the harness
    generated the packet). SHA256-only packets are reported as
    tamper-evident-only and rejected for governance-grade decisions.
    """
    packet = read_packet(path)
    passed, msg = verify_packet(packet)
    sig = packet.signature
    algo = sig.get("algorithm", "?")
    console.print(
        f"[bold]{packet.spec_id}@{packet.spec_version}[/]  "
        f"run={packet.run_type}  algo={algo}  generated_at={packet.generated_at}"
    )
    if "warning" in sig:
        console.print(f"[yellow]warning:[/] {sig['warning']}")
    if passed:
        console.print(f"[green]PASS[/] — {msg}")
    else:
        console.print(f"[red]FAIL[/] — {msg}")
        raise typer.Exit(code=7)


def _build_generation_context() -> GenerationContext:
    """Snapshot the live registries for the LLM. Pulls strategy classes,
    feature ids, condition evaluators, sizing methods, and stop methods
    — every primitive a generated spec can reference is listed in the
    cached prompt prefix.
    """
    from tradegy.strategies.auxiliary import (
        list_condition_evaluators,
        list_sizing_classes,
        list_stop_classes,
    )
    from tradegy.strategies.base import list_strategy_classes

    feature_ids = tuple(
        sorted(p.stem for p in config.features_registry_dir().glob("*.yaml"))
    )
    return GenerationContext(
        available_class_ids=tuple(sorted(list_strategy_classes())),
        available_feature_ids=feature_ids,
        available_condition_ids=tuple(sorted(list_condition_evaluators())),
        available_sizing_methods=tuple(sorted(list_sizing_classes())),
        available_stop_methods=tuple(sorted(list_stop_classes())),
        instrument_scope=("MES",),
    )


def _make_anthropic_client() -> "Any":  # noqa: F821 — runtime-imported
    """Build an Anthropic client. Surfaces a clear error if the key
    isn't set rather than waiting for the API to 401.
    """
    import os

    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        console.print(
            "[red]ERROR:[/] ANTHROPIC_API_KEY not set in the environment."
        )
        raise typer.Exit(code=2)
    return anthropic.Anthropic(api_key=key)


@app.command("hypothesize")
def hypothesize_cmd(
    n: Annotated[int, typer.Option(
        "--n", min=1, max=10,
        help="number of hypotheses to draft per call",
    )] = 3,
    seed: Annotated[str, typer.Option(
        "--seed",
        help="optional one-line direction the LLM should bias toward "
        "(market-structure observation, recent news, etc.)",
    )] = "",
    effort: Annotated[str, typer.Option(
        help="opus 4.7 effort level: low | medium | high | xhigh | max",
    )] = "high",
    model: Annotated[str, typer.Option(
        help="anthropic model id",
    )] = "claude-opus-4-7",
) -> None:
    """LLM-driven hypothesis generation.

    Reads the live class + feature registry, prompts Claude for `n`
    hypothesis drafts, wraps each as a full Hypothesis record with
    `status: proposed`, and writes one YAML per hypothesis under
    `hypotheses/`. Doc 06 §39 says promotion is a human decision —
    nothing in this command auto-promotes.
    """
    import yaml

    client = _make_anthropic_client()
    ctx = _build_generation_context()
    gen = AnthropicHypothesisGenerator(
        client=client, model=model, effort=effort,
    )
    console.print(
        f"[bold]hypothesize[/]  model={model}  effort={effort}  n={n}  "
        f"seed={seed!r}"
    )
    hyps = gen.generate(seed=seed, context=ctx, n=n)

    out_dir = hypotheses_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    table = Table(title="generated hypotheses")
    table.add_column("id")
    table.add_column("title")
    table.add_column("path")
    for h in hyps:
        path = out_dir / f"{h.id}.yaml"
        path.write_text(
            yaml.safe_dump(
                h.model_dump(mode="json"), sort_keys=False, allow_unicode=True,
            )
        )
        table.add_row(
            h.id, h.title, str(path.relative_to(config.repo_root())),
        )
    console.print(table)
    if gen.last_cost is not None:
        console.print(format_cost_line(gen.last_cost))


@app.command("auto-vary")
def auto_vary_cmd(
    hypothesis_id: Annotated[str, typer.Argument(
        help="hypothesis id (must be status=promoted)",
    )],
    n: Annotated[int, typer.Option("--n", min=1, max=15)] = 5,
    effort: Annotated[str, typer.Option()] = "medium",
    model: Annotated[str, typer.Option()] = "claude-opus-4-7",
) -> None:
    """LLM-driven variant generation for a promoted hypothesis.

    Loads the hypothesis YAML, requires it to be `status: promoted`
    per doc 07 § Where auto-generation is allowed, prompts Claude for
    `n` strategy specs, validates each against the schema, and writes
    one YAML per spec under `strategies/`.
    """
    import yaml

    h = load_hypothesis(hypothesis_id)
    if h.status != "promoted":
        console.print(
            f"[red]ERROR:[/] hypothesis {hypothesis_id!r} is "
            f"status={h.status!r}; only `promoted` hypotheses can be "
            "auto-varied (doc 07 § Where auto-generation is allowed)."
        )
        raise typer.Exit(code=2)
    if n > h.variant_budget:
        console.print(
            f"[red]ERROR:[/] requested n={n} exceeds the hypothesis's "
            f"declared variant_budget={h.variant_budget}. Doc 07 §82-90 "
            "forbids post-hoc budget expansion. Edit the hypothesis or "
            "lower n."
        )
        raise typer.Exit(code=2)

    client = _make_anthropic_client()
    ctx = _build_generation_context()
    gen = AnthropicVariantGenerator(
        client=client, model=model, effort=effort,
    )
    console.print(
        f"[bold]auto-vary[/]  hypothesis={hypothesis_id}  model={model}  "
        f"effort={effort}  n={n}/{h.variant_budget}"
    )
    specs = gen.generate(hypothesis=h, context=ctx, n=n)

    out_dir = config.strategy_specs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    table = Table(title="generated variants")
    table.add_column("spec id")
    table.add_column("class")
    table.add_column("path")
    for spec in specs:
        path = out_dir / f"{spec.metadata.id}.yaml"
        path.write_text(
            yaml.safe_dump(
                spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True,
            )
        )
        table.add_row(
            spec.metadata.id,
            spec.entry.strategy_class,
            str(path.relative_to(config.repo_root())),
        )
    console.print(table)
    if gen.last_cost is not None:
        console.print(format_cost_line(gen.last_cost))


@app.command("auto-test")
def auto_test_cmd(
    hypothesis_id: Annotated[str, typer.Argument(
        help="hypothesis id (must be status=promoted)",
    )],
    spec_glob: Annotated[Optional[str], typer.Option(
        "--spec-glob",
        help="glob (under strategies/) selecting variant specs; "
             "defaults to '<hypothesis_id>__variant_*'",
    )] = None,
    run_walk_forward: Annotated[bool, typer.Option(
        "--walk-forward/--sanity-only",
        help="whether to run the walk-forward gate after sanity passes",
    )] = True,
    holdout_months: Annotated[int, typer.Option()] = 0,
) -> None:
    """Run the auto-test orchestrator on every spec already on disk
    that matches the hypothesis. Per-variant records land under
    `data/auto_generation/<hypothesis_id>/variants.jsonl`.

    Pre-registration enforcement (doc 07 §218-228): if the hypothesis
    has already had `variant_budget` records logged, the orchestrator
    refuses — expanding the budget post-hoc is a sprint-level rule.
    """
    h = load_hypothesis(hypothesis_id)
    if h.status != "promoted":
        console.print(
            f"[red]ERROR:[/] hypothesis {hypothesis_id!r} is "
            f"status={h.status!r}; auto-test requires `promoted`."
        )
        raise typer.Exit(code=2)

    pattern = spec_glob or f"{hypothesis_id}__variant_*"
    matches = sorted(config.strategy_specs_dir().glob(f"{pattern}.yaml"))
    if not matches:
        console.print(
            f"[red]ERROR:[/] no specs matched glob {pattern!r}.yaml "
            "under strategies/. Run `tradegy auto-vary` first."
        )
        raise typer.Exit(code=2)

    specs = [load_spec(p) for p in matches]
    console.print(
        f"[bold]auto-test[/]  hypothesis={hypothesis_id}  "
        f"variants_on_disk={len(specs)}  walk_forward={run_walk_forward}"
    )

    ctx = _build_generation_context()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator(specs),
        context=ctx,
        run_walk_forward_on_pass=run_walk_forward,
        run_holdout_on_pass=holdout_months > 0,
        holdout_months=holdout_months,
    )
    summary = orch.run()

    table = Table(title=f"auto-test summary: {hypothesis_id}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("variants generated", str(summary.variants_generated))
    table.add_row("validation failed", str(summary.variants_validation_failed))
    table.add_row("passed sanity", str(summary.variants_passed_sanity))
    table.add_row("passed walk-forward", str(summary.variants_passed_walk_forward))
    table.add_row("candidate pool", str(summary.candidate_count))
    console.print(table)
    if summary.candidate_pool_ids:
        console.print(
            "[green]candidate pool[/]: "
            + ", ".join(summary.candidate_pool_ids)
        )


@app.command("hypothesis-list")
def hypothesis_list_cmd() -> None:
    """List all hypotheses on disk with their status + variant budget."""
    items = list_hypotheses()
    if not items:
        console.print("[yellow]no hypotheses found in hypotheses/[/]")
        return
    table = Table(title="hypotheses")
    table.add_column("id")
    table.add_column("status")
    table.add_column("budget", justify="right")
    table.add_column("title")
    for h in items:
        table.add_row(h.id, h.status, str(h.variant_budget), h.title)
    console.print(table)


if __name__ == "__main__":
    app()
