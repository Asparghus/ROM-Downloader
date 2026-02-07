#!/usr/bin/env python

import sys
import os
import time
import re
import threading
import argparse
import logging
import json
import xml.etree.ElementTree as ET
import ctypes 
from urllib.parse import urljoin, urlparse, unquote, urlunparse
from difflib import get_close_matches
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- DEPENDENCY CHECK ---
def check_dependencies():
    """Checks for required third-party libraries."""
    if getattr(sys, 'frozen', False):
        return

    required_packages = [
        ("requests", "requests"),
        ("bs4", "beautifulsoup4"),
        ("colorama", "colorama"),
        ("tqdm", "tqdm"),
        ("PyQt5", "PyQt5")
    ]
    missing = []
    for import_name, package_name in required_packages:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        print("\n" + "!" * 60)
        print(" [ERROR] Missing required Python libraries.")
        print(f" The following packages are missing: {', '.join(missing)}")
        print("-" * 60)
        print(" Please run the following command to install them:")
        print(f"\n    {sys.executable} -m pip install {' '.join(missing)}\n")
        print("!" * 60 + "\n")
        sys.exit(1)

check_dependencies()

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style, init
from tqdm import tqdm

# PyQt5 Imports - Hard dependency for PyInstaller to detect
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                                QFormLayout, QLabel, QLineEdit, QPushButton, 
                                QFileDialog, QCheckBox, QGroupBox, QMessageBox, 
                                QScrollArea, QDialog, QDialogButtonBox, QGridLayout,
                                QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, 
                                QProgressBar, QStyledItemDelegate, QInputDialog)
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QIcon, QPalette, QColor, QTextCursor

init(autoreset=True)

# --- GLOBAL CONFIGURATION ---
CONFIG = {
    "INPUT_PATH": "", 
    "URL_MAPPING_FILE": "url_mapping.txt",
    "DOWNLOAD_FOLDER": os.path.join(os.getcwd(), "Downloads"),
    "TIMEOUT": 60,
    "RETRY_DELAY": 5,
    "MAX_RETRIES": 3,
    "DELETE_EMPTY_DATS": True,
    "FOLDER_PERMISSIONS": 0o777,
    "FILE_PERMISSIONS": 0o666,
    "SUB_THREAD_WORKERS": 3,
    "FUZZY_THRESHOLD": 0.90,
    "RECURSION_DEPTH": 2,
    "INTERACTIVE_MODE": False,
    "DISABLED_DOMAINS": [],
    "GUI_GEOMETRY": "",
    "DOMAIN_FILTER_GEOMETRY": "",
    "MAIN_THREADS": 4,
    "SHOW_NONE_DATS": False,
    "SHOW_PROCESSED_DATS": False,
    "SHOW_DELETED_DATS": False
}

# --- CONFIG PERSISTENCE ---
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved_config = json.load(f)
                CONFIG.update(saved_config)
        except Exception as e:
            pass

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(CONFIG, f, indent=4)
    except Exception as e:
        log.error(f"{Fore.RED}[ERROR] Could not save config file: {e}{Style.RESET_ALL}")

# --- INITIALIZE CONFIG ---
load_config() 

# --- GLOBAL SIGNALS BRIDGE ---
class WorkerSignals(QObject):
    progress = pyqtSignal(str, int, str)
    finished = pyqtSignal(str)
    input_request = pyqtSignal(str, object, dict)

GUI_SIGNALS = None

# --- LOGGING SETUP ---
log = logging.getLogger()
log.setLevel(logging.INFO)
log_lock = threading.Lock()

class ConditionalFormatter(logging.Formatter):
    DEBUG_FORMAT = '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    INFO_FORMAT = '%(message)s'
    DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

    def __init__(self):
        super().__init__(fmt=self.INFO_FORMAT, datefmt=self.DATE_FORMAT)

    def format(self, record):
        original_format = self._style._fmt
        if record.levelno == logging.DEBUG:
            self._style._fmt = self.DEBUG_FORMAT
        else:
            self._style._fmt = self.INFO_FORMAT
        result = super().format(record)
        self._style._fmt = original_format
        return result

class TqdmLoggingHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            with log_lock:
                # If GUI is running, simple print (redirected), else tqdm write
                if GUI_SIGNALS:
                    print(msg)
                else:
                    tqdm.write(msg)
        except Exception:
            self.handleError(record)

handler = TqdmLoggingHandler(sys.stdout)
formatter = ConditionalFormatter()
handler.setFormatter(formatter)
if not log.hasHandlers():
    log.addHandler(handler)

# --- SESSION SETUP ---
session = requests.Session()

# --- HELPER FUNCTIONS ---

def _set_path_permissions(path, permissions, is_folder=False):
    try:
        os.chmod(path, permissions)
    except OSError as e:
        log.warning(f"{Fore.YELLOW}[WARN] Could not set permissions on {('folder' if is_folder else 'file')} {path}: {e}{Style.RESET_ALL}")

def _sanitize_for_path(name, fallback_base="UnknownDAT"):
    if not name or not name.strip():
        return fallback_base
    invalid_chars = r'<>:"/\|?*' + ''.join(chr(i) for i in range(32))
    safe_name = name.strip()
    for char in invalid_chars:
        safe_name = safe_name.replace(char, '_')
    safe_name = re.sub('_+', '_', safe_name)
    safe_name = safe_name.strip('. _')
    return safe_name if safe_name else fallback_base

def _ensure_subfolder_with_permissions(subfolder_path):
    if not os.path.isdir(subfolder_path):
        try:
            os.makedirs(subfolder_path, exist_ok=True)
            _set_path_permissions(subfolder_path, CONFIG["FOLDER_PERMISSIONS"], is_folder=True)
        except OSError as e:
            log.error(f"{Fore.RED}[ERROR] Could not create subfolder: {subfolder_path}. Error: {e}{Style.RESET_ALL}")
            return False
    return True

def get_headers(url):
    try:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}/"
    except:
        base_url = "https://archive.org/"

    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': base_url,
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'DNT': '1'
    }

