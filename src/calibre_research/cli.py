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
from .goodreads import (
    GoodreadsImportError,
    import_goodreads_csv,
    reconcile_goodreads_works,
)
from .metadata import build_proposals, candidate_from_normalized, classify_unresolved
from .providers import ProviderError, make_metadata_provider, metadata_query_key
from .scoring import assessed_maximum, load_rubric, score_significance
from .significance import (
    CACHED_METADATA_CLAIM_CATEGORIES,
    CACHED_METADATA_FACT_FIELDS,
    build_about,
    build_significance_explanation,
    evidence_coverage,
    facts_by_name,
    research_from_cached_metadata,
)
from .wikidata import (
    WIKIDATA_FACT_FIELDS,
    WikidataAwardProvider,
    combine_results,
)

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
    retry_errors: Annotated[
        bool,
        typer.Option(
            "--retry-errors",
            help="Retry editions with an open provider error.",
        ),
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Ignore cached provider lookups.")
    ] = False,
    config: ConfigOpt = None,
):
    """Research metadata and store non-destructive repair proposals."""
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()

    provider_names = [provider] if provider else cfg.metadata.providers
    if not provider_names:
        raise typer.Exit(code=_print_error("No metadata providers are configured"))
    if retry_errors and refresh:
        raise typer.Exit(code=_print_error("--retry-errors cannot be combined with --refresh"))

    providers = []
    for provider_name in provider_names:
        try:
            provider_settings = getattr(cfg.metadata, provider_name, None)
            if provider_settings is None:
                raise ValueError(
                    f"metadata provider {provider_name!r} has no configuration section"
                )
            providers.append(make_metadata_provider(provider_name, provider_settings.model_dump()))
        except (AttributeError, ProviderError, ValueError) as exc:
            raise typer.Exit(code=_print_error(str(exc))) from exc

    effective_limit = limit
    if "openlibrary" in provider_names:
        openlibrary_settings = cfg.metadata.openlibrary
        if openlibrary_settings is None:
            raise typer.Exit(
                code=_print_error("metadata provider 'openlibrary' has no configuration section")
            )
        max_books = openlibrary_settings.max_books_per_run
        if effective_limit is None:
            effective_limit = max_books
        elif effective_limit > max_books:
            raise typer.Exit(
                code=_print_error(
                    f"Open Library runs are capped at {max_books} books by configuration; "
                    "raise metadata.openlibrary.max_books_per_run explicitly if appropriate"
                )
            )

    rows = db.metadata_candidates(
        providers=provider_names,
        retry_errors=retry_errors,
        refresh=refresh,
        limit=effective_limit,
    )
    if not rows:
        if retry_errors:
            console.print("No editions with provider errors to retry.")
        else:
            console.print("No pending editions to research.")
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
        provider_errors: list[tuple[str, str]] = []
        completed_providers: set[str] = set()
        error_providers = (
            db.open_metadata_error_providers(edition_id=edition["edition_id"])
            if retry_errors
            else set()
        )

        for metadata_provider in providers:
            provider_name = metadata_provider.name
            cached = None
            if cfg.metadata.reuse_cached_lookups and not refresh:
                cached = db.cached_metadata_lookup(
                    edition_id=edition["edition_id"],
                    provider=provider_name,
                    query_key=query_key,
                )

            if cached is not None:
                cached_count += 1
                completed_providers.add(provider_name)
                if cached["status"] == "NO_MATCH":
                    continue
                candidate = candidate_from_normalized(cached["normalized"], raw=cached["raw"])
                lookup_id = cached["id"]
                used_cache = True
                break

            if retry_errors and provider_name not in error_providers:
                continue

            try:
                candidate = metadata_provider.lookup(
                    title=edition["title"],
                    author=edition["author"],
                    isbn=edition["isbn"],
                )
            except ProviderError as exc:
                provider_errors.append((provider_name, str(exc)))
                continue

            completed_providers.add(provider_name)
            if candidate is None:
                db.store_metadata_miss(
                    edition_id=edition["edition_id"],
                    provider=provider_name,
                    query_key=query_key,
                )
                continue

            lookup_id = db.store_metadata_lookup(
                edition_id=edition["edition_id"], candidate=candidate
            )
            break

        _print_metadata_header(edition)

        if candidate is None:
            if provider_errors:
                failed += 1
                db.resolve_metadata_issues(edition_id=edition["edition_id"])
                for provider_name, error in provider_errors:
                    db.record_metadata_issue(
                        edition_id=edition["edition_id"],
                        classification="PROVIDER_ERROR",
                        provider=provider_name,
                        reason=error,
                    )
                    console.print(f"  [red]{provider_name} error:[/red] {error}")
                console.print(
                    "  [yellow]Metadata lookup incomplete because a provider failed; "
                    "this edition remains eligible for retry.[/yellow]"
                )
            elif completed_providers == set(provider_names):
                no_match += 1
                classification = classify_unresolved(edition)
                db.resolve_metadata_issues(edition_id=edition["edition_id"])
                db.record_metadata_issue(
                    edition_id=edition["edition_id"],
                    classification=classification,
                    provider=None,
                    reason="No configured metadata provider produced a confident match",
                )
                console.print(
                    f"  [yellow]No confident metadata match[/yellow] [dim]({classification})[/dim]"
                )
            else:
                db.resolve_metadata_issues(edition_id=edition["edition_id"])
                console.print(
                    "  [yellow]Provider errors were resolved; remaining providers are "
                    "pending for a normal metadata run.[/yellow]"
                )
            continue

        assert lookup_id is not None
        matched += 1
        db.resolve_metadata_issues(edition_id=edition["edition_id"])
        proposals = build_proposals(edition, candidate)
        db.replace_metadata_proposals(
            edition_id=edition["edition_id"],
            lookup_id=lookup_id,
            proposals=proposals,
        )
        proposal_count += len(proposals)

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
        f"Researched {len(rows)} editions: {matched} matched, {no_match} unresolved, "
        f"{failed} with provider errors; {proposal_count} proposals; "
        f"{cached_count} cached lookups"
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
            "Metadata issues": con.execute(
                "SELECT COUNT(*) FROM metadata_issues WHERE status='OPEN'"
            ).fetchone()[0],
            "Facts": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            "Evidence": con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "Claims": con.execute("SELECT COUNT(*) FROM significance_claims").fetchone()[0],
            "Award evidence": con.execute(
                "SELECT COUNT(*) FROM award_evidence WHERE status='RESEARCHED'"
            ).fetchone()[0],
            "Derived scores": con.execute("SELECT COUNT(*) FROM derived_scores").fetchone()[0],
            "Source imports": con.execute("SELECT COUNT(*) FROM source_imports").fetchone()[0],
            "Reading observations": con.execute(
                "SELECT COUNT(*) FROM reading_status_observations"
            ).fetchone()[0],
            "Owned editions": con.execute("SELECT COUNT(*) FROM owned_editions").fetchone()[0],
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


