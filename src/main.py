"""
Scan GitHub for available OpenAI API Keys
"""

import argparse
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpcloak
import rich
from bs4 import BeautifulSoup
from tqdm import tqdm

from configs import KEYWORDS, LANGUAGES, PATHS, REGEX_LIST
from manager import CookieManager, DatabaseManager, ProgressManager
from utils import check_key

FORMAT = "%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT, datefmt="[%X]")
log = logging.getLogger("ChatGPT-API-Leakage")
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


class APIKeyLeakageScanner:
    """
    Scan GitHub for available OpenAI API Keys
    """

    def __init__(self, db_file: str, keywords: list, languages: list):
        self.db_file = db_file
        self.session: httpcloak.Session | None = None
        self.cookies: CookieManager | None = None
        self.page_source: str = ""  # Store the current page HTML
        rich.print(f"📂 Opening database file {self.db_file}")

        self.dbmgr = DatabaseManager(self.db_file)

        self.keywords = keywords
        self.languages = languages
        self.candidate_urls = []
        for regex, too_many_results, _ in REGEX_LIST:
            # Add the paths to the search query
            for path in PATHS:
                self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)+AND+({path})&type=code&ref=advsearch")

            for language in self.languages:
                if too_many_results:  # if the regex is too many results, then we need to add AND condition
                    self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)+language:{language}&type=code&ref=advsearch")
                else:  # if the regex is not too many results, then we just need the regex
                    self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)&type=code&ref=advsearch")

    def login_to_github(self):
        """
        Login to GitHub using httpcloak session with saved cookies
        """
        rich.print("🌍 Initializing HTTP session ...")

        # Create httpcloak session with Chrome fingerprint
        self.session = httpcloak.Session(
            preset="chrome-146",
            timeout=30,
            verify=True,
            allow_redirects=True,
        )

        self.cookies = CookieManager(self.session)

        session_file = "session.json"
        cookie_exists = os.path.exists(session_file)

        if not cookie_exists:
            rich.print("🤗 No session found. Please provide your GitHub cookies.")
            rich.print("   To get cookies, login to GitHub in your browser, then:")
            rich.print("   1. Open browser DevTools (F12)")
            rich.print("   2. Go to Application/Storage -> Cookies -> github.com")
            rich.print("   3. Copy all cookie values")
            rich.print("")

            # Prompt for essential cookies
            user_session = input("Enter 'user_session' cookie value: ").strip()
            logged_in = input("Enter 'logged_in' cookie value (usually 'yes'): ").strip() or "yes"
            dotcom_user = input("Enter 'dotcom_user' cookie value (optional, press Enter to skip): ").strip()
            gh_sess = input("Enter '_gh_sess' cookie value (optional, press Enter to skip): ").strip()

            # Set the cookies
            self.session.set_cookie("user_session", user_session, domain=".github.com", path="/", secure=True, http_only=True)
            self.session.set_cookie("logged_in", logged_in, domain=".github.com", path="/", secure=True)

            if dotcom_user:
                self.session.set_cookie("dotcom_user", dotcom_user, domain=".github.com", path="/", secure=True)
            if gh_sess:
                self.session.set_cookie("_gh_sess", gh_sess, domain=".github.com", path="/", secure=True, http_only=True)

            self.cookies.save()
        else:
            rich.print("🍪 Session found, loading session")
            self.cookies.load()

        self.cookies.verify_user_login()

    def _fetch_page(self, url: str) -> str:
        """
        Fetch a page using httpcloak and return the HTML content
        """
        if self.session is None:
            raise ValueError("Session is not initialized")

        response = self.session.get(url)
        self.page_source = response.text
        return self.page_source

    def _find_urls_and_apis(self, html: str) -> tuple[list[str], list[str]]:
        """
        Find all the urls and apis in the HTML content using BeautifulSoup
        """
        apis_found = []
        urls_need_expand = []

        soup = BeautifulSoup(html, "html.parser")

        # Find code blocks - GitHub uses different selectors for code search results
        # Try multiple selectors for robustness
        code_blocks = soup.find_all(class_="code-list")
        if not code_blocks:
            # Try alternative selectors
            code_blocks = soup.find_all("div", {"data-testid": "results"})
        if not code_blocks:
            code_blocks = soup.select(".search-match")

        for element in code_blocks:
            apis = []
            text_content = element.get_text()

            # Check all regex for each code block
            for regex, _, too_long in REGEX_LIST[2:]:
                if not too_long:
                    apis.extend(regex.findall(text_content))

            if len(apis) == 0:
                # Need to show full code. (because the api key is too long)
                # get the <a> tag
                a_tag = element.find("a", href=True)
                if a_tag and a_tag.get("href"):
                    href = a_tag["href"]
                    # Make sure it's an absolute URL
                    if href.startswith("/"):
                        href = f"https://github.com{href}"
                    urls_need_expand.append(href)
            apis_found.extend(apis)

        # Also try to find API keys directly in the page source for all regex patterns
        for regex, _, _ in REGEX_LIST:
            apis_found.extend(regex.findall(html))

        return list(set(apis_found)), list(set(urls_need_expand))

    def _check_rate_limit(self, html: str) -> bool:
        """
        Check if the page indicates a rate limit
        """
        return "You have exceeded a secondary rate limit" in html

    def _get_next_page_url(self, html: str) -> str | None:
        """
        Extract the next page URL from the HTML
        """
        soup = BeautifulSoup(html, "html.parser")

        # Find the "Next Page" link
        next_link = soup.find("a", {"aria-label": "Next Page"})
        if next_link and next_link.get("href"):
            href = next_link["href"]
            if href.startswith("/"):
                return f"https://github.com{href}"
            return href

        # Try alternative selectors
        next_link = soup.find("a", class_="next_page")
        if next_link and next_link.get("href"):
            href = next_link["href"]
            if href.startswith("/"):
                return f"https://github.com{href}"
            return href

        return None

    def _convert_to_raw_url(self, github_url: str) -> str:
        """
        Convert a GitHub file URL to its raw content URL.

        Examples:
            https://github.com/user/repo/blob/main/file.py -> https://raw.githubusercontent.com/user/repo/main/file.py
            https://github.com/user/repo/blob/abc123/path/file.py -> https://raw.githubusercontent.com/user/repo/abc123/path/file.py
        """
        # Pattern to match GitHub blob URLs
        # https://github.com/{owner}/{repo}/blob/{branch_or_commit}/{path}
        pattern = r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)"
        match = re.match(pattern, github_url)

        if match:
            owner, repo, ref, path = match.groups()
            # Remove any URL fragments (like #L123 line numbers)
            path = path.split("#")[0]
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"

        # If it doesn't match the expected pattern, return original URL
        return github_url

    def _expand_single_url(self, url: str) -> tuple[str, list[str], bool]:
        """
        Expand a single URL by fetching its raw content and extracting API keys.

        NOTE: This method should NOT access the database as it runs in parallel threads.
        Database operations are handled by the caller.

        Args:
            url: The GitHub URL to expand

        Returns:
            Tuple of (url, list of found API keys, success flag)
        """
        if self.session is None:
            return (url, [], False)

        raw_url = self._convert_to_raw_url(url)

        # If URL couldn't be converted to raw format, skip it
        if raw_url == url and "raw.githubusercontent.com" not in url:
            log.debug("Skipping non-convertible URL: %s", url[:80])
            return (url, [], True)  # Mark as success to not retry

        max_retries = 2
        for retry in range(max_retries + 1):
            try:
                response = self.session.get(raw_url, timeout=15)

                # Check for rate limiting
                if response.status_code == 429:
                    log.warning("Rate limited on %s, waiting...", raw_url[:50])
                    time.sleep(5 * (retry + 1))  # Exponential backoff
                    continue

                # 404 means file doesn't exist or was deleted - don't retry
                if response.status_code == 404:
                    log.debug("File not found (404): %s", raw_url[:80])
                    return (url, [], True)  # Mark as success to not retry

                # Other client errors (400-499) - don't retry
                if 400 <= response.status_code < 500:
                    log.debug("Client error %d for %s", response.status_code, raw_url[:80])
                    return (url, [], True)  # Mark as success to not retry

                # Server errors (500+) - retry
                if response.status_code >= 500:
                    log.debug("Server error %d for %s", response.status_code, raw_url[:50])
                    if retry < max_retries:
                        time.sleep(2)
                        continue
                    return (url, [], False)

                if response.status_code != 200:
                    log.debug("Unexpected status %d for %s", response.status_code, raw_url[:50])
                    return (url, [], True)

                content = response.text

                matches = []
                for regex, _, _ in REGEX_LIST:
                    matches.extend(regex.findall(content))
                matches = list(set(matches))

                # No need to retry if no matches - the file just doesn't contain API keys
                return (url, matches, True)

            except Exception as e:  # pylint: disable=broad-except
                log.debug("Error fetching %s: %s (retry %d/%d)", raw_url[:50], e, retry, max_retries)
                if retry < max_retries:
                    time.sleep(2 * (retry + 1))
                    continue
                return (url, [], False)

        return (url, [], False)

    def _process_url(self, url: str):
        """
        Process a search query url using HTTP requests with parallel URL expansion
        """
        if self.session is None:
            raise ValueError("Session is not initialized")

        current_url = url
        all_apis_found = []
        all_urls_need_expand = []
        page_count = 0

        while True:  # Loop until all the pages are processed
            page_count += 1
            html = self._fetch_page(current_url)

            # If current webpage is reached the rate limit, then wait for 30 seconds
            if self._check_rate_limit(html):
                for _ in tqdm(range(30), desc="⏳ Rate limit reached, waiting ..."):
                    time.sleep(1)
                continue  # Retry the same URL

            # Note: We can't "expand" code by clicking in HTTP mode
            # All code is already in the HTML response

            apis_found, urls_need_expand = self._find_urls_and_apis(html)
            all_apis_found.extend(apis_found)
            all_urls_need_expand.extend(urls_need_expand)

            log.debug("Page %d: found %d APIs, %d URLs to expand", page_count, len(apis_found), len(urls_need_expand))

            # Try to get next page
            next_url = self._get_next_page_url(html)
            if next_url:
                current_url = next_url
            else:
                break

        rich.print(f"    📄 Processed {page_count} pages, found {len(all_apis_found)} APIs directly, {len(all_urls_need_expand)} URLs to expand")

        # Filter out already processed URLs (done BEFORE parallel execution)
        urls_to_process = []
        processed_urls_cache = set()
        with self.dbmgr as mgr:
            for u in all_urls_need_expand:
                if not mgr.get_url(u):
                    urls_to_process.append(u)
                else:
                    processed_urls_cache.add(u)

        if not urls_to_process:
            rich.print("    ⚪️ All URLs already processed")
        else:
            rich.print(f"    🚀 Expanding {len(urls_to_process)} new URLs in parallel (10 threads)...")

            # Process URLs in parallel with 10 threads
            failed_urls = []
            successful_count = 0
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(tqdm(executor.map(self._expand_single_url, urls_to_process), total=len(urls_to_process), desc="🔍 Expanding URLs"))

            # Collect results and update database (done AFTER parallel execution)
            with self.dbmgr as mgr:
                for url_result, matches, success in results:
                    if success:
                        successful_count += 1
                        # Mark URL as processed
                        if url_result not in processed_urls_cache:
                            mgr.insert_url(url_result)
                            processed_urls_cache.add(url_result)

                        if matches:
                            # Filter out already existing keys
                            new_apis = [api for api in matches if not mgr.key_exists(api)]
                            all_apis_found.extend(new_apis)
                            if new_apis:
                                rich.print(f"    🔬 Found {len(new_apis)} new keys from {url_result[:50]}...")
                    else:
                        failed_urls.append(url_result)

            rich.print(f"    ✅ Successfully processed {successful_count}/{len(urls_to_process)} URLs")

            # Retry failed URLs with rechecking (only if there are actual failures)
            if failed_urls:
                rich.print(f"    🔄 Rechecking {len(failed_urls)} failed URLs after 5s wait...")
                time.sleep(5)  # Wait before retry

                with ThreadPoolExecutor(max_workers=10) as executor:
                    retry_results = list(tqdm(executor.map(self._expand_single_url, failed_urls), total=len(failed_urls), desc="🔄 Rechecking"))

                # Process retry results (done AFTER parallel execution)
                retry_success = 0
                with self.dbmgr as mgr:
                    for url_result, matches, success in retry_results:
                        if success:
                            retry_success += 1
                            # Mark URL as processed
                            if url_result not in processed_urls_cache:
                                mgr.insert_url(url_result)
                                processed_urls_cache.add(url_result)

                            if matches:
                                new_apis = [api for api in matches if not mgr.key_exists(api)]
                                all_apis_found.extend(new_apis)
                                if new_apis:
                                    rich.print(f"    🔬 Found {len(new_apis)} new keys on retry from {url_result[:50]}...")

                rich.print(f"    ✅ Retry recovered {retry_success}/{len(failed_urls)} URLs")

        self.check_api_keys_and_save(all_apis_found)

    def check_api_keys_and_save(self, keys: list[str]):
        """
        Check a list of API keys
        """
        with self.dbmgr as mgr:
            unique_keys = list(set(keys))
            unique_keys = [api for api in unique_keys if not mgr.key_exists(api)]

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(check_key, unique_keys))
            with self.dbmgr as mgr:
                for idx, result in enumerate(results):
                    mgr.insert(unique_keys[idx], result)

    def search(self, from_iter: int | None = None):
        """
        Search for API keys, and save the results to the database
        """
        progress = ProgressManager()
        total = len(self.candidate_urls)
        pbar = tqdm(
            enumerate(self.candidate_urls),
            total=total,
            desc="🔍 Searching ...",
        )
        if from_iter is None:
            from_iter = progress.load(total=total)

        for idx, url in enumerate(self.candidate_urls):
            if idx < from_iter:
                pbar.update()
                time.sleep(0.05)  # let tqdm print the bar
                log.debug("⚪️ Skip %s", url)
                continue
            self._process_url(url)
            progress.save(idx, total)
            log.debug("🔍 Finished %s", url)
            pbar.update()
        pbar.close()

    def deduplication(self):
        """
        Deduplicate the database
        """
        with self.dbmgr as mgr:
            mgr.deduplicate()

    def update_existed_keys(self):
        """
        Update previously checked API keys in the database with their current status
        """
        with self.dbmgr as mgr:
            rich.print("🔄 Updating existed keys")
            keys = mgr.all_keys()
            for key in tqdm(keys, desc="🔄 Updating existed keys ..."):
                result = check_key(key[0])
                mgr.delete(key[0])
                mgr.insert(key[0], result)

    def update_iq_keys(self):
        """
        Update insuffcient quota keys
        """
        with self.dbmgr as mgr:
            rich.print("🔄 Updating insuffcient quota keys")
            keys = mgr.all_iq_keys()
            for key in tqdm(keys, desc="🔄 Updating insuffcient quota keys ..."):
                result = check_key(key[0])
                mgr.delete(key[0])
                mgr.insert(key[0], result)

    def all_available_keys(self) -> list:
        """
        Get all available keys
        """
        with self.dbmgr as mgr:
            return mgr.all_keys()

    def __del__(self):
        if hasattr(self, "session") and self.session is not None:
            self.session.close()