def make_request(url, stream=False):
    for attempt in range(CONFIG["MAX_RETRIES"]):
        response = None
        try:
            response = session.get(
                url, 
                stream=stream, 
                timeout=CONFIG["TIMEOUT"], 
                headers=get_headers(url), 
                allow_redirects=True
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            status_code = None
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                status_code = e.response.status_code
            
            if status_code == 404:
                return None

            log.warning(f"{Fore.YELLOW}[RETRY {attempt + 1}/{CONFIG['MAX_RETRIES']}] Request failed for {url} (Status: {status_code}): {e}{Style.RESET_ALL}")
            
            if response:
                try: response.close()
                except Exception: pass
            
            if attempt + 1 == CONFIG["MAX_RETRIES"]:
                log.error(f"{Fore.RED}[FAIL] Max retries reached for {url}.{Style.RESET_ALL}")
                return None
            time.sleep(CONFIG["RETRY_DELAY"] * (attempt + 1))
    return None

def classify_archive_org_url(url):
    try:
        parsed = urlparse(url)
        if parsed.netloc.lower() != 'archive.org':
            return 'generic'
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) >= 2:
            if path_parts[0] == 'details':
                if path_parts[1].startswith('@'): return 'profile'
                if len(path_parts) == 2 and path_parts[1]: return 'archive'
            elif path_parts[0] == 'download':
                 if len(path_parts) == 2 and path_parts[1]: return 'download'
        return 'generic'
    except Exception:
        return 'generic'

def convert_archive_to_download_url(archive_url):
    try:
        parsed = urlparse(archive_url)
        path_parts = parsed.path.strip('/').split('/')
        if parsed.netloc.lower() == 'archive.org' and len(path_parts) == 2 and path_parts[0] == 'details' and path_parts[1] and not path_parts[1].startswith('@'):
            new_path = '/download/' + path_parts[1]
            return urlunparse((parsed.scheme, parsed.netloc, new_path, '', '', ''))
        return archive_url
    except Exception:
        return archive_url

def extract_download_urls_from_profile(profile_url):
    log.info(f"{Fore.CYAN}[PROFILE] Scraping profile: {profile_url}{Style.RESET_ALL}")
    response = make_request(profile_url)
    if not response: return []

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        if "archive.org" in urlparse(profile_url).netloc.lower():
            main_content = soup.find("main", id="maincontent")
            if main_content:
                soup = main_content

        found_urls = set()
        archive_link_pattern = re.compile(r"^/details/([^/@][^/]*)$")
        base_for_join = response.url if response.url.endswith('/') else response.url + '/'

        for link in soup.find_all("a"):
            href = link.get("href")
            if not href: continue
            absolute_href = urljoin(base_for_join, href)
            parsed_href = urlparse(absolute_href)

            if parsed_href.netloc.lower() != 'archive.org': continue
            match = archive_link_pattern.match(parsed_href.path)
            if match:
                archive_url = absolute_href
                download_url = convert_archive_to_download_url(archive_url)
                found_urls.add(download_url)
        
        return sorted(list(found_urls))
    finally:
        if response:
            try: response.close()
            except Exception: pass