@app.command("import-goodreads")
def import_goodreads(
    csv_path: Annotated[Path, typer.Argument(help="Goodreads library export CSV.")],
    config: ConfigOpt = None,
):
    """Import Goodreads observations into the local evidence database."""
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    try:
        summary = import_goodreads_csv(db, csv_path)
    except (GoodreadsImportError, OSError) as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc
    if summary.already_imported:
        console.print(f"Goodreads export already imported; {summary.rows} rows unchanged.")
        return
    console.print(
        f"Imported {summary.rows} Goodreads rows: {summary.matched} matched, "
        f"{summary.created} works created, {summary.ambiguous} need review."
    )


@app.command("reconcile-goodreads")
def reconcile_goodreads(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Merge safe duplicates. Without this flag, only show the plan.",
        ),
    ] = False,
    config: ConfigOpt = None,
):
    """Reconcile Goodreads-created duplicates with Calibre-backed works."""
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    summary = reconcile_goodreads_works(db, apply=apply)

    if not summary.candidates:
        console.print("No Goodreads-created duplicates match Calibre-backed works.")
        return

    table = Table(title="Goodreads reconciliation")
    table.add_column("Goodreads work")
    table.add_column("Calibre work")
    table.add_column("Match")
    table.add_column("Action")
    for candidate in summary.candidates:
        source = f"{candidate.source_work_id}: {candidate.source_title}"
        target = (
            f"{candidate.target_work_id}: {candidate.target_title}"
            if candidate.target_work_id is not None
            else "multiple matches"
        )
        if candidate.blocked_reason:
            action = f"blocked: {candidate.blocked_reason}"
        elif apply:
            action = f"merged ({candidate.source_records} source records)"
        else:
            action = f"would merge ({candidate.source_records} source records)"
        table.add_row(source, target, candidate.match_method, action)
    console.print(table)
    if apply:
        console.print(
            f"Merged {summary.applied} duplicate works; {summary.blocked} require review."
        )
    else:
        console.print(
            f"Dry run: {summary.ready} duplicates can be merged; "
            f"{summary.blocked} require review. Re-run with --apply to merge safe matches."
        )


