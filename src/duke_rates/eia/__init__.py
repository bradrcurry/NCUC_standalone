"""EIA Open Data API v2 integration package.

Provides an HTTP client, endpoint extractors, data transformers, and SQLite
loaders for pulling EIA electricity data into the project database.

Modules
-------
client       — low-level EIA API v2 HTTP client with pagination and retry
endpoints    — endpoint-specific fetch functions (retail sales, generation, etc.)
transformers — normalize raw API records to canonical Python dicts
loaders      — idempotent SQLite upsert functions
references   — static lookup tables (census regions, market structure, RTO)
"""