def ensure_download_folder(path):
    if not os.path.exists(path):
        log.info(f"{Fore.CYAN}[SETUP] Creating base download folder: {path}{Style.RESET_ALL}")
        try:
            os.makedirs(path, exist_ok=True)
            _set_path_permissions(path, CONFIG["FOLDER_PERMISSIONS"], is_folder=True)
        except OSError as e:
            log.critical(f"{Fore.RED}[FATAL] Could not create base download folder: {path}. Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
    else:
        _set_path_permissions(path, CONFIG["FOLDER_PERMISSIONS"], is_folder=True)


# --- DOMAIN FILTERING ---
def _normalize_domain(netloc: str) -> str:
    if not netloc:
        return ""
    netloc = netloc.strip().lower()
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

def _domain_from_url(url: str) -> str:
    try:
        return _normalize_domain(urlparse(url).netloc)
    except Exception:
        return ""

def _disabled_domains_set() -> set:
    raw = CONFIG.get("DISABLED_DOMAINS") or []
    return { _normalize_domain(d) for d in raw if isinstance(d, str) and d.strip() }

def _is_domain_disabled(url: str, disabled_domains: set) -> bool:
    d = _domain_from_url(url)
    return bool(d) and d in disabled_domains

# --- MAPPING IO ---
def load_url_mappings(filename):
    url_mappings = {}
    if not os.path.exists(filename): return {}
    try:
        with open(filename, "r") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith('#') or "=" not in line: continue
                name, url = line.split("=", 1)
                name = name.strip()
                url = url.strip().strip('"').strip("'")
                if name and url:
                    url_mappings.setdefault(name, []).append(url)
    except IOError as e:
        log.error(f"{Fore.RED}[ERROR] Could not read mapping file. {e}{Style.RESET_ALL}")
    return url_mappings

def save_url_mappings(filename, url_mappings):
    try:
        with open(filename, "w") as file:
            for name in sorted(url_mappings.keys()):
                urls = url_mappings[name]
                if isinstance(urls, list):
                    for url in sorted(urls):
                        file.write(f'{name}="{url}"\n')
                elif isinstance(urls, str):
                    file.write(f'{name}="{urls}"\n')
    except IOError: pass

# --- PARSING & CRAWLING ---

def parse_dat_file(dat_file):
    try:
        tree = ET.parse(dat_file)
        root = tree.getroot()
    except Exception as e:
        log.error(f"{Fore.RED}[ERROR] Failed to parse {dat_file}: {e}{Style.RESET_ALL}")
        return None, None, 0

    games_info = {}
    header = root.find("header")
    dat_name = header.find("name").text.strip() if header is not None and header.find("name") is not None else os.path.splitext(os.path.basename(dat_file))[0]

    mia_count = 0
    for game_element in root.findall(".//game") + root.findall(".//machine"):
        game_name = game_element.get("name")
        if not game_name: continue

        is_mia = game_element.get("mia") == "yes"
        if not is_mia:
             roms = game_element.findall("rom"); disks = game_element.findall("disk")
             if roms and all(r.get("mia") == "yes" for r in roms if r.get("status") != "optional"): is_mia = True
             if disks and all(d.get("mia") == "yes" for d in disks if d.get("status") != "optional"): is_mia = True

        if is_mia:
            mia_count += 1; continue

        disks = [d.get("name") for d in game_element.findall("disk") if d.get("name") and d.get("status") != "optional"]
        if disks:
            games_info[game_name] = {"type": "disk", "files": disks}
            continue
        
        if game_element.findall("rom"):
            games_info[game_name] = {"type": "rom", "files": [game_name]} 
    
    return dat_name, games_info, mia_count

def crawl_links_recursive(start_url, cache, max_depth, current_depth=0):
    if current_depth > max_depth:
        return {}
    
    if start_url in cache:
        return cache[start_url]

    if current_depth == 0:
        log.info(f"{Fore.CYAN}[CRAWL] Scanning: {start_url}{Style.RESET_ALL}")

    response = make_request(start_url)
    if not response:
        cache[start_url] = {}
        return {}

    links = {}
    subfolders_to_scan = []

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        if "archive.org" in urlparse(start_url).netloc.lower():
            main_content = soup.find("main", id="maincontent")
            if main_content:
                soup = main_content
        base_for_join = response.url if response.url.endswith('/') else response.url + '/'

        for link in soup.find_all("a"):
            href = link.get("href")
            if not href or href.strip() == '' or href.startswith(('?', '#', 'javascript:', 'mailto:')): continue
            if href in ['../', './', '/']: continue
            
            full_url = urljoin(base_for_join, href)
            path = urlparse(full_url).path
            
            if href.endswith('/') or path.endswith('/'):
                subfolders_to_scan.append(full_url)
                continue

            basename = unquote(os.path.basename(path))
            key_base = os.path.splitext(basename)[0].strip()
            if key_base:
                links[key_base] = full_url

        if subfolders_to_scan and current_depth < max_depth:
            for folder_url in subfolders_to_scan:
                sub_links = crawl_links_recursive(folder_url, cache, max_depth, current_depth + 1)
                for k, v in sub_links.items():
                    if k not in links:
                        links[k] = v

        cache[start_url] = links
        return links
    finally:
        if response: 
            try: response.close()
            except Exception: pass

def download_file(url, destination_path, file_display_name="Unknown"):
    response = make_request(url, stream=True)
    if not response: return False

    total_size = int(response.headers.get('content-length', 0))
    temp_path = destination_path + ".part"
    file_display = os.path.basename(destination_path)

    # --- GUI MODE CHECK ---
    is_gui_mode = (GUI_SIGNALS is not None)
    
    try:
        with open(temp_path, "wb") as file:
            
            if is_gui_mode:
                # GUI Mode: Manual chunk reading and signal emission
                downloaded = 0
                start_time = time.time()
                for chunk in response.iter_content(chunk_size=32768):
                    if chunk:
                        file.write(chunk)
                        downloaded += len(chunk)
                        
                        # Calculate percentage and speed
                        percent = int((downloaded / total_size) * 100) if total_size > 0 else 0
                        elapsed = time.time() - start_time
                        speed_mb = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                        status_text = f"{speed_mb:.1f} MB/s"
                        
                        GUI_SIGNALS.progress.emit(file_display, percent, status_text)
            else:
                # CLI Mode: Use TQDM
                with tqdm(total=total_size, unit='B', unit_scale=True, desc=f"{Fore.BLUE}{file_display[:20]:<20}{Style.RESET_ALL}", leave=False, ncols=100) as bar:
                    for chunk in response.iter_content(chunk_size=32768):
                        if chunk:
                            file.write(chunk)
                            bar.update(len(chunk))

        if total_size > 0 and os.path.getsize(temp_path) != total_size:
            raise IOError("Incomplete download")
        
        os.rename(temp_path, destination_path)
        _set_path_permissions(destination_path, CONFIG["FILE_PERMISSIONS"])
        
        if is_gui_mode:
            GUI_SIGNALS.finished.emit(file_display)
            
        return True
    except Exception as e:
        log.warning(f"{Fore.YELLOW}[FAIL] Download error {file_display}: {e}{Style.RESET_ALL}")
        if os.path.exists(temp_path): os.remove(temp_path)
        return False
    finally:
        if response:
            try: response.close()
            except Exception: pass

def get_fuzzy_match(target_name, candidates, threshold=0.9):
    matches = get_close_matches(target_name, candidates.keys(), n=3, cutoff=threshold)
    
    def get_numbers(s):
        return set(re.findall(r'\d+', s))

    target_nums = get_numbers(target_name)

    for match in matches:
        match_nums = get_numbers(match)
        if target_nums == match_nums:
            return match, candidates[match]
            
    return None, None

def _process_file_target(target, game_name, aggregated_links, subfolder_path):
    found_url = None
    if target in aggregated_links:
        found_url = aggregated_links[target]
    
    if not found_url:
        match_name, match_url = get_fuzzy_match(target, aggregated_links, threshold=CONFIG["FUZZY_THRESHOLD"])
        if match_url:
            log.info(f"{Fore.CYAN}[FUZZY] '{game_name}': Matched '{target}' -> '{match_name}'{Style.RESET_ALL}")
            found_url = match_url

    if found_url:
        filename = unquote(os.path.basename(urlparse(found_url).path))
        dest_path = os.path.join(subfolder_path, filename)
        if download_file(found_url, dest_path, filename):
            return urlparse(found_url).netloc
        else:
            return None
    else:
        return None

def process_single_game(game_name, game_info, source_urls, available_links_cache, subfolder_path):
    game_type = game_info["type"]
    required_files = game_info["files"]
    
    if game_type == "rom":
        targets = [required_files[0].strip()]
    else:
        targets = [d.strip() for d in required_files if d.strip()]
    
    if not targets: return False, f"Skipped '{game_name}': No valid file names."

    try:
        local_files = os.listdir(subfolder_path) if os.path.exists(subfolder_path) else []
    except OSError: local_files = []
    
    local_basenames = {os.path.splitext(unquote(f))[0].strip() for f in local_files}
    missing_targets = [t for t in targets if t not in local_basenames]

    if not missing_targets:
        return True, f"{Fore.GREEN}[EXISTS] '{game_name}'{Style.RESET_ALL}"

    if not _ensure_subfolder_with_permissions(subfolder_path):
        return False, f"Permission error creating folder for {game_name}"

    aggregated_links = {}
    for url in source_urls:
        links = crawl_links_recursive(url, available_links_cache, max_depth=CONFIG["RECURSION_DEPTH"])
        aggregated_links.update(links)

    success_count = 0
    used_domains = set()
    
    with ThreadPoolExecutor(max_workers=CONFIG["SUB_THREAD_WORKERS"]) as file_executor:
        future_to_file = {
            file_executor.submit(_process_file_target, target, game_name, aggregated_links, subfolder_path): target for target in missing_targets
        }

        for future in as_completed(future_to_file):
            result = future.result()
            if result:
                success_count += 1
                used_domains.add(result)

    if success_count == len(missing_targets):
        sources_str = ", ".join(sorted(used_domains))
        return True, f"{Fore.GREEN}[DONE] '{game_name}' (from {sources_str}){Style.RESET_ALL}"
    elif success_count > 0:
        return False, f"{Fore.YELLOW}[PARTIAL] '{game_name}'{Style.RESET_ALL}"
    else:
        return False, f"{Fore.RED}[MISSING] '{game_name}'{Style.RESET_ALL}"

def _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, interactive_mode, disabled_domains):
    original_urls = url_mappings.get(dat_name)
    processed_urls = set()
    
    needs_input = False
    if original_urls is None: needs_input = True
    elif original_urls == ['none']: return None
    else:
        valid_found = False
        for url in original_urls:
            if url and url.lower() != 'none':
                u_type = classify_archive_org_url(url)
                if u_type == 'profile':
                    extracted = extract_download_urls_from_profile(url)
                    processed_urls.update(extracted)
                    valid_found = True
                elif u_type == 'archive':
                    processed_urls.add(convert_archive_to_download_url(url))
                    valid_found = True
                else:
                    processed_urls.add(url)
                    valid_found = True
        if not valid_found: needs_input = True

    if needs_input:
        if not interactive_mode:
            return None
        
        user_input = ""
        
        if GUI_SIGNALS:
            event = threading.Event()
            container = {}
            GUI_SIGNALS.input_request.emit(dat_name, event, container)
            event.wait()
            user_input = container.get("text", "").strip()
        else:
            print(f"{Fore.YELLOW}>>> Missing URL for: {dat_name}{Style.RESET_ALL}")
            try:
                user_input = input(f"{Fore.CYAN}Enter URL (or 'none' to skip): {Style.RESET_ALL}").strip()
            except EOFError:
                user_input = "none"

        if not user_input or user_input.lower() == 'none':
            url_mappings[dat_name] = ['none']
            save_url_mappings(CONFIG["URL_MAPPING_FILE"], url_mappings)
            return None
            
        url_mappings[dat_name] = [user_input]
        save_url_mappings(CONFIG["URL_MAPPING_FILE"], url_mappings)
        return _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, interactive_mode, disabled_domains)

    if disabled_domains:
        processed_urls = {u for u in processed_urls if not _is_domain_disabled(u, disabled_domains)}
    return sorted(list(processed_urls))

