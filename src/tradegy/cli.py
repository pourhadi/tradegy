"""tradegy CLI — operator entrypoints for the feature pipeline."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from tradegy import config
from tradegy.audit.basic import audit_source
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
      * generic_csv (or omitted ingest spec) → ingest_csv (ts/price/size).
    """
    source = load_data_source(source_id)
    fmt = source.ingest.format if source.ingest is not None else "generic_csv"
    if fmt == "sierra_chart_csv":
        result = ingest_sierra_csv(csv_path, source, input_tz=input_tz)
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


@app.command("walk-forward")
def walk_forward_cmd(
    spec_id: Annotated[str, typer.Argument(help="strategy spec id")],
    train_years: Annotated[float, typer.Option(help="train window length")] = 3.0,
    test_years: Annotated[float, typer.Option(help="test (OOS) window length")] = 1.0,
    roll_years: Annotated[float, typer.Option(help="roll step between windows")] = 1.0,
    coverage_start: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's first ts")] = None,
    coverage_end: Annotated[Optional[str], typer.Option(help="ISO 8601; defaults to bar feature's last ts")] = None,
    tick_size: Annotated[float, typer.Option()] = 0.25,
    slippage_ticks: Annotated[float, typer.Option()] = 0.5,
    commission_round_trip: Annotated[float, typer.Option()] = 1.50,
) -> None:
    """Run rolling walk-forward validation. Same parameters in both halves
    of every window — exposes overfitting and regime fragility.

    Gate (per 07_auto_generation.md:171): avg OOS Sharpe must be ≥ 50%
    of avg in-sample Sharpe, AND in-sample Sharpe must be positive.
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
    if not summary.passed:
        raise typer.Exit(code=4)


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
    if not summary.passed:
        raise typer.Exit(code=5)


if __name__ == "__main__":
    app()
