from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .calibre import CalibreError, scan_library
from .config import load_config
from .db import Database
from .metadata import build_proposals, candidate_from_normalized
from .providers import ProviderError, make_metadata_provider, metadata_query_key

app = typer.Typer(no_args_is_help=True)
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="YAML config file")]


def _db(database_path: Path) -> Database:
    return Database(database_path)


def _load(config: ConfigOpt):
    try:
        return load_config(config)
    except FileNotFoundError as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc


@app.command("init-db")
def init_db(config: ConfigOpt = None):
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    console.print(f"Initialized [bold]{db.path}[/bold]")


@app.command()
def scan(
    library: Annotated[str | None, typer.Option("--library")] = None,
    executable: Annotated[str | None, typer.Option("--executable")] = None,
    report: Annotated[
        bool, typer.Option("--report", help="Show metadata completeness summary.")
    ] = False,
    config: ConfigOpt = None,
):
    cfg = _load(config)
    library = library or cfg.calibre.library
    executable = executable or cfg.calibre.executable
    if library is None:
        raise typer.Exit(code=_print_error("Calibre library is not configured"))
    if executable is None:
        raise typer.Exit(code=_print_error("calibredb executable is not configured"))

    db = _db(cfg.database_path)
    db.initialize()
    try:
        books = scan_library(executable=executable, library=library)
    except CalibreError as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc

    count = 0
    failures: list[tuple[dict, str]] = []
    stats = Counter()

    for book in books:
        result = db.upsert_calibre_book(library, book)
        if result.ok:
            count += 1
        else:
            failures.append((book, result.error or "unknown error"))

        if not book.get("title"):
            stats["missing_title"] += 1
        if not book.get("authors"):
            stats["missing_author"] += 1
        if not book.get("isbn"):
            stats["missing_isbn"] += 1
        if not book.get("publisher"):
            stats["missing_publisher"] += 1
        pubdate = book.get("pubdate")
        if not pubdate or str(pubdate).startswith("0101-01-01"):
            stats["missing_pubdate"] += 1
        if not book.get("languages"):
            stats["missing_language"] += 1
        if book.get("series"):
            stats["has_series"] += 1

    console.print(f"Scanned [bold]{count}[/bold] Calibre records")
    if failures:
        console.print(f"[yellow]Skipped {len(failures)} records requiring metadata repair[/yellow]")

    if report:
        console.print()
        console.print("[bold]Metadata report[/bold]")
        console.print(f"  Total records:       {len(books)}")
        console.print(f"  Missing title:       {stats['missing_title']}")
        console.print(f"  Missing author:      {stats['missing_author']}")
        console.print(f"  Missing ISBN:        {stats['missing_isbn']}")
        console.print(f"  Missing publisher:   {stats['missing_publisher']}")
        console.print(f"  Missing pubdate:     {stats['missing_pubdate']}")
        console.print(f"  Missing language:    {stats['missing_language']}")
        console.print(f"  With series:         {stats['has_series']}")


@app.command()
def metadata(
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Ignore cached provider lookups.")
    ] = False,
    config: ConfigOpt = None,
):
    """Research metadata and store non-destructive repair proposals."""
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()

    provider_name = provider or cfg.metadata.provider
    try:
        metadata_provider = make_metadata_provider(provider_name)
    except ValueError as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc

    rows = db.metadata_candidates(provider=provider_name, limit=limit)
    if not rows:
        console.print("No editions to research.")
        return

    matched = 0
    no_match = 0
    failed = 0
    proposal_count = 0
    cached_count = 0

    for edition in rows:
        query_key = metadata_query_key(
            title=edition["title"], author=edition["author"], isbn=edition["isbn"]
        )
        candidate = None
        lookup_id = None
        used_cache = False

        if cfg.metadata.reuse_cached_lookups and not refresh:
            cached = db.cached_metadata_lookup(
                edition_id=edition["edition_id"],
                provider=provider_name,
                query_key=query_key,
            )
            if cached is not None:
                used_cache = True
                cached_count += 1
                if cached["status"] == "NO_MATCH":
                    no_match += 1
                    _print_metadata_header(edition)
                    console.print(
                        "  [yellow]No confident metadata match[/yellow] [dim](cached)[/dim]"
                    )
                    continue
                candidate = candidate_from_normalized(cached["normalized"], raw=cached["raw"])
                lookup_id = cached["id"]

        if candidate is None:
            try:
                candidate = metadata_provider.lookup(
                    title=edition["title"],
                    author=edition["author"],
                    isbn=edition["isbn"],
                )
            except ProviderError as exc:
                failed += 1
                _print_metadata_header(edition)
                console.print(f"  [red]provider error:[/red] {exc}")
                continue

            if candidate is None:
                no_match += 1
                db.store_metadata_miss(
                    edition_id=edition["edition_id"],
                    provider=provider_name,
                    query_key=query_key,
                )
                _print_metadata_header(edition)
                console.print("  [yellow]No confident metadata match[/yellow]")
                continue

            lookup_id = db.store_metadata_lookup(
                edition_id=edition["edition_id"], candidate=candidate
            )

        assert lookup_id is not None
        matched += 1
        proposals = build_proposals(edition, candidate)
        db.replace_metadata_proposals(
            edition_id=edition["edition_id"],
            lookup_id=lookup_id,
            proposals=proposals,
        )
        proposal_count += len(proposals)

        _print_metadata_header(edition)
        console.print(
            f"  {candidate.provider}: confidence={candidate.identity_confidence:.2f}"
            + (" [dim](cached)[/dim]" if used_cache else "")
        )
        if candidate.original_publication_date:
            console.print(f"  original publication: {candidate.original_publication_date}")
        if candidate.edition_publication_date:
            console.print(f"  edition publication:  {candidate.edition_publication_date}")
        if proposals:
            for proposal in proposals:
                current = _display_value(proposal.current_value)
                proposed = _display_value(proposal.proposed_value)
                console.print(
                    f"  [cyan]propose[/cyan] {proposal.field_name}: {current} -> {proposed} "
                    f"({proposal.confidence:.2f})"
                )
        else:
            console.print("  No missing-field repairs proposed.")

    console.print()
    console.print(
        f"Researched {len(rows)} editions: {matched} matched, {no_match} unmatched, "
        f"{failed} failed; {proposal_count} proposals; {cached_count} cached lookups"
    )
    console.print("[dim]No Calibre metadata was modified.[/dim]")


