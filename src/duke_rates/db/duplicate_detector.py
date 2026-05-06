"""
Duplicate document detection using checksums (content_hash field).

This module provides utilities to:
1. Calculate SHA256 checksums for PDF files
2. Check if a checksum already exists in the database
3. Find duplicate documents by content hash
4. Skip re-downloading files that are already in the database
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional


def calculate_file_checksum(file_path: Path | str, algorithm: str = "sha256") -> str:
    """
    Calculate SHA256 (or other) checksum of a file.

    Args:
        file_path: Path to file to hash
        algorithm: Hash algorithm ('sha256', 'md5', etc.)

    Returns:
        Hex digest of file contents
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    hasher = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def checksum_exists_in_ncuc_discovery(
    conn: sqlite3.Connection,
    content_hash: str,
) -> bool:
    """
    Check if a content hash already exists in ncuc_discovery_records table.

    Args:
        conn: Database connection
        content_hash: SHA256 hash to check

    Returns:
        True if hash exists, False otherwise
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1 FROM ncuc_discovery_records
        WHERE content_hash = ? AND fetch_status = 'success'
        LIMIT 1
        """,
        (content_hash,),
    )
    return cursor.fetchone() is not None


def checksum_exists_in_historical_docs(
    conn: sqlite3.Connection,
    content_hash: str,
) -> bool:
    """
    Check if a content hash already exists in historical_documents table.

    Args:
        conn: Database connection
        content_hash: SHA256 hash to check

    Returns:
        True if hash exists, False otherwise
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1 FROM historical_documents
        WHERE content_hash = ? AND local_path IS NOT NULL
        LIMIT 1
        """,
        (content_hash,),
    )
    return cursor.fetchone() is not None


def find_duplicate_by_checksum(
    conn: sqlite3.Connection,
    content_hash: str,
) -> Optional[dict]:
    """
    Find an existing document with the same content hash.

    Returns dict with 'source' and 'record' keys, or None if not found.
    """
    # Check ncuc_discovery_records first
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 'ncuc_discovery' as source, id, local_path, filing_title, discovered_url
        FROM ncuc_discovery_records
        WHERE content_hash = ? AND fetch_status = 'success'
        LIMIT 1
        """,
        (content_hash,),
    )
    row = cursor.fetchone()
    if row:
        return {
            "source": row[0],
            "id": row[1],
            "local_path": row[2],
            "title": row[3],
            "url": row[4],
        }

    # Check historical_documents
    cursor.execute(
        """
        SELECT 'historical_documents' as source, id, local_path, title, canonical_url
        FROM historical_documents
        WHERE content_hash = ? AND local_path IS NOT NULL
        LIMIT 1
        """,
        (content_hash,),
    )
    row = cursor.fetchone()
    if row:
        return {
            "source": row[0],
            "id": row[1],
            "local_path": row[2],
            "title": row[3],
            "url": row[4],
        }

    return None


def get_all_checksums_in_database(
    conn: sqlite3.Connection,
) -> set[str]:
    """
    Get all content_hash values in both discovery and historical tables.

    Returns:
        Set of all non-null checksums currently in database
    """
    cursor = conn.cursor()
    checksums = set()

    # Get from ncuc_discovery_records
    cursor.execute(
        "SELECT DISTINCT content_hash FROM ncuc_discovery_records WHERE content_hash IS NOT NULL"
    )
    for row in cursor.fetchall():
        checksums.add(row[0])

    # Get from historical_documents
    cursor.execute(
        "SELECT DISTINCT content_hash FROM historical_documents WHERE content_hash IS NOT NULL"
    )
    for row in cursor.fetchall():
        checksums.add(row[0])

    return checksums


def batch_check_checksums(
    conn: sqlite3.Connection,
    hashes: list[str],
) -> dict[str, bool]:
    """
    Check which hashes already exist in database (batch operation).

    Args:
        conn: Database connection
        hashes: List of SHA256 checksums to check

    Returns:
        Dict mapping hash -> exists_in_db (True/False)
    """
    existing = get_all_checksums_in_database(conn)
    return {h: h in existing for h in hashes}


def should_skip_download(
    conn: sqlite3.Connection,
    file_path: Path | str,
) -> tuple[bool, Optional[str]]:
    """
    Determine if a file should be skipped based on checksum comparison.

    Returns:
        (should_skip, reason)
        If should_skip is True, reason explains why (e.g., "Duplicate of existing doc ID 12345")
    """
    file_path = Path(file_path)

    if not file_path.exists():
        return False, None  # Can't check hash if file doesn't exist

    checksum = calculate_file_checksum(file_path)
    duplicate = find_duplicate_by_checksum(conn, checksum)

    if duplicate:
        return True, f"Duplicate of {duplicate['source']} ID {duplicate['id']} ({duplicate['title'][:50]}...)"

    return False, None


def update_checksum_in_ncuc_discovery(
    conn: sqlite3.Connection,
    record_id: int,
    file_path: Path | str,
) -> None:
    """
    Calculate and store checksum for a ncuc_discovery_records entry.
    """
    checksum = calculate_file_checksum(file_path)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE ncuc_discovery_records SET content_hash = ? WHERE id = ?",
        (checksum, record_id),
    )
    conn.commit()


def update_checksum_in_historical_docs(
    conn: sqlite3.Connection,
    doc_id: int,
    file_path: Path | str,
) -> None:
    """
    Calculate and store checksum for a historical_documents entry.
    """
    checksum = calculate_file_checksum(file_path)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE historical_documents SET content_hash = ? WHERE id = ?",
        (checksum, doc_id),
    )
    conn.commit()
