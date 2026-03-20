"""
Progress and Cookie Management Module

This module provides functionality for managing application progress, cookies,
and database operations.

Classes:
    ProgressManager: Handles progress tracking and persistence
    CookieManager: Manages HTTP session cookie operations
    DatabaseManager: Handles database interactions
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpcloak

LOGGER_NAME = "ChatGPT-API-Leakage"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="[%X]")
logger = logging.getLogger(LOGGER_NAME)


class ProgressManagerError(Exception):
    """Custom exception for ProgressManager class errors"""

    def __init__(self, message):
        super().__init__(message)


class ProgressManager:
    """
    Manages and persists progress information for long-running operations.

    Attributes:
        progress_file (Path): Path to the progress file

    Methods:
        save: Saves current progress
        load: Loads saved progress
    """

    def __init__(self, progress_file=".progress.txt"):
        """
        Initialize the ProgressManager with a specified progress file.

        Args:
            progress_file (str): The file where progress data is stored.
        """
        self.progress_file = progress_file

    def save(self, from_iter: int, total: int):
        """
        Saves the current progress to a file.

        Args:
            from_iter (int): The current iteration progress.
            total (int): The total number of iterations.
        """
        with open(self.progress_file, "w", encoding="utf-8") as file:
            file.write(f"{from_iter}/{total}/{time.time()}")

    def load(self, total: int) -> int:
        """
        Loads the previously saved progress if available and valid.

        Args:
            total (int): The total number of iterations for the current process.

        Returns:
            int: The iteration number to continue from.
        """
        if not os.path.exists(self.progress_file):
            return 0

        with open(self.progress_file, "r", encoding="utf-8") as file:
            last_, totl_, tmst_ = file.read().strip().split("/")
            last, totl = int(last_), int(totl_)

        if time.time() - float(tmst_) < 3600 and totl == total:
            action = input(f"🔍 Progress found, do you want to continue from the last progress ({last}/{totl})? [yes] | no: ").lower()
            if action in {"yes", "y", ""}:
                return last

        return 0


class CookieManager:
    """
    Manages HTTP session cookie operations for httpcloak.

    Methods:
        save: Saves session to a file
        load: Loads session from a file
        verify_user_login: Checks if the user is currently logged in
    """

    SESSION_FILE = "session.json"

    def __init__(self, session: "httpcloak.Session"):
        """
        Initialize the CookieManager with an httpcloak Session instance.

        Args:
            session (httpcloak.Session): The httpcloak session for cookie operations.
        """
        self.session = session

    def save(self):
        """
        Save the current session state (cookies, TLS sessions) to a file.
        """
        self.session.save(self.SESSION_FILE)
        logger.info("🍪 Session saved to %s", self.SESSION_FILE)

    def load(self):
        """
        Load session state from a file.
        """
        try:
            import httpcloak

            # Load the session from file and copy cookies to current session
            loaded_session = httpcloak.Session.load(self.SESSION_FILE)
            cookies = loaded_session.get_cookies_detailed()

            for cookie in cookies:
                self.session.set_cookie(
                    name=cookie.name,
                    value=cookie.value,
                    domain=cookie.domain or "",
                    path=cookie.path or "/",
                    secure=cookie.secure,
                    http_only=cookie.http_only,
                )
            loaded_session.close()
            logger.info("🍪 Session loaded from %s", self.SESSION_FILE)
        except Exception as e:
            if os.path.exists(self.SESSION_FILE):
                os.remove(self.SESSION_FILE)
            logger.error("🔴 Error loading session: %s. Invalid session has been removed, please restart.", e)

    def verify_user_login(self):
        """
        Test if the user is really logged in by fetching GitHub and checking login status.
        """
        logger.info("🤗 Verifying login status ...")

        response = self.session.get("https://github.com/")
        html = response.text

        # Check if "Sign in" text is present (indicates not logged in)
        if "Sign in" in html and 'href="/login"' in html:
            # Additional check - look for user menu or dashboard elements that indicate logged in state
            if '<meta name="user-login"' not in html and "dashboard" not in html.lower():
                if os.path.exists(self.SESSION_FILE):
                    os.remove(self.SESSION_FILE)
                logger.error("🔴 Error, you are not logged in, please restart and try again.")
                sys.exit(1)

        logger.info("✅ Login verified successfully")
        return True


class DatabaseManager:
    """
    This class is used to manage the database, including creating tables and handling data interactions.
    """

    def __init__(self, db_filename: str):
        """
        Initialize the DatabaseManager with the specified database filename.

        Args:
            db_filename (str): Path to the SQLite database file.
        """
        self.db_filename = db_filename
        self.con = None
        self.cur = None

    def __enter__(self):
        """
        Enter the runtime context related to this object, initializing the database if needed.
        """
        if not os.path.exists(self.db_filename):
            logging.info("Creating database github.db")

        self.con = sqlite3.connect(self.db_filename)
        self.cur = self.con.cursor()

        self.cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='APIKeys'")
        if self.cur.fetchone() is None:
            logging.info("Creating table APIKeys")
            self.cur.execute("CREATE TABLE APIKeys(apiKey, status, lastChecked)")

        self.cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='URLs'")
        if self.cur.fetchone() is None:
            logging.info("Creating table URLs")
            self.cur.execute("CREATE TABLE URLs(url, key)")

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Exit the runtime context and close the database connection.
        """
        if self.con:
            self.con.close()

    def all_iq_keys(self) -> list:
        """
        Get all keys with the status 'insufficient_quota'.

        Returns:
            list: A list of tuples containing API keys.
        """
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("SELECT apiKey FROM APIKeys WHERE status='insufficient_quota'")
        return self.cur.fetchall()

    def all_keys(self) -> list:
        """
        Get all keys with the status 'yes'.

        Returns:
            list: A list of tuples containing API keys.
        """
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("SELECT apiKey FROM APIKeys WHERE status='yes'")
        return self.cur.fetchall()

    def deduplicate(self) -> None:
        """
        Deduplicate the 'APIKeys' table by retaining only the latest record for each key.
        """
        if self.con is None:
            raise ValueError("Connection is not initialized")
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("CREATE TABLE temp_table as SELECT apiKey, status, MAX(lastChecked) as lastChecked FROM APIKeys GROUP BY apiKey;")
        self.cur.execute("DROP TABLE APIKeys;")
        self.cur.execute("ALTER TABLE temp_table RENAME TO APIKeys;")
        self.con.commit()

    def delete(self, api_key: str) -> None:
        """
        Delete a specific API key from the database.

        Args:
            api_key (str): The unique API key to remove.
        """
        if self.con is None:
            raise ValueError("Connection is not initialized")
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("DELETE FROM APIKeys WHERE apiKey=?", (api_key,))
        self.con.commit()

    def insert(self, api_key: str, status: str):
        """
        Insert a new API key and status into the database.

        Args:
            api_key (str): The API key to insert.
            status (str): The status of the API key.
        """
        if self.con is None:
            raise ValueError("Connection is not initialized")
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        today = date.today()
        self.cur.execute("INSERT INTO APIKeys(apiKey, status, lastChecked) VALUES(?, ?, ?)", (api_key, status, today))
        self.con.commit()

    def key_exists(self, api_key: str) -> bool:
        """
        Check if a given API key exists in the database.

        Args:
            api_key (str): The API key to search for.

        Returns:
            bool: True if the API key exists, False otherwise.
        """
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("SELECT apiKey FROM APIKeys WHERE apiKey=?", (api_key,))
        return self.cur.fetchone() is not None

    def insert_url(self, url: str) -> None:
        """
        Insert a new URL into the 'URLs' table.

        Args:
            url (str): The URL to add.
        """
        if self.con is None:
            raise ValueError("Connection is not initialized")
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("INSERT INTO URLs(url, key) VALUES(?, ?)", (url, 1))
        self.con.commit()

    def get_url(self, url: str) -> str | None:
        """
        Retrieve the 'key' associated with the given URL.

        Args:
            url (str): The URL to look up.

        Returns:
            str | None: The key if it exists, None if not.
        """
        if self.cur is None:
            raise ValueError("Cursor is not initialized")
        self.cur.execute("SELECT key FROM URLs WHERE url=?", (url,))
        fetch = self.cur.fetchone()
        return fetch[0] if fetch else None
