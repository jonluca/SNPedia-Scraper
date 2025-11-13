import requests
import sqlite3
import time
import json
from datetime import datetime
import os
import sys
import threading
from contextlib import contextmanager
from typing import Optional, Tuple

# --- Path Setup ---
# Get the absolute path to the project root directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# Define the absolute path for the database
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, 'snpedia.db')
ERROR_LOG_PATH = os.path.join(PROJECT_ROOT, 'scraper_errors.log')


class DatabaseConnectionPool:
    """Manages a single persistent database connection throughout the scraper's lifetime."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = None

    def get_connection(self) -> sqlite3.Connection:
        """Get or create the persistent database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._conn

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self):
        """Context manager for database transactions."""
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


class SNPediaScraper:
    def __init__(self, db_path=DEFAULT_DB_PATH, status_callback=None, log_callback=None):
        self.db_path = db_path
        self.api_url = "https://bots.snpedia.com/api.php"
        self.total_snps = 110000  # From README
        self.total_genos = 104887  # From https://bots.snpedia.com/index.php/Category:Is_a_genotype

        # Database connection pool
        self.db_pool = DatabaseConnectionPool(db_path)

        # Callbacks for UI updates
        self.status_callback = status_callback
        self.log_callback = log_callback

        # State management
        self.running = False
        self.paused = False
        self._thread = None

        self._create_tables()
        self._init_error_log()

    def _init_error_log(self):
        """Initialize or append to error log file."""
        if not os.path.exists(ERROR_LOG_PATH):
            with open(ERROR_LOG_PATH, 'w') as f:
                f.write("# SNPedia Scraper Error Log\n")
                f.write(f"# Started: {datetime.now()}\n")
                f.write("# Format: timestamp | rsid | error_type | error_message\n")
                f.write("-" * 80 + "\n")
    
    def _log_error(self, rsid, error_type, error_message):
        """Log an error to the error file."""
        with open(ERROR_LOG_PATH, 'a') as f:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"{timestamp} | {rsid} | {error_type} | {error_message}\n")
            f.flush()  # Ensure it's written immediately

    def _create_tables(self):
        """Create database tables if they don't exist."""
        with self.db_pool.transaction() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS snps (
                    rsid TEXT PRIMARY KEY,
                    content TEXT,
                    scraped_at TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS genotypes (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    scraped_at TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS progress (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

    def start(self):
        if not self.running:
            self.running = True
            self.paused = False
            self._thread = threading.Thread(target=self._scrape_loop)
            self._thread.start()
            if self.log_callback: self.log_callback("Scraper started.")

    def pause(self):
        self.paused = True
        if self.log_callback: self.log_callback("Scraper paused.")

    def resume(self):
        self.paused = False
        if self.log_callback: self.log_callback("Scraper resumed.")

    def stop(self):
        self.running = False
        self.db_pool.close()
        if self.log_callback: self.log_callback("Scraper stopping...")

    def get_current_progress(self) -> Tuple[int, int]:
        """Get current progress for SNPs."""
        count = int(self.get_progress('snp_count') or 0)
        return count, self.total_snps

    def get_genotype_progress(self) -> Tuple[int, int]:
        """Get current progress for genotypes."""
        count = int(self.get_progress('genotype_count') or 0)
        return count, self.total_genos

    def get_combined_progress(self) -> Tuple[int, int]:
        """Get combined progress for SNPs and genotypes."""
        snp_count = int(self.get_progress('snp_count') or 0)
        genotype_count = int(self.get_progress('genotype_count') or 0)
        return snp_count + genotype_count, self.total_snps + self.total_genos

    def _fetch_batch_content(self, page_titles: list) -> dict:
        """
        Fetch wiki content for multiple page titles in a single API call.

        Args:
            page_titles: List of page titles to fetch

        Returns:
            Dictionary mapping page titles to their content (or None if not found)
        """
        if not page_titles:
            return {}

        # MediaWiki API accepts pipe-delimited titles
        titles_param = '|'.join(page_titles)

        params_content = {
            'action': 'query',
            'prop': 'revisions',
            'rvprop': 'content',
            'format': 'json',
            'titles': titles_param
        }

        content_response = requests.get(self.api_url, params=params_content, headers={
            'User-Agent': 'SNPediaScraper/1.0 (Educational Research; https://github.com/jaykobdetar/SNPedia-Scraper; simyc4982@email.com) Mozilla/5.0 compatible'
        })

        data_content = content_response.json()

        results = {}

        if 'query' in data_content and 'pages' in data_content['query']:
            for page_id, page_data in data_content['query']['pages'].items():
                if page_id == '-1':  # Page doesn't exist
                    continue

                title = page_data.get('title', '').replace(' ', '_')

                if 'revisions' in page_data and len(page_data['revisions']) > 0:
                    results[title] = page_data['revisions'][0]['*']
                else:
                    results[title] = None

        return results

    def _save_entries(self, table: str, id_column: str, entries: list):
        """
        Save multiple entries to the database in a single transaction.

        Args:
            table: Database table name
            id_column: ID column name
            entries: List of tuples (identifier, content)
        """
        if not entries:
            return

        timestamp = datetime.now()
        with self.db_pool.transaction() as conn:
            conn.executemany(
                f'INSERT INTO {table} ({id_column}, content, scraped_at) VALUES (?, ?, ?)',
                [(identifier, content, timestamp) for identifier, content in entries]
            )

    def _save_entry(self, table: str, id_column: str, identifier: str, content: str):
        """Save a single entry to the database."""
        self._save_entries(table, id_column, [(identifier, content)])

    def _scrape_category(
        self,
        category: str,
        table: str,
        id_column: str,
        count_key: str,
        continue_key: str,
        total_count: int,
        item_name: str,
        batch_size: int = 50
    ) -> int:
        """
        Generic method to scrape a category from SNPedia using batch fetching.

        Args:
            category: The category to scrape (e.g., 'Category:Is_a_snp')
            table: Database table name (e.g., 'snps')
            id_column: ID column name (e.g., 'rsid')
            count_key: Progress key for count (e.g., 'snp_count')
            continue_key: Progress key for continuation (e.g., 'cmcontinue_snp')
            total_count: Total expected count
            item_name: Display name for items (e.g., 'SNP')
            batch_size: Number of pages to fetch in a single API call (default: 50)

        Returns:
            Final count of items scraped
        """
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': category,
            'cmlimit': 500,
            'format': 'json'
        }

        last_continue = self.get_progress(continue_key)
        if last_continue:
            params['cmcontinue'] = last_continue

        count = int(self.get_progress(count_key) or 0)

        while self.running:
            if self.paused:
                time.sleep(1)
                continue

            try:
                r = requests.get(self.api_url, params=params)
                r.raise_for_status()
                data = r.json()

                # Collect all page titles from this batch
                all_identifiers = []
                for page in data['query']['categorymembers']:
                    identifier = page['title'].replace(' ', '_')
                    all_identifiers.append(identifier)

                # Batch check which items already exist
                existing_identifiers = self._batch_check_exists(table, id_column, all_identifiers)

                # Filter out already scraped items
                identifiers_to_fetch = [
                    identifier for identifier in all_identifiers
                    if identifier not in existing_identifiers
                ]

                # Skip already scraped items
                skipped_count = len(existing_identifiers)
                if skipped_count > 0 and self.log_callback:
                    self.log_callback(f"Skipping {skipped_count} already scraped {item_name}s")

                # Process in batches
                for i in range(0, len(identifiers_to_fetch), batch_size):
                    if not self.running:
                        break

                    while self.paused:
                        if not self.running:
                            break
                        time.sleep(1)

                    batch = identifiers_to_fetch[i:i + batch_size]

                    try:
                        # Fetch content for all titles in this batch
                        batch_results = self._fetch_batch_content(batch)

                        # Collect valid entries to save
                        entries_to_save = []
                        for identifier in batch:
                            if identifier not in batch_results:
                                if self.log_callback:
                                    self.log_callback(f"Page not found for {identifier}. Skipping.")
                                continue

                            content = batch_results[identifier]
                            if content is None:
                                if self.log_callback:
                                    self.log_callback(f"No content for {identifier}. Skipping.")
                                continue

                            entries_to_save.append((identifier, content))

                        # Save all entries in a single transaction
                        if entries_to_save:
                            self._save_entries(table, id_column, entries_to_save)

                            # Update progress for all saved items
                            for identifier, _ in entries_to_save:
                                count += 1
                                if self.status_callback:
                                    self.status_callback(count, total_count, identifier)

                            self.save_progress(count_key, str(count))

                            if self.log_callback:
                                self.log_callback(f"Scraped batch of {len(entries_to_save)} {item_name}s. Total: {count}")

                    except Exception as e:
                        # On error, fall back to individual fetching for this batch
                        if self.log_callback:
                            self.log_callback(f"Batch fetch error: {e}. Retrying individual items in 30 seconds...")

                        error_type = "502_ERROR" if "502" in str(e) else "BATCH_ERROR"
                        for identifier in batch:
                            self._log_error(identifier, error_type, str(e))

                        time.sleep(30)
                        continue

                    # Rate limiting between batches
                    time.sleep(3)

                if 'continue' in data and data['continue']:
                    params['cmcontinue'] = data['continue']['cmcontinue']
                    self.save_progress(continue_key, params['cmcontinue'])
                else:
                    if self.log_callback:
                        self.log_callback(f"Scraping complete: Reached end of {item_name} list.")
                    break

                time.sleep(2)

            except KeyboardInterrupt:
                self.stop()
                print("\n\nPausing... Progress saved. Run again to resume.")
                break
            except Exception as e:
                if self.log_callback:
                    self.log_callback(f"Error: {e}. Retrying in 30 seconds...")
                time.sleep(30)

        return count

    def _scrape_loop(self):
        """Main scraping loop that handles both SNPs and genotypes."""
        try:
            # Scrape SNPs
            if self.log_callback:
                self.log_callback("Starting genotype scraping...")

            self._scrape_category(
                category='Category:Is_a_genotype',
                table='genotypes',
                id_column='id',
                count_key='genotype_count',
                continue_key='cmcontinue_genotype',
                total_count=self.total_genos,
                item_name='genotype'
            )

            if self.log_callback:
                self.log_callback("Starting SNP scraping...")

            self._scrape_category(
                category='Category:Is_a_snp',
                table='snps',
                id_column='rsid',
                count_key='snp_count',
                continue_key='cmcontinue_snp',
                total_count=self.total_snps,
                item_name='SNP'
            )

        finally:
            self.running = False
            if self.log_callback:
                self.log_callback("Scraper stopped.")

    def _already_exists(self, table: str, id_column: str, identifier: str) -> bool:
        """Check if an entry already exists in the database."""
        conn = self.db_pool.get_connection()
        cursor = conn.execute(f'SELECT 1 FROM {table} WHERE {id_column} = ?', (identifier,))
        return cursor.fetchone() is not None

    def _batch_check_exists(self, table: str, id_column: str, identifiers: list) -> set:
        """
        Check which entries already exist in the database using a single query.

        Args:
            table: Database table name
            id_column: ID column name
            identifiers: List of identifiers to check

        Returns:
            Set of identifiers that already exist in the database
        """
        if not identifiers:
            return set()

        conn = self.db_pool.get_connection()

        # Use placeholders for parameterized query
        placeholders = ','.join('?' * len(identifiers))
        query = f'SELECT {id_column} FROM {table} WHERE {id_column} IN ({placeholders})'

        cursor = conn.execute(query, identifiers)
        existing = {row[0] for row in cursor.fetchall()}

        return existing

    def already_scraped(self, rsid: str) -> bool:
        """Check if a SNP has already been scraped."""
        return self._already_exists('snps', 'rsid', rsid)

    def genotype_already_scraped(self, genotype_id: str) -> bool:
        """Check if a genotype has already been scraped."""
        return self._already_exists('genotypes', 'id', genotype_id)

    def save_progress(self, key: str, value: str):
        """Save progress to the database."""
        with self.db_pool.transaction() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO progress (key, value) VALUES (?, ?)',
                (key, str(value))
            )

    def get_progress(self, key: str) -> Optional[str]:
        """Get progress value from the database."""
        conn = self.db_pool.get_connection()
        cursor = conn.execute('SELECT value FROM progress WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row[0] if row else None


if __name__ == "__main__":
    def console_status_callback(count, total, current_snp):
        # Simple progress bar for the console
        percent = (100 * (count / float(total)))
        bar_length = 50
        filled_length = int(bar_length * count // total)
        bar = '█' * filled_length + '-' * (bar_length - filled_length)
        sys.stdout.write(f'\rProgress: |{bar}| {percent:.1f}% Complete ({current_snp})')
        sys.stdout.flush()

    def console_log_callback(message):
        sys.stdout.write(f'\n{datetime.now().strftime("%H:%M:%S")} - {message}\n')
        sys.stdout.flush()

    print("=== SNPedia Scraper (CLI) ===")
    print("This will take a couple hours to complete.")
    print("Press Ctrl+C anytime to pause (progress is saved).")
    print("="*30)

    scraper = SNPediaScraper(
        status_callback=console_status_callback, 
        log_callback=console_log_callback
    )
    
    # Initial progress display
    initial_count, total_snps = scraper.get_current_progress()
    console_status_callback(initial_count, total_snps, "Ready")

    scraper.start()

    try:
        while scraper.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nCaught Ctrl+C. Shutting down gracefully...")
        scraper.stop()