# --- CORE LOGIC ---
def run_downloader():
    if not CONFIG["INPUT_PATH"] or not os.path.exists(CONFIG["INPUT_PATH"]):
        log.critical(f"Input path not found or not set: {CONFIG.get('INPUT_PATH')}")
        return

    dat_files = []
    if os.path.isdir(CONFIG["INPUT_PATH"]):
        dat_files = [os.path.join(CONFIG["INPUT_PATH"], f) for f in os.listdir(CONFIG["INPUT_PATH"]) if f.lower().endswith('.dat')]
    else:
        dat_files = [CONFIG["INPUT_PATH"]]

    ensure_download_folder(CONFIG["DOWNLOAD_FOLDER"])
    url_mappings = load_url_mappings(CONFIG["URL_MAPPING_FILE"])
    available_links_cache = {}
    disabled_domains = _disabled_domains_set() 
    
    missing_mapping_dats = []
    mapping_none_dats = []
    processed_dats = []
    deleted_dats = []

    log.info(f"{Fore.GREEN}Starting processing with {CONFIG['MAIN_THREADS']} threads.{Style.RESET_ALL}")
    
    log.info(f"{Fore.GREEN}--- Active Configuration ---{Style.RESET_ALL}")
    for key, value in sorted(CONFIG.items()):
        if "PERMISSIONS" in key and isinstance(value, int):
            val_str = oct(value)
        else:
            val_str = str(value)
        log.info(f"  {Fore.CYAN}{key:<20}{Style.RESET_ALL} : {val_str}")
    log.info(f"{Fore.GREEN}----------------------------{Style.RESET_ALL}")

    for dat_path in sorted(dat_files):
        dat_filename = os.path.basename(dat_path)
        log.info(f"\n{Fore.BLUE}{'='*30}\nProcessing: {dat_filename}\n{'='*30}{Style.RESET_ALL}")

        dat_name, games_info, mia_count = parse_dat_file(dat_path)
        if not dat_name: continue

        if not games_info:
            log.info(f"Empty DAT (MIA only or no roms).")
            if CONFIG["DELETE_EMPTY_DATS"]:
                os.remove(dat_path)
                deleted_dats.append(dat_filename)
                log.info(f"{Fore.YELLOW}Deleted empty DAT.{Style.RESET_ALL}")
            else:
                log.info(f"{Fore.YELLOW}Skipped deletion (kept empty).{Style.RESET_ALL}")
            continue

        source_urls = _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, CONFIG["INTERACTIVE_MODE"], disabled_domains)
        
        if dat_name not in url_mappings or not url_mappings[dat_name]:
            source_urls = _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, CONFIG["INTERACTIVE_MODE"], disabled_domains)
        else:
            source_urls = _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, False, disabled_domains)

        if not source_urls:
            if dat_name in url_mappings and url_mappings[dat_name] and url_mappings[dat_name] != ['none']:
                log.warning(f"{Fore.RED}[FAIL] Mapping exists but no links found. Check the URL content: {url_mappings[dat_name]}{Style.RESET_ALL}")
            elif url_mappings[dat_name] == ['none']:
                log.warning(f"{Fore.RED}[SKIP] URL for '{dat_name}' mapped as none on url_mappings.txt{Style.RESET_ALL}")
                mapping_none_dats.append(dat_name)
            else:
                log.warning(f"{Fore.RED}[SKIP] No URL mapping found for '{dat_name}'{Style.RESET_ALL}")
                missing_mapping_dats.append(dat_name)
            continue

        subfolder_name = _sanitize_for_path(dat_name)
        subfolder_path = os.path.join(CONFIG["DOWNLOAD_FOLDER"], subfolder_name)
        
        log.info(f"Scanning {len(games_info)} games using {len(source_urls)} sources...")
        
        successful_games = 0
        failed_games = 0
        
        with ThreadPoolExecutor(max_workers=CONFIG["MAIN_THREADS"]) as executor:
            future_to_game = {
                executor.submit(
                    process_single_game, 
                    g_name, 
                    g_info, 
                    source_urls, 
                    available_links_cache, 
                    subfolder_path
                ): g_name for g_name, g_info in games_info.items()
            }
            
            for future in as_completed(future_to_game):
                try:
                    success, msg = future.result()
                    # Print without extra newline if using GUI stream
                    if GUI_SIGNALS: print(msg)
                    else: tqdm.write(msg)
                    
                    if success: successful_games += 1
                    else: failed_games += 1
                except Exception as exc:
                    log.error(f"Thread exception: {exc}")
                    failed_games += 1

        log.info(f"{Fore.CYAN}[RESULT] {dat_name}: Success: {successful_games}, Failed: {failed_games}{Style.RESET_ALL}")
        
        if failed_games == 0 and successful_games > 0:
            log.info(f"{Fore.GREEN}DAT Complete. Deleting DAT file.{Style.RESET_ALL}")
            try: os.remove(dat_path); deleted_dats.append(dat_filename)
            except: pass
        
        processed_dats.append(dat_name)

    print(f"\n{Fore.BLUE}{'='*30}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}Run Complete.{Style.RESET_ALL}")

    if processed_dats:
        if CONFIG.get("SHOW_PROCESSED_DATS", False):
            print(f"\n{Fore.BLUE}Processed DATs ({len(processed_dats)}):{Style.RESET_ALL}")
            for d in processed_dats: print(f" - {d}")
        else:
            print(f"Processed: {len(processed_dats)}")

    if deleted_dats:
        if CONFIG.get("SHOW_DELETED_DATS", False):
            print(f"\n{Fore.BLUE}Deleted DATs ({len(deleted_dats)}):{Style.RESET_ALL}")
            for d in deleted_dats: print(f" - {d}")
        else:
            print(f"Deleted: {len(deleted_dats)}")
    
    if missing_mapping_dats:
        print(f"\n{Fore.BLUE}Missing URL mappings:{Style.RESET_ALL}")
        for d in missing_mapping_dats:
            print(f" - {d}")
        print(f"{Fore.YELLOW}(Enable Interactive Mode to fix these on next run){Style.RESET_ALL}")

    if mapping_none_dats:
        if CONFIG.get("SHOW_NONE_DATS", False):
             print(f"\n{Fore.BLUE}'None' Mappings ({len(mapping_none_dats)}):{Style.RESET_ALL}")
             for d in mapping_none_dats:
                print(f" - {d}")
        else:
             print(f"Skipped 'None' Mappings: {len(mapping_none_dats)}")
        print(f"{Fore.YELLOW}(Edit the URL mappings file to fix these on next run){Style.RESET_ALL}")

