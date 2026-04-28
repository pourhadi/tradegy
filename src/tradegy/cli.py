"""tradegy CLI — operator entrypoints for the feature pipeline."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from tradegy.audit.basic import audit_source
from tradegy.features import transforms  # noqa: F401  — register transforms
from tradegy.features.engine import compute_feature
from tradegy.ingest.csv_es import ingest_csv
from tradegy.ingest.csv_sierra import ingest_sierra_csv
from tradegy.registry.api import find_features, get_feature, value_at
from tradegy.registry.loader import load_data_source, load_feature
from tradegy.validate.no_lookahead import audit_no_lookahead
from tradegy.validate.reproducibility import check_reproducibility

app = typer.Typer(help="Tradegy feature pipeline.", no_args_is_help=True)
registry_app = typer.Typer(help="Registry queries.", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
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
    max_gap_seconds: Annotated[float, typer.Option()] = 60.0,
) -> None:
    """Run Stage 2 (basic) audit for an ingested source."""
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


if __name__ == "__main__":
    app()
