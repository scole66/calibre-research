# calibre-research

CLI for **metadata repair** and **significance research** over a Calibre library.

This initial scaffold deliberately separates:

- bibliographic facts and editions
- significance claims and evidence
- scoring rubrics
- Calibre synchronization
- paid/LLM research providers

The goal is: **research once, retain evidence indefinitely, reinterpret cheaply.**

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

calibre-research init-db
calibre-research scan --library /path/to/Calibre\ Library
calibre-research stats
```

At this stage, `scan` uses Calibre's `calibredb list` command if available. Research provider integration is intentionally stubbed.