# --- THREADING & LOGGING HELPERS ---
class StreamRedirector(QObject):
    """Redirects print() and logging output to a PyQt signal."""
    text_written = pyqtSignal(str)

    def write(self, text):
        self.text_written.emit(str(text))
    
    def flush(self):
        pass

class DownloadWorker(QThread):
    """Runs the downloader logic in a separate thread."""
    finished = pyqtSignal()

    def run(self):
        try:
            run_downloader()
        except Exception as e:
            print(f"Error in worker: {e}")
        finally:
            self.finished.emit()

# --- PYQT5 GUI CLASSES ---
class DomainFilterDialog(QDialog):
    def __init__(self, parent=None, mapping_path=""):
        super().__init__(parent)
        self.setWindowTitle("Domain Filters")
        
        # --- Force Dark Title Bar (Windows) ---
        try:
            hwnd = self.winId()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(hwnd), 20, ctypes.byref(ctypes.c_int(1)), 4
            )
        except Exception:
            pass

        # --- Window Geometry ---
        geom = CONFIG.get("DOMAIN_FILTER_GEOMETRY")
        geometry_set = False
        if geom:
            try:
                w, h = map(int, geom.split('x'))
                self.resize(w, h)
                geometry_set = True
            except: 
                pass

        if not geometry_set:
            self.resize(467, 590) 

        layout = QVBoxLayout(self)

        info_lbl = QLabel("Uncheck any domain you want to disable. Disabled domains will be skipped during crawling and downloading.")
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        # Scroll Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_area.setWidget(self.scroll_content)
        layout.addWidget(self.scroll_area)

        # Logic
        self.checkboxes = {}
        domains = self._collect_domains_from_mapping(mapping_path)
        
        CONFIG.setdefault("DISABLED_DOMAINS", [])
        disabled = set([d.lower() for d in CONFIG.get("DISABLED_DOMAINS", []) if d])

        if not domains:
             self.scroll_layout.addWidget(QLabel("No valid source domains found in mapping file."))
        
        for dom in domains:
            cb = QCheckBox(dom)
            cb.setChecked(dom not in disabled)
            self.scroll_layout.addWidget(cb)
            self.checkboxes[dom] = cb
        
        self.scroll_layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_enable_all = QPushButton("Enable All")
        btn_disable_all = QPushButton("Disable All")
        btn_save = QPushButton("Save")
        btn_cancel = QPushButton("Cancel")

        btn_enable_all.clicked.connect(self.enable_all)
        btn_disable_all.clicked.connect(self.disable_all)
        btn_save.clicked.connect(self.save_and_close)
        btn_cancel.clicked.connect(self.reject)

        btn_layout.addWidget(btn_enable_all)
        btn_layout.addWidget(btn_disable_all)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _collect_domains_from_mapping(self, mapping_path):
        domains = set()
        try:
            url_mappings = load_url_mappings(mapping_path)
        except Exception:
            url_mappings = {}

        for _, urls in url_mappings.items():
            if not urls: continue
            for u in urls:
                if not u or str(u).lower() == "none": continue
                d = _domain_from_url(u)
                if d: domains.add(d)
        return sorted(domains)

    def enable_all(self):
        for cb in self.checkboxes.values(): cb.setChecked(True)

    def disable_all(self):
        for cb in self.checkboxes.values(): cb.setChecked(False)

    def save_and_close(self):
        new_disabled = [d for d, cb in self.checkboxes.items() if not cb.isChecked()]
        CONFIG["DISABLED_DOMAINS"] = sorted(set([d.lower() for d in new_disabled]))
        
        sz = self.size()
        CONFIG["DOMAIN_FILTER_GEOMETRY"] = f"{sz.width()}x{sz.height()}"
        save_config()
        self.accept()

class ConfigWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROM Downloader Configuration")
        
        # --- Force Dark Title Bar (Windows) ---
        try:
            hwnd = self.winId()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(hwnd), 20, ctypes.byref(ctypes.c_int(1)), 4
            )
        except Exception:
            pass

        # --- Window Geometry ---
        geom = CONFIG.get("GUI_GEOMETRY")
        geometry_set = False
        if geom:
            try:
                w, h = map(int, geom.split('x'))
                self.resize(w, h)
                geometry_set = True
            except: 
                pass
        
        if not geometry_set:
            self.resize(550, 850)

        self.inputs = {}
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # --- SCROLL AREA FOR CONFIGS (Fixed Height) ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(0)
        
        # Enforce the specific dimensions you requested
        scroll.setMinimumWidth(520)
        scroll.setFixedHeight(520) 
        
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0,0,0,0)
        scroll.setWidget(scroll_content)
        
        main_layout.addWidget(scroll)

        # 1. Paths
        group_paths = QGroupBox("Paths & Files")
        layout_paths = QGridLayout()
        group_paths.setLayout(layout_paths)
        self.add_path_row(layout_paths, 0, "INPUT_PATH", "FixDATs Source (DAT/Folder):", mode="both")
        self.add_path_row(layout_paths, 1, "DOWNLOAD_FOLDER", "Download Folder:", mode="dir")
        self.add_path_row(layout_paths, 2, "URL_MAPPING_FILE", "URL Mapping File:", mode="file")
        btn_domains = QPushButton("Edit Domain Filters")
        btn_domains.clicked.connect(self.open_domain_filter)
        layout_paths.addWidget(btn_domains, 3, 1, 1, 1)
        scroll_layout.addWidget(group_paths)

        # 2. Performance
        group_perf = QGroupBox("Performance")
        layout_perf = QGridLayout()
        group_perf.setLayout(layout_perf)
        self.add_input_pair(layout_perf, 0, 0, "MAIN_THREADS", "Game Threads:")
        self.add_input_pair(layout_perf, 0, 2, "SUB_THREAD_WORKERS", "ROM Threads:")
        scroll_layout.addWidget(group_perf)

        # 3. Network
        group_net = QGroupBox("Network")
        layout_net = QGridLayout()
        group_net.setLayout(layout_net)
        self.add_input_pair(layout_net, 0, 0, "TIMEOUT", "Timeout (s):")
        self.add_input_pair(layout_net, 0, 2, "MAX_RETRIES", "Max Retries:")
        self.add_input_pair(layout_net, 0, 4, "RETRY_DELAY", "Retry (s):")
        scroll_layout.addWidget(group_net)

        # 4. Scraping
        group_scrape = QGroupBox("Scraping & Logic")
        layout_scrape = QGridLayout()
        group_scrape.setLayout(layout_scrape)
        self.add_input_pair(layout_scrape, 0, 0, "FUZZY_THRESHOLD", "Fuzzy (0-1):")
        self.add_input_pair(layout_scrape, 1, 0, "RECURSION_DEPTH", "Recurse:")
        self.add_checkbox(layout_scrape, 0, 2, "DELETE_EMPTY_DATS", "Delete Empty DATs")
        self.add_checkbox(layout_scrape, 1, 2, "INTERACTIVE_MODE", "Interactive Mode")
        scroll_layout.addWidget(group_scrape)

        # 5. Reporting
        group_report = QGroupBox("Reporting")
        layout_report = QGridLayout()
        group_report.setLayout(layout_report)
        self.add_checkbox(layout_report, 0, 0, "SHOW_NONE_DATS", "Show 'None' Skipped")
        self.add_checkbox(layout_report, 0, 1, "SHOW_PROCESSED_DATS", "Show Processed")
        self.add_checkbox(layout_report, 0, 2, "SHOW_DELETED_DATS", "Show Deleted")
        scroll_layout.addWidget(group_report)

        # 6. Permissions
        group_perm = QGroupBox("Permissions (Octal)")
        layout_perm = QGridLayout()
        group_perm.setLayout(layout_perm)
        f_val = oct(CONFIG["FOLDER_PERMISSIONS"]).replace("0o", "")
        self.add_input_pair(layout_perm, 0, 0, "FOLDER_PERMISSIONS", "Folder:", val_override=f_val)
        p_val = oct(CONFIG["FILE_PERMISSIONS"]).replace("0o", "")
        self.add_input_pair(layout_perm, 0, 2, "FILE_PERMISSIONS", "File:", val_override=p_val)
        scroll_layout.addWidget(group_perm)

        # --- DOWNLOAD QUEUE TABLE ---
        lbl_queue = QLabel("Active Downloads:")
        lbl_queue.setStyleSheet("font-weight: bold; margin-top: 5px;")
        main_layout.addWidget(lbl_queue)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Filename", "Progress", "Speed"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)     
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)       
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)       
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 100)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # CHANGED: Use minimum height so it can grow
        self.table.setMinimumHeight(100) 
        main_layout.addWidget(self.table)
        
        self.active_rows = {}

        # --- LOG BOX SECTION ---
        lbl_log = QLabel("Process Log:")
        lbl_log.setStyleSheet("font-weight: bold; margin-top: 5px;")
        main_layout.addWidget(lbl_log)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        # CHANGED: Use minimum height so it can grow
        self.log_box.setMinimumHeight(80)
        self.log_box.setFont(QFont("Consolas", 9))
        self.log_box.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; border: 1px solid #3c3c3c;")
        main_layout.addWidget(self.log_box)

        # --- ACTION BUTTON ---
        self.btn_start = QPushButton("SAVE SETTINGS && START")
        self.btn_start.setFixedHeight(45)
        self.btn_start.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.btn_start.clicked.connect(self.start_process)
        main_layout.addWidget(self.btn_start)

    # --- WIDGET HELPERS ---
    def add_path_row(self, layout, row, key, label_text, mode="file"):
        lbl = QLabel(label_text)
        inp = QLineEdit()
        inp.setText(str(CONFIG.get(key, "")))
        self.inputs[key] = inp
        
        layout.addWidget(lbl, row, 0)
        layout.addWidget(inp, row, 1)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0,0,0,0)
        
        if mode == "both":
            btn_file = QPushButton("File")
            btn_folder = QPushButton("Folder")
            btn_file.clicked.connect(lambda: self.browse(inp, "file"))
            btn_folder.clicked.connect(lambda: self.browse(inp, "dir"))
            btn_layout.addWidget(btn_file)
            btn_layout.addWidget(btn_folder)
        else:
            btn = QPushButton("...")
            btn.clicked.connect(lambda: self.browse(inp, mode))
            btn_layout.addWidget(btn)
        
        container = QWidget()
        container.setLayout(btn_layout)
        layout.addWidget(container, row, 2)

    def add_input_pair(self, layout, row, col, key, label_text, val_override=None):
        lbl = QLabel(label_text)
        inp = QLineEdit()
        val = val_override if val_override is not None else str(CONFIG.get(key, ""))
        inp.setText(val)
        self.inputs[key] = inp
        layout.addWidget(lbl, row, col)
        layout.addWidget(inp, row, col+1)

    def add_checkbox(self, layout, row, col, key, label_text):
        chk = QCheckBox(label_text)
        chk.setChecked(bool(CONFIG.get(key, False)))
        self.inputs[key] = chk
        layout.addWidget(chk, row, col)

    def browse(self, input_widget, mode):
        if mode == "file":
            path, _ = QFileDialog.getOpenFileName(self, "Select File")
        else:
            path = QFileDialog.getExistingDirectory(self, "Select Folder")
        if path: input_widget.setText(path)

    def open_domain_filter(self):
        mapping_path = self.inputs["URL_MAPPING_FILE"].text()
        if not mapping_path or not os.path.exists(mapping_path):
             QMessageBox.critical(self, "Error", "URL mapping file not found. Please select a valid url_mapping.txt first.")
             return
        dlg = DomainFilterDialog(self, mapping_path)
        dlg.exec_()

    def save_settings(self):
        try:
            CONFIG["INPUT_PATH"] = self.inputs["INPUT_PATH"].text()
            CONFIG["DOWNLOAD_FOLDER"] = self.inputs["DOWNLOAD_FOLDER"].text()
            CONFIG["URL_MAPPING_FILE"] = self.inputs["URL_MAPPING_FILE"].text()
            CONFIG["MAIN_THREADS"] = int(self.inputs["MAIN_THREADS"].text())
            CONFIG["SUB_THREAD_WORKERS"] = int(self.inputs["SUB_THREAD_WORKERS"].text())
            CONFIG["TIMEOUT"] = int(self.inputs["TIMEOUT"].text())
            CONFIG["MAX_RETRIES"] = int(self.inputs["MAX_RETRIES"].text())
            CONFIG["RETRY_DELAY"] = int(self.inputs["RETRY_DELAY"].text())
            CONFIG["FUZZY_THRESHOLD"] = float(self.inputs["FUZZY_THRESHOLD"].text())
            CONFIG["RECURSION_DEPTH"] = int(self.inputs["RECURSION_DEPTH"].text())
            CONFIG["DELETE_EMPTY_DATS"] = self.inputs["DELETE_EMPTY_DATS"].isChecked()
            CONFIG["INTERACTIVE_MODE"] = self.inputs["INTERACTIVE_MODE"].isChecked()
            CONFIG["SHOW_NONE_DATS"] = self.inputs["SHOW_NONE_DATS"].isChecked()
            CONFIG["SHOW_PROCESSED_DATS"] = self.inputs["SHOW_PROCESSED_DATS"].isChecked()
            CONFIG["SHOW_DELETED_DATS"] = self.inputs["SHOW_DELETED_DATS"].isChecked()
            CONFIG["FOLDER_PERMISSIONS"] = int(self.inputs["FOLDER_PERMISSIONS"].text(), 8)
            CONFIG["FILE_PERMISSIONS"] = int(self.inputs["FILE_PERMISSIONS"].text(), 8)

            if not CONFIG["INPUT_PATH"] or not CONFIG["DOWNLOAD_FOLDER"] or not CONFIG["URL_MAPPING_FILE"]:
                 QMessageBox.critical(self, "Error", "Please verify all paths are set.")
                 return False

            sz = self.size()
            CONFIG["GUI_GEOMETRY"] = f"{sz.width()}x{sz.height()}"
            save_config()
            return True
        except ValueError as e:
            QMessageBox.critical(self, "Input Error", f"Invalid value: {e}")
            return False

    def start_process(self):
        # 1. Save Settings
        if not self.save_settings():
            return

        # 2. UI Prep
        self.btn_start.setEnabled(False)
        self.btn_start.setText("PROCESSING...")
        self.log_box.clear()
        self.table.setRowCount(0)
        self.active_rows.clear()
        
        # 3. Initialize Global Signals
        global GUI_SIGNALS
        GUI_SIGNALS = WorkerSignals()
        GUI_SIGNALS.progress.connect(self.update_progress_row)
        GUI_SIGNALS.finished.connect(self.remove_progress_row)
        GUI_SIGNALS.input_request.connect(self.handle_input_request)

        # 4. Redirect Output
        self.redirector = StreamRedirector()
        self.redirector.text_written.connect(self.update_log)
        
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        sys.stdout = self.redirector
        sys.stderr = self.redirector

        # 5. Start Thread
        self.worker = DownloadWorker()
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def update_log(self, text):
        # ANSI Color Parsing
        ansi_map = {
            '30': 'gray', '31': '#ff5555', '32': '#50fa7b', '33': '#f1fa8c', 
            '34': '#bd93f9', '35': '#ff79c6', '36': '#8be9fd', '37': 'white', '0': 'white'
        }

        # Regex split by ANSI codes
        parts = re.split(r'\x1b\[(\d+)m', text)
        cursor = self.log_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        current_fmt = cursor.charFormat()
        
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part: cursor.insertText(part, current_fmt)
            else:
                code = part.split(';')[0]
                if code in ansi_map:
                    current_fmt.setForeground(QColor(ansi_map[code]))
                elif code == '0':
                    current_fmt.setForeground(QColor("white"))

        self.log_box.ensureCursorVisible()

    def update_progress_row(self, filename, percent, speed_str):
        if filename not in self.active_rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.active_rows[filename] = row
            
            self.table.setItem(row, 0, QTableWidgetItem(filename))
            
            pbar = QProgressBar()
            pbar.setValue(percent)
            pbar.setAlignment(Qt.AlignCenter)
            pbar.setStyleSheet("""
                QProgressBar { border: 1px solid #555; border-radius: 3px; text-align: center; }
                QProgressBar::chunk { background-color: #2a82da; width: 10px; }
            """)
            self.table.setCellWidget(row, 1, pbar)
            self.table.setItem(row, 2, QTableWidgetItem(speed_str))
        else:
            row = self.active_rows[filename]
            pbar = self.table.cellWidget(row, 1)
            if pbar: pbar.setValue(percent)
            self.table.setItem(row, 2, QTableWidgetItem(speed_str))

    def remove_progress_row(self, filename):
        if filename in self.active_rows:
            row = self.active_rows[filename]
            pbar = self.table.cellWidget(row, 1)
            if pbar: 
                pbar.setValue(100)
                pbar.setStyleSheet("""
                    QProgressBar { border: 1px solid #555; border-radius: 3px; text-align: center; }
                    QProgressBar::chunk { background-color: #50fa7b; } 
                """) 
            self.table.setItem(row, 2, QTableWidgetItem("Done"))

    def on_worker_finished(self):
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        self.btn_start.setEnabled(True)
        self.btn_start.setText("SAVE SETTINGS && START")
        QMessageBox.information(self, "Done", "Process Complete.")

    def handle_input_request(self, dat_name, event, container):
            text, ok = QInputDialog.getText(self, "Missing URL Mapping", 
                                            f"The following DAT has no URL mapped:\n\n{dat_name}\n\nEnter source URL (or leave empty to skip):", 
                                            QLineEdit.Normal, "")
            if ok and text.strip():
                container["text"] = text.strip()
            else:
                container["text"] = "none"
                
            event.set()

