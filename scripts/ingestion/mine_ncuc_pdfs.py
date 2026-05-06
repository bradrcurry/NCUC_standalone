#!/usr/bin/env python3
"""
Mine downloaded NCUC PDFs for historical tariff documents.
Expects PYTHONPATH to include src/ directory.
"""
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.models.ncuc import NcucFetchStatus
from duke_rates.historical.ncuc.importer import NcucPipelineImporter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    settings = get_settings()
    db_path = Path(settings.database_path) if settings.database_path else Path("data/historical_tariffs.db")
    repo = Repository(db_path)
    importer = NcucPipelineImporter(settings=settings, repository=repo)
    
    # Get all successfully downloaded records
    downloaded = repo.list_ncuc_discovery_records(fetch_status=NcucFetchStatus.SUCCESS.value)
    
    if not downloaded:
        logger.warning("No successfully downloaded records found in database")
        return
    
    logger.info(f"Found {len(downloaded)} downloaded NCUC records to mine")
    
    # Process with progress tracking
    total = len(downloaded)
    completed = 0
    created_docs = 0
    failed = 0
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        for record in downloaded:
            future = executor.submit(importer.mine_discovery_record_spans, record)
            futures[future] = record
        
        for future in as_completed(futures):
            completed += 1
            record = futures[future]
            try:
                span_ids = future.result()
                created_docs += len(span_ids) if span_ids else 0
                if completed % 50 == 0:
                    logger.info(f"Progress: {completed}/{total} ({100*completed//total}%) - "
                               f"Created {created_docs} historical documents")
            except Exception as e:
                failed += 1
                logger.error(f"Record {record.id} ({record.docket_number}): {e}")
    
    logger.info(f"Mining complete: {completed} records processed, {created_docs} historical documents created, {failed} failures")

if __name__ == "__main__":
    main()