@app.command()
def research(
    depth: Annotated[str, typer.Option("--depth", help="metadata|significance|full")] = "full",
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    budget: Annotated[float | None, typer.Option("--budget")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Recompute existing scores.")] = False,
    config: ConfigOpt = None,
):
    if depth not in {"metadata", "significance", "full"}:
        raise typer.BadParameter("depth must be metadata, significance, or full")
    cfg = _load(config)
    db = Database(cfg.database_path)
    db.initialize()
    effective_budget = budget if budget is not None else cfg.research.max_cost_per_run_usd
    if depth == "metadata":
        console.print("Use [bold]calibre-research metadata[/bold] for metadata research.")
        return

    rubric = load_rubric(Path(cfg.rubric))
    rubric_version = str(rubric["version"])
    enabled_significance_providers = ["wikidata"] if cfg.research.wikidata.enabled else []
    rows = db.significance_candidates(
        rubric_version=rubric_version,
        refresh=refresh,
        limit=limit,
        retry_error_providers=enabled_significance_providers,
    )
    console.print(
        f"Significance research from cached metadata; rubric={rubric_version}; "
        f"cost=$0.00; candidates={len(rows)}"
    )
    if effective_budget < 0:
        raise typer.Exit(code=_print_error("Research budget cannot be negative"))
    if not rows:
        console.print("No works with current metadata matches require scoring.")
        return

    wikidata = (
        WikidataAwardProvider(cfg.research.wikidata) if cfg.research.wikidata.enabled else None
    )
    for row in rows:
        lookups = db.cached_metadata_matches_for_work(work_id=row["work_id"])
        result = research_from_cached_metadata(
            title=row["title"],
            author=row["author"],
            lookups=lookups,
        )
        managed_fact_fields = set(CACHED_METADATA_FACT_FIELDS)
        managed_claim_categories = set(CACHED_METADATA_CLAIM_CATEGORIES)
        managed_award_sources: set[str] = set()
        if wikidata is not None:
            try:
                wikidata_result = wikidata.research(title=row["title"], author=row["author"])
            except ProviderError as exc:
                console.print(f"[yellow]warning:[/yellow] {exc}")
                db.record_significance_provider_attempt(
                    work_id=row["work_id"],
                    provider="wikidata",
                    status="ERROR",
                    error=str(exc),
                )
                continue
            else:
                result = combine_results(result, wikidata_result)
                managed_fact_fields.update(WIKIDATA_FACT_FIELDS)
                managed_award_sources.add("Wikidata")
        db.store_research_result(
            work_id=row["work_id"],
            result=result,
            managed_fact_fields=managed_fact_fields,
            managed_claim_categories=managed_claim_categories,
            managed_award_sources=managed_award_sources,
        )
        if wikidata is not None:
            db.record_significance_provider_attempt(
                work_id=row["work_id"],
                provider="wikidata",
                status="MATCH" if wikidata_result.identity_confidence > 0 else "MISS",
            )
        awards = db.award_evidence_for_work(work_id=row["work_id"])
        scored_result = result.model_copy(update={"awards": awards})
        facts = facts_by_name(result)
        total, components = score_significance(rubric=rubric, awards=awards)
        confidence = evidence_coverage(rubric=rubric, result=scored_result, facts=facts)
        explanation = build_significance_explanation(awards=awards)
        about = build_about(facts=facts)
        db.store_derived_score(
            work_id=row["work_id"],
            score_kind="significance",
            rubric_version=rubric_version,
            total=total,
            confidence=confidence,
            components=components,
            explanation=explanation,
        )
        console.print()
        console.print(f"[bold]{row['title']}[/bold] — {row['author']}")
        assessed_max = assessed_maximum(components)
        if assessed_max:
            console.print(
                f"  provisional significance: {_display_score(total)}/"
                f"{_display_score(assessed_max)} assessed points"
            )
        else:
            console.print("  significance: not yet rated")
        console.print(f"  confidence: {confidence:.0%}")
        console.print(f"  significance evidence: {explanation}")
        console.print("  personal read score: unavailable")
        if about:
            console.print(f"  about: {about}")


@app.command("next")
def next_books(
    limit: Annotated[int, typer.Option("--limit", min=1)] = 25,
    config: ConfigOpt = None,
):
    """Rank works by significance; this is not yet a personal recommendation."""
    cfg = _load(config)
    db = _db(cfg.database_path)
    db.initialize()
    rubric = load_rubric(Path(cfg.rubric))
    rubric_version = str(rubric["version"])
    rows = db.ranked_scores(rubric_version=rubric_version, limit=limit)
    if not rows:
        console.print("No significance scores are available. Run significance research first.")
        return

    console.print(f"[bold]Significance ranking[/bold] [dim](rubric {rubric_version})[/dim]")
    for position, row in enumerate(rows, start=1):
        console.print()
        console.print(f"[bold]{position}. {row['title']}[/bold] — {row['author']}")
        assessed_max = assessed_maximum(row["components"])
        if assessed_max:
            rating = (
                f"{_display_score(row['total'])}/{_display_score(assessed_max)} assessed points"
            )
        else:
            rating = "not yet rated"
        console.print(f"   Significance: {rating}; confidence: {row['confidence']:.0%}")
        console.print(f"   {row['explanation']}")


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
            FROM works w LEFT JOIN derived_scores s
              ON s.work_id=w.id AND s.score_kind='significance'
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
    else:
        components = json.loads(row["components_json"])
        assessed_max = assessed_maximum(components)
        if assessed_max:
            console.print(
                f"Provisional significance: {_display_score(row['total'])}/"
                f"{_display_score(assessed_max)} assessed points "
                f"(rubric {row['rubric_version']})"
            )
        else:
            console.print(f"Significance: not yet rated (rubric {row['rubric_version']})")
        console.print(f"Confidence: {row['confidence']}")
        console.print(f"Significance evidence: {row['explanation'] or '-'}")
        console.print(json.dumps(components, indent=2))
    console.print("Personal read score: unavailable")

    with db.connect() as con:
        claims = con.execute(
            """
            SELECT category, claim, confidence
            FROM significance_claims
            WHERE work_id=? AND status='RESEARCHED'
            ORDER BY category, id
            """,
            (row["id"],),
        ).fetchall()
        evidence = con.execute(
            """
            SELECT DISTINCT source_name, source_url, citation_text
            FROM evidence
            WHERE work_id=?
            ORDER BY source_name, source_url
            """,
            (row["id"],),
        ).fetchall()
        awards = con.execute(
            """
            SELECT award_name, award_year, category, result, source_name,
                   source_identifier, source_url, confidence
            FROM award_evidence
            WHERE work_id=? AND status='RESEARCHED'
            ORDER BY award_year, award_name, result
            """,
            (row["id"],),
        ).fetchall()
        personal_history = con.execute(
            """
            SELECT si.source, si.imported_at, sr.source_record_id,
                   rso.status, rso.date_read,
                   ro.rating, ro.scale_max,
                   oe.format, oe.isbn, oe.owned_count
            FROM source_records sr
            JOIN source_imports si ON si.id=sr.import_id
            LEFT JOIN reading_status_observations rso ON rso.source_record_id=sr.id
            LEFT JOIN rating_observations ro ON ro.source_record_id=sr.id
            LEFT JOIN owned_editions oe ON oe.source_record_id=sr.id
            WHERE sr.work_id=?
            ORDER BY si.imported_at, sr.id
            """,
            (row["id"],),
        ).fetchall()
        tags = con.execute(
            """
            SELECT DISTINCT si.source, t.tag
            FROM tag_observations t
            JOIN source_records sr ON sr.id=t.source_record_id
            JOIN source_imports si ON si.id=sr.import_id
            WHERE t.work_id=?
            ORDER BY si.source, t.tag
            """,
            (row["id"],),
        ).fetchall()
    if personal_history or tags:
        console.print("[bold]Personal history[/bold]")
        for item in personal_history:
            details = []
            if item["status"]:
                details.append(f"status={item['status']}")
            if item["date_read"]:
                details.append(f"date read={item['date_read']}")
            if item["rating"] is not None:
                details.append(f"rating={item['rating']:g}/{item['scale_max']:g}")
            if item["owned_count"] is not None:
                owned = f"owned={item['owned_count']}"
                if item["format"]:
                    owned += f" {item['format']}"
                details.append(owned)
            if details:
                console.print(
                    f"  {item['source']}:{item['source_record_id']} "
                    f"(imported {item['imported_at']}): " + "; ".join(details)
                )
        if tags:
            grouped_tags: dict[str, list[str]] = {}
            for tag in tags:
                grouped_tags.setdefault(tag["source"], []).append(tag["tag"])
            for source, source_tags in grouped_tags.items():
                console.print(f"  {source} tags: {', '.join(source_tags)}")
    if awards:
        console.print("[bold]Award evidence[/bold]")
        for award in awards:
            year = f" ({award['award_year']})" if award["award_year"] else ""
            console.print(
                f"  {award['result']}: {award['award_name']}{year} "
                f"[{award['source_name']}:{award['source_identifier']}]"
            )
    if claims:
        console.print("[bold]Claims[/bold]")
        for claim in claims:
            console.print(f"  {claim['category']}: {claim['claim']} ({claim['confidence']:.2f})")
    if evidence:
        console.print("[bold]Sources (including unscored context)[/bold]")
        for item in evidence:
            label = item["source_name"] or item["citation_text"] or "Source"
            console.print(f"  {label}: {item['source_url'] or '-'}")


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


def _display_score(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def _print_error(message: str) -> int:
    console.print(f"[red]error:[/red] {message}")
    return 1


if __name__ == "__main__":
    app()
