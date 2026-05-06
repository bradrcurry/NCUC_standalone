"""
Smart NCUC document downloader with duplicate detection.

This module provides intelligent download management:
1. Checks for duplicates before downloading
2. Calculates checksums after downloading
3. Deduplicates downloaded files
4. Tracks which documents have been downloaded

Usage:
    downloader = SmartNcucDownloader(conn, download_dir, max_retries=3)

    result = downloader.download_with_dedup(
        document_url="https://...",
        document_title="E-2 Sub 1354 Filing",
        docket_number="E-2 Sub 1354",
    )

    if result.skipped:
        print(f"Skipped: {result.reason}")
    else:
        print(f"Downloaded: {result.file_path} ({result.file_size} bytes)")
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from duke_rates.db.duplicate_detector import (
    calculate_file_checksum,
    find_duplicate_by_checksum,
    update_checksum_in_ncuc_discovery,
)

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    """Result of a download attempt."""

    success: bool
    skipped: bool = False
    reason: Optional[str] = None
    file_path: Optional[Path] = None
    file_size: Optional[int] = None
    content_hash: Optional[str] = None
    duplicate_of: Optional[dict] = None

    def __str__(self):
        if self.skipped:
            return f"[SKIPPED] {self.reason}"
        elif self.success:
            return f"[DOWNLOADED] {self.file_path.name} ({self.file_size} bytes)"
        else:
            return f"[FAILED] {self.reason}"


class SmartNcucDownloader:
    """Download manager with duplicate detection and checksum verification."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        download_dir: Path | str,
        max_retries: int = 3,
    ):
        """
        Initialize the downloader.

        Args:
            conn: Database connection
            download_dir: Directory to save downloads
            max_retries: Number of retry attempts on failure
        """
        self.conn = conn
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self._download_count = 0
        self._skipped_count = 0
        self._failed_count = 0

    def should_download(
        self,
        document_title: str,
        document_url: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Determine if a document should be downloaded.

        Returns:
            (should_download, reason_if_skip)
        """
        # Check if already exists in database
        cursor = self.conn.cursor()

        # Check ncuc_discovery_records
        cursor.execute(
            """
            SELECT id, local_path FROM ncuc_discovery_records
            WHERE discovered_url = ? AND fetch_status = 'success'
            LIMIT 1
            """,
            (document_url,),
        )
        row = cursor.fetchone()
        if row:
            doc_id, local_path = row
            if Path(local_path).exists():
                return False, f"Already downloaded (ncuc_discovery ID {doc_id})"
            return True, None

        # Check historical_documents
        cursor.execute(
            """
            SELECT id, local_path FROM historical_documents
            WHERE canonical_url = ? AND local_path IS NOT NULL
            LIMIT 1
            """,
            (document_url,),
        )
        row = cursor.fetchone()
        if row:
            doc_id, local_path = row
            if Path(local_path).exists():
                return False, f"Already downloaded (historical_documents ID {doc_id})"
            return True, None

        return True, None

    def prepare_filename(
        self,
        docket_number: str,
        document_title: str,
    ) -> str:
        """
        Generate a safe filename for a document.

        Example:
            "E-2_Sub_1354_Filing_Jan_2024" -> "e2_sub_1354_filing_jan_2024.pdf"
        """
        import re
        from datetime import datetime

        # Use title + docket number
        base = f"{docket_number} {document_title}"

        # Remove/replace unsafe characters
        safe = re.sub(r"[^\w\s\-]", "", base)
        safe = re.sub(r"\s+", "_", safe).lower()
        safe = re.sub(r"_+", "_", safe).strip("_")

        # Truncate to reasonable length
        safe = safe[:60]

        return f"{safe}.pdf"

    def register_download(
        self,
        discovery_record_id: int,
        file_path: Path,
        content_hash: str,
    ) -> None:
        """Update the database with download completion info."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE ncuc_discovery_records
            SET local_path = ?, content_hash = ?, fetch_status = 'success', fetched_at = datetime('now')
            WHERE id = ?
            """,
            (str(file_path), content_hash, discovery_record_id),
        )
        self.conn.commit()

    def register_duplicate_skip(
        self,
        discovery_record_id: int,
        content_hash: str | None,
        duplicate_of: dict | None,
    ) -> None:
        """Mark a discovery row as skipped because the downloaded bytes were duplicate."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE ncuc_discovery_records
            SET fetch_status = 'skipped_duplicate',
                content_hash = ?,
                error_detail = ?,
                fetched_at = datetime('now')
            WHERE id = ?
            """,
            (
                content_hash,
                (
                    f"duplicate_of:{duplicate_of['source']}:{duplicate_of['id']}"
                    if duplicate_of else "duplicate_of:unknown"
                ),
                discovery_record_id,
            ),
        )
        self.conn.commit()

    def check_for_duplicates(self, file_path: Path) -> Optional[dict]:
        """
        After downloading, check if this file is a duplicate of another.

        Returns:
            dict with duplicate info if found, None otherwise
        """
        if not file_path.exists():
            return None

        try:
            checksum = calculate_file_checksum(file_path)
            return find_duplicate_by_checksum(self.conn, checksum)
        except Exception as e:
            logger.warning(f"Failed to check for duplicates: {e}")
            return None

    def download_with_dedup(
        self,
        document_url: str,
        document_title: str,
        docket_number: str,
        discovery_record_id: Optional[int] = None,
        download_func=None,
    ) -> DownloadResult:
        """
        Download a document with automatic deduplication checking.

        Args:
            document_url: URL to download from
            document_title: Document title (for filename)
            docket_number: Docket number (for organization)
            discovery_record_id: If provided, updates this ncuc_discovery_records row
            download_func: Callable that takes (url, dest_path) and downloads the file
                          Return value should be file size in bytes

        Returns:
            DownloadResult with status info
        """
        # Check if we should even download
        should_dl, skip_reason = self.should_download(document_title, document_url)
        if not should_dl:
            self._skipped_count += 1
            return DownloadResult(
                success=False,
                skipped=True,
                reason=skip_reason,
            )

        # Prepare filename
        filename = self.prepare_filename(docket_number, document_title)
        file_path = self.download_dir / docket_number.replace(" ", "_") / filename

        # Attempt download with retries
        file_size = None
        for attempt in range(self.max_retries):
            try:
                if download_func is None:
                    raise ValueError("download_func must be provided")

                logger.info(f"[DOWNLOAD {attempt+1}/{self.max_retries}] {document_title}")
                file_size = download_func(document_url, file_path)
                break

            except Exception as e:
                logger.warning(f"Download attempt {attempt+1} failed: {e}")
                if attempt == self.max_retries - 1:
                    self._failed_count += 1
                    return DownloadResult(
                        success=False,
                        reason=f"Download failed after {self.max_retries} attempts: {e}",
                    )

        # Calculate checksum
        try:
            content_hash = calculate_file_checksum(file_path)
        except Exception as e:
            logger.warning(f"Failed to calculate checksum: {e}")
            content_hash = None

        # Check for duplicates
        duplicate = self.check_for_duplicates(file_path)
        if duplicate:
            logger.warning(
                f"File is a duplicate of {duplicate['source']} ID {duplicate['id']}"
            )
            try:
                file_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to remove duplicate download {file_path}: {e}")
            if discovery_record_id:
                try:
                    self.register_duplicate_skip(discovery_record_id, content_hash, duplicate)
                except Exception as e:
                    logger.warning(f"Failed to mark duplicate skip: {e}")
            self._skipped_count += 1
            return DownloadResult(
                success=False,
                skipped=True,
                reason=f"Duplicate of {duplicate['source']} ID {duplicate['id']}",
                file_size=file_size,
                content_hash=content_hash,
                duplicate_of=duplicate,
            )

        # Register in database
        if discovery_record_id:
            try:
                self.register_download(discovery_record_id, file_path, content_hash)
            except Exception as e:
                logger.warning(f"Failed to register download: {e}")

        self._download_count += 1
        return DownloadResult(
            success=True,
            file_path=file_path,
            file_size=file_size,
            content_hash=content_hash,
            duplicate_of=duplicate,
        )

    def get_stats(self) -> dict:
        """Return download statistics."""
        return {
            "downloaded": self._download_count,
            "skipped": self._skipped_count,
            "failed": self._failed_count,
            "total": self._download_count + self._skipped_count + self._failed_count,
        }

    def print_summary(self) -> None:
        """Print download statistics."""
        stats = self.get_stats()
        print(f"\n{'='*60}")
        print(f"Download Summary:")
        print(f"  Downloaded: {stats['downloaded']}")
        print(f"  Skipped:    {stats['skipped']}")
        print(f"  Failed:     {stats['failed']}")
        print(f"  Total:      {stats['total']}")
        print(f"{'='*60}\n")