def set_dark_theme(app):
    app.setStyle("Fusion")
    dark_palette = QPalette()
    dark_color = QColor(45, 45, 45)
    
    dark_palette.setColor(QPalette.Window, dark_color)
    dark_palette.setColor(QPalette.WindowText, Qt.white)
    dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
    dark_palette.setColor(QPalette.AlternateBase, dark_color)
    dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
    dark_palette.setColor(QPalette.ToolTipText, Qt.white)
    dark_palette.setColor(QPalette.Text, Qt.white)
    dark_palette.setColor(QPalette.Button, dark_color)
    dark_palette.setColor(QPalette.ButtonText, Qt.white)
    dark_palette.setColor(QPalette.BrightText, Qt.red)
    dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.HighlightedText, Qt.black)

    app.setPalette(dark_palette)
    app.setStyleSheet("QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }")

def main():
    load_config()

    if len(sys.argv) > 1:
        # CLI Mode
        parser = argparse.ArgumentParser(description="DAT File Downloader/Scraper", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        parser.add_argument('input_path', help="Path to DAT file or folder containing DATs")
        parser.add_argument('--output', default=CONFIG["DOWNLOAD_FOLDER"], help="Download destination folder")
        parser.add_argument('--mapping-file', default=CONFIG["URL_MAPPING_FILE"], help="Path to the URL mapping file")
        parser.add_argument('--interactive', action='store_true', help="Ask for missing URLs instead of skipping")
        parser.add_argument('--threads', type=int, default=CONFIG["MAIN_THREADS"], help="Number of concurrent game download threads")
        parser.add_argument('--sub-threads', type=int, default=CONFIG["SUB_THREAD_WORKERS"], help="Number of concurrent file downloads per game")
        parser.add_argument('--depth', type=int, default=CONFIG["RECURSION_DEPTH"], help="Recursion depth for crawling subfolders")
        parser.add_argument('--fuzzy', type=float, default=CONFIG["FUZZY_THRESHOLD"], help="Fuzzy match threshold (0.0 - 1.0)")
        parser.add_argument('--keep-empty', action='store_true', help="Do not delete DAT files that have no ROMs/MIA")
        parser.add_argument('--timeout', type=int, default=CONFIG["TIMEOUT"], help="Request timeout in seconds")
        parser.add_argument('--retries', type=int, default=CONFIG["MAX_RETRIES"], help="Max retries per request")
        parser.add_argument('--retry-delay', type=int, default=CONFIG["RETRY_DELAY"], help="Delay between retries in seconds")
        parser.add_argument('--folder-perm', type=lambda x: int(x, 8), default=CONFIG["FOLDER_PERMISSIONS"], help="Folder permissions in octal (e.g. 777)")
        parser.add_argument('--file-perm', type=lambda x: int(x, 8), default=CONFIG["FILE_PERMISSIONS"], help="File permissions in octal (e.g. 666)")
        parser.add_argument('--debug', action='store_true', help="Enable debug logging")
        parser.add_argument('--show-none', action='store_true', help="Show list of DATs skipped due to 'none' mapping")
        parser.add_argument('--show-processed', action='store_true', help="Show list of processed DATs at the end")
        parser.add_argument('--show-deleted', action='store_true', help="Show list of deleted DATs at the end")

        args = parser.parse_args()

        CONFIG["INPUT_PATH"] = args.input_path
        CONFIG["DOWNLOAD_FOLDER"] = args.output
        CONFIG["URL_MAPPING_FILE"] = args.mapping_file
        CONFIG["TIMEOUT"] = args.timeout
        CONFIG["MAX_RETRIES"] = args.retries
        CONFIG["RETRY_DELAY"] = args.retry_delay
        CONFIG["DELETE_EMPTY_DATS"] = not args.keep_empty
        CONFIG["FOLDER_PERMISSIONS"] = args.folder_perm
        CONFIG["FILE_PERMISSIONS"] = args.file_perm
        CONFIG["SUB_THREAD_WORKERS"] = args.sub_threads
        CONFIG["FUZZY_THRESHOLD"] = args.fuzzy
        CONFIG["RECURSION_DEPTH"] = args.depth
        CONFIG["MAIN_THREADS"] = args.threads
        CONFIG["INTERACTIVE_MODE"] = args.interactive
        CONFIG["SHOW_NONE_DATS"] = args.show_none
        CONFIG["SHOW_PROCESSED_DATS"] = args.show_processed
        CONFIG["SHOW_DELETED_DATS"] = args.show_deleted

        if args.debug: log.setLevel(logging.DEBUG)

        run_downloader()
    else:
        # GUI Mode
        if hasattr(Qt, 'AA_EnableHighDpiScaling'):
            QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
            QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

        app = QApplication(sys.argv)
        set_dark_theme(app)

        window = ConfigWindow()
        window.show()
        
        sys.exit(app.exec_())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        print("\n" + "!" * 60)
        print(" [CRITICAL ERROR] The script crashed unexpectedly.")
        print(" See the error trace above for details.")
        print("!" * 60)
        sys.exit(1)
    finally:
        if len(sys.argv) == 1:
            # If CLI mode, pause; if GUI mode, just exit.
            pass
        else:
            print(f"\n{Fore.RED}Press Enter to exit (auto-closing in 10 seconds)...{Style.RESET_ALL}")
            try:
                import msvcrt
                start_time = time.time()
                while time.time() - start_time < 10:
                    if msvcrt.kbhit():
                        msvcrt.getch()
                        break
                    time.sleep(0.1)
            except ImportError:
                pass     
            os._exit(0)