def main(from_iter: int | None = None, check_existed_keys_only: bool = False, keywords: list | None = None, languages: list | None = None, check_insuffcient_quota: bool = False):
    """
    Main function to scan GitHub for available OpenAI API Keys
    """
    keywords = KEYWORDS.copy() if keywords is None else keywords
    languages = LANGUAGES.copy() if languages is None else languages

    leakage = APIKeyLeakageScanner("github.db", keywords, languages)

    if not check_existed_keys_only:
        leakage.login_to_github()
        leakage.search(from_iter=from_iter)

    if check_insuffcient_quota:
        leakage.update_iq_keys()

    leakage.update_existed_keys()
    leakage.deduplication()
    keys = leakage.all_available_keys()

    rich.print(f"🔑 [bold green]Available keys ({len(keys)}):[/bold green]")
    for key in keys:
        rich.print(f"[bold green]{key[0]}[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-iter", type=int, default=None, help="Start from the specific iteration")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode, otherwise INFO mode. Default is False (INFO mode)",
    )
    parser.add_argument(
        "-ceko",
        "--check-existed-keys-only",
        action="store_true",
        default=False,
        help="Only check existed keys",
    )
    parser.add_argument(
        "-ciq",
        "--check-insuffcient-quota",
        action="store_true",
        default=False,
        help="Check and update status of the insuffcient quota keys",
    )
    parser.add_argument(
        "-k",
        "--keywords",
        nargs="+",
        default=KEYWORDS,
        help="Keywords to search",
    )
    parser.add_argument(
        "-l",
        "--languages",
        nargs="+",
        default=LANGUAGES,
        help="Languages to search",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    main(
        from_iter=args.from_iter,
        check_existed_keys_only=args.check_existed_keys_only,
        keywords=args.keywords,
        languages=args.languages,
        check_insuffcient_quota=args.check_insuffcient_quota,
    )