@app.command()
def stats(config: ConfigOpt = None):
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    with db.connect() as con:
        values = {
            "Works": con.execute("SELECT COUNT(*) FROM works").fetchone()[0],
            "Editions": con.execute("SELECT COUNT(*) FROM editions").fetchone()[0],
            "Metadata lookups": con.execute("SELECT COUNT(*) FROM metadata_lookups").fetchone()[0],
            "Metadata proposals": con.execute(
                "SELECT COUNT(*) FROM metadata_proposals WHERE status='PROPOSED'"
            ).fetchone()[0],
            "Facts": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            "Evidence": con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "Claims": con.execute("SELECT COUNT(*) FROM significance_claims").fetchone()[0],
            "Scores": con.execute("SELECT COUNT(*) FROM scores").fetchone()[0],
            "Review queue": con.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='OPEN'"
            ).fetchone()[0],
        }
    table = Table(title="calibre-research")
    table.add_column("Item")
    table.add_column("Count", justify="right")
    for key, value in values.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def research(
    depth: Annotated[str, typer.Option("--depth", help="metadata|significance|full")] = "full",
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    budget: Annotated[float | None, typer.Option("--budget")] = None,
    config: ConfigOpt = None,
):
    if depth not in {"metadata", "significance", "full"}:
        raise typer.BadParameter("depth must be metadata, significance, or full")
    cfg = _load(config)
    db = Database(cfg.database_path)
    db.initialize()
    effective_budget = budget if budget is not None else cfg.research.max_cost_per_run_usd

    with db.connect() as con:
        query = """
            SELECT w.id, w.canonical_title, w.canonical_author
            FROM works w
            WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.work_id=w.id)
              AND NOT EXISTS (SELECT 1 FROM significance_claims s WHERE s.work_id=w.id)
            ORDER BY w.id
        """
        params: tuple = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = con.execute(query, params).fetchall()

    console.print(
        f"Research provider [bold]{cfg.research.provider}[/bold]; "
        f"depth={depth}; budget=${effective_budget:.2f}; candidates={len(rows)}"
    )
    console.print("Significance provider integration is intentionally stubbed.")


@app.command()
def review(config: ConfigOpt = None):
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    with db.connect() as con:
        rows = con.execute(
            """
            SELECT r.id, w.canonical_title, w.canonical_author, r.kind, r.reason
            FROM review_queue r JOIN works w ON w.id=r.work_id
            WHERE r.status='OPEN' ORDER BY r.id
            """
        ).fetchall()
    if not rows:
        console.print("No open review items.")
        return
    table = Table(title="Review queue")
    for col in ["ID", "Title", "Author", "Kind", "Reason"]:
        table.add_column(col)
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["canonical_title"],
            row["canonical_author"],
            row["kind"],
            row["reason"],
        )
    console.print(table)


@app.command()
def explain(query: str, config: ConfigOpt = None):
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    with db.connect() as con:
        row = con.execute(
            """
            SELECT w.id, w.canonical_title, w.canonical_author, s.*
            FROM works w LEFT JOIN scores s ON s.work_id=w.id
            WHERE lower(w.canonical_title) LIKE lower(?)
            ORDER BY s.scored_at DESC LIMIT 1
            """,
            (f"%{query}%",),
        ).fetchone()
    if not row:
        raise typer.Exit(code=_print_error("No matching work"))
    console.print(f"[bold]{row['canonical_title']}[/bold] — {row['canonical_author']}")
    if row["total"] is None:
        console.print("Not scored yet.")
        return
    console.print(f"Score: {row['total']} (rubric {row['rubric_version']})")
    console.print(f"Confidence: {row['confidence']}")
    console.print(f"Why read: {row['why_read'] or '-'}")
    console.print(json.dumps(json.loads(row["components_json"]), indent=2))


def _print_metadata_header(edition: dict) -> None:
    console.print()
    console.print(f"[bold]{edition['title']}[/bold] — {edition['author']}")
    console.print(f"  Calibre id: {edition['calibre_book_id']}")


def _display_value(value) -> str:
    if value is None or value == "":
        return "[dim]<missing>[/dim]"
    if isinstance(value, str) and value.startswith("0101-01-01"):
        return "[dim]<missing>[/dim]"
    return str(value)


def _print_error(message: str) -> int:
    console.print(f"[red]error:[/red] {message}")
    return 1


if __name__ == "__main__":
    app()
