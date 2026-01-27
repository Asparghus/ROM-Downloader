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
from urllib.parse import urljoin, urlparse, unquote, urlunparse
from difflib import get_close_matches
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- GUI IMPORTS ---
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- DEPENDENCY CHECK (Skipped if frozen by PyInstaller) ---
def check_dependencies():
    """Checks for required third-party libraries."""
    # If running as compiled exe, skip check
    if getattr(sys, 'frozen', False):
        return

    required_packages = [
        ("requests", "requests"),
        ("bs4", "beautifulsoup4"),
        ("colorama", "colorama"),
        ("tqdm", "tqdm")
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

init(autoreset=True)

# --- GLOBAL CONFIGURATION (Populated by Arguments or GUI) ---
CONFIG = {
    "INPUT_PATH": "", # Added for GUI context
    "URL_MAPPING_FILE": "url_mapping.txt",
    "DOWNLOAD_FOLDER": os.path.join(os.getcwd(), "Downloads"), # Default to local folder
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
    "MAIN_THREADS": 4
}

# --- CONFIG PERSISTENCE ---
CONFIG_FILE = "config.json"

def load_config():
    """Loads settings from config.json if it exists, overriding defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved_config = json.load(f)
                # Update GLOBAL CONFIG with saved values
                CONFIG.update(saved_config)
        except Exception as e:
            log.warning(f"{Fore.YELLOW}[WARN] Could not load config file: {e}{Style.RESET_ALL}")

def save_config():
    """Saves the current global CONFIG to config.json."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(CONFIG, f, indent=4)
    except Exception as e:
        log.error(f"{Fore.RED}[ERROR] Could not save config file: {e}{Style.RESET_ALL}")

# --- INITIALIZE CONFIG ---
# Call this immediately after the CONFIG dictionary definition in your code
load_config() 

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
                links.update(sub_links)

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

    try:
        with open(temp_path, "wb") as file:
            with tqdm(total=total_size, unit='B', unit_scale=True, desc=f"{Fore.BLUE}{file_display[:20]:<20}{Style.RESET_ALL}", leave=False, ncols=100) as bar:
                for chunk in response.iter_content(chunk_size=32768):
                    if chunk:
                        file.write(chunk)
                        bar.update(len(chunk))

        if total_size > 0 and os.path.getsize(temp_path) != total_size:
            raise IOError("Incomplete download")
        
        os.rename(temp_path, destination_path)
        _set_path_permissions(destination_path, CONFIG["FILE_PERMISSIONS"])
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
    matches = get_close_matches(target_name, candidates.keys(), n=1, cutoff=threshold)
    if matches:
        return matches[0], candidates[matches[0]]
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

def _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, interactive_mode):
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
        print(f"{Fore.YELLOW}>>> Missing URL for: {dat_name}{Style.RESET_ALL}")
        user_input = input(f"{Fore.CYAN}Enter URL (or 'none' to skip): {Style.RESET_ALL}").strip()
        if not user_input or user_input.lower() == 'none':
            url_mappings[dat_name] = ['none']
            save_url_mappings(CONFIG["URL_MAPPING_FILE"], url_mappings)
            return None
        url_mappings[dat_name] = [user_input]
        save_url_mappings(CONFIG["URL_MAPPING_FILE"], url_mappings)
        return _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, interactive_mode)

    return sorted(list(processed_urls))

# --- CORE LOGIC (Decoupled from CLI/GUI) ---
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
    
    missing_mapping_dats = []
    processed_count = 0
    deleted_count = 0

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
                deleted_count += 1
                log.info(f"{Fore.YELLOW}Deleted empty DAT.{Style.RESET_ALL}")
            else:
                log.info(f"{Fore.YELLOW}Skipped deletion (kept empty).{Style.RESET_ALL}")
            continue

        source_urls = _resolve_dat_source_urls(dat_name, url_mappings, available_links_cache, CONFIG["INTERACTIVE_MODE"])
        
        if not source_urls:
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
                    tqdm.write(msg)
                    if success: successful_games += 1
                    else: failed_games += 1
                except Exception as exc:
                    log.error(f"Thread exception: {exc}")
                    failed_games += 1

        log.info(f"{Fore.CYAN}[RESULT] {dat_name}: Success: {successful_games}, Failed: {failed_games}{Style.RESET_ALL}")
        
        if failed_games == 0 and successful_games > 0:
            log.info(f"{Fore.GREEN}DAT Complete. Deleting DAT file.{Style.RESET_ALL}")
            try: os.remove(dat_path); deleted_count += 1
            except: pass
        
        processed_count += 1

    print(f"\n{Fore.BLUE}{'='*30}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}Run Complete.{Style.RESET_ALL}")
    print(f"Processed: {processed_count}")
    print(f"Deleted: {deleted_count}")
    
    if missing_mapping_dats:
        print(f"\n{Fore.RED}The following DATs were skipped due to missing URL mappings:{Style.RESET_ALL}")
        for d in missing_mapping_dats:
            print(f" - {d}")
        print(f"{Fore.YELLOW}(Enable Interactive Mode to fix these on next run){Style.RESET_ALL}")

    print("\nPress Enter to exit...")
    input()

# --- CONFIG GUI CLASS ---
class ConfigGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("ROM Downloader Configuration")
        self.root.geometry("600x530") # Slightly shorter height needed now
        
        self.entries = {}
        
        # Main container with padding
        main_frame = ttk.Frame(root, padding="10")
        main_frame.pack(fill="both", expand=True)

        self.create_widgets(main_frame)

    def create_widgets(self, parent):
        # --- PATHS SECTION (Full Width) ---
        path_frame = ttk.LabelFrame(parent, text="Paths & Files", padding="10")
        path_frame.pack(fill="x", pady=5)
        
        self.add_path_row(path_frame, 0, "INPUT_PATH", "FixDATs Source (DAT/Folder):", mode="both")
        self.add_path_row(path_frame, 1, "DOWNLOAD_FOLDER", "Download Folder:", False)
        self.add_path_row(path_frame, 2, "URL_MAPPING_FILE", "URL Mapping File:", True)

        # --- PERFORMANCE SECTION (Grouped) ---
        perf_frame = ttk.LabelFrame(parent, text="Performance", padding="10")
        perf_frame.pack(fill="x", pady=5)
        
        self.add_small_entry(perf_frame, 0, 0, "MAIN_THREADS", "Concurrent Game/Machine/Disk Downloads:")
        self.add_small_entry(perf_frame, 0, 2, "SUB_THREAD_WORKERS", "Concurrent ROM Downloads:")

        # --- NETWORK SECTION (Grouped) ---
        net_frame = ttk.LabelFrame(parent, text="Network", padding="10")
        net_frame.pack(fill="x", pady=5)

        self.add_small_entry(net_frame, 0, 0, "TIMEOUT", "Timeout (s):")
        self.add_small_entry(net_frame, 0, 2, "MAX_RETRIES", "Max Retries:")
        self.add_small_entry(net_frame, 0, 4, "RETRY_DELAY", "Retry Delay (s):")

        # --- SCRAPING & LOGIC (Grouped) ---
        scrape_frame = ttk.LabelFrame(parent, text="Scraping & Logic", padding="10")
        scrape_frame.pack(fill="x", pady=5)

        self.add_small_entry(scrape_frame, 0, 0, "FUZZY_THRESHOLD", "Fuzzy Match (0-1):")
        self.add_small_entry(scrape_frame, 0, 2, "RECURSION_DEPTH", "Recurse Depth:")
        
        # Checkboxes on the next row within the same frame
        self.add_checkbox(scrape_frame, 1, 0, "DELETE_EMPTY_DATS", "Delete Empty DATs")
        self.add_checkbox(scrape_frame, 1, 2, "INTERACTIVE_MODE", "Interactive Mode")

        # --- PERMISSIONS (Grouped) ---
        perm_frame = ttk.LabelFrame(parent, text="Permissions (Octal)", padding="10")
        perm_frame.pack(fill="x", pady=5)
        
        # Format octals for display
        f_val = oct(CONFIG["FOLDER_PERMISSIONS"]).replace("0o", "")
        self.add_small_entry(perm_frame, 0, 0, "FOLDER_PERMISSIONS", "Folder (e.g. 777):", val_override=f_val)
        
        p_val = oct(CONFIG["FILE_PERMISSIONS"]).replace("0o", "")
        self.add_small_entry(perm_frame, 0, 2, "FILE_PERMISSIONS", "File (e.g. 666):", val_override=p_val)

        # --- ACTION BUTTON ---
        ttk.Button(parent, text="SAVE SETTINGS & START", command=self.save_and_run).pack(pady=20, fill='x')

    # --- GUI HELPERS ---

    def add_path_row(self, parent, row, key, label, mode="file"):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=5, pady=2)
        entry = ttk.Entry(parent)
        entry.insert(0, str(CONFIG[key]))
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        parent.columnconfigure(1, weight=1) 
        
        button_frame = ttk.Frame(parent)
        button_frame.grid(row=row, column=2, padx=2)

        def browse_file():
            res = filedialog.askopenfilename()
            if res:
                entry.delete(0, tk.END)
                entry.insert(0, res)

        def browse_folder():
            res = filedialog.askdirectory()
            if res:
                entry.delete(0, tk.END)
                entry.insert(0, res)

        if mode == "both":
            # Two small buttons for File or Folder
            ttk.Button(button_frame, text="File", width=5, command=browse_file).pack(side="left", padx=1)
            ttk.Button(button_frame, text="Folder", width=6, command=browse_folder).pack(side="left", padx=1)
        else:
            # Single button logic
            cmd = browse_file if mode == True or mode == "file" else browse_folder
            ttk.Button(button_frame, text="...", width=3, command=cmd).pack()

        self.entries[key] = entry

    def add_small_entry(self, parent, row, col, key, label, val_override=None):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="e", padx=(10, 5), pady=2)
        entry = ttk.Entry(parent, width=8) # Small width
        val = val_override if val_override is not None else str(CONFIG[key])
        entry.insert(0, val)
        entry.grid(row=row, column=col+1, sticky="w", padx=0, pady=2)
        self.entries[key] = entry

    def add_checkbox(self, parent, row, col, key, label):
        var = tk.BooleanVar(value=CONFIG[key])
        ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=col, columnspan=2, sticky="w", padx=10, pady=2)
        self.entries[key] = var

    def save_and_run(self):
        try:
            # Update CONFIG from GUI
            CONFIG["INPUT_PATH"] = self.entries["INPUT_PATH"].get()
            CONFIG["DOWNLOAD_FOLDER"] = self.entries["DOWNLOAD_FOLDER"].get()
            CONFIG["URL_MAPPING_FILE"] = self.entries["URL_MAPPING_FILE"].get()
            
            CONFIG["MAIN_THREADS"] = int(self.entries["MAIN_THREADS"].get())
            CONFIG["SUB_THREAD_WORKERS"] = int(self.entries["SUB_THREAD_WORKERS"].get())
            CONFIG["TIMEOUT"] = int(self.entries["TIMEOUT"].get())
            CONFIG["MAX_RETRIES"] = int(self.entries["MAX_RETRIES"].get())
            CONFIG["RETRY_DELAY"] = int(self.entries["RETRY_DELAY"].get())
            CONFIG["FUZZY_THRESHOLD"] = float(self.entries["FUZZY_THRESHOLD"].get())
            CONFIG["RECURSION_DEPTH"] = int(self.entries["RECURSION_DEPTH"].get())
            
            CONFIG["DELETE_EMPTY_DATS"] = self.entries["DELETE_EMPTY_DATS"].get()
            CONFIG["INTERACTIVE_MODE"] = self.entries["INTERACTIVE_MODE"].get()

            # Handle Octal Inputs
            CONFIG["FOLDER_PERMISSIONS"] = int(self.entries["FOLDER_PERMISSIONS"].get(), 8)
            CONFIG["FILE_PERMISSIONS"] = int(self.entries["FILE_PERMISSIONS"].get(), 8)

            if not CONFIG["INPUT_PATH"]:
                messagebox.showerror("Error", "Please select a FixDAT file/folder.")
                return

            if not CONFIG["DOWNLOAD_FOLDER"]:
                messagebox.showerror("Error", "Please select a Download Path.")
                return

            if not CONFIG["URL_MAPPING_FILE"]:
                messagebox.showerror("Error", "Please select a path for url_mapping.txt.")
                return

            # SAVE CONFIG TO DISK
            save_config()

            self.root.destroy()
            run_downloader()

        except ValueError as e:
            messagebox.showerror("Input Error", f"Invalid value: {e}")

def main():
    # Load saved config first
    load_config()

    # If arguments are passed, use CLI mode
    if len(sys.argv) > 1:
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

        args = parser.parse_args()

        # Override Config with Args
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

        if args.debug: log.setLevel(logging.DEBUG)

        run_downloader()
    else:
        # No arguments -> Launch GUI
        root = tk.Tk()
        app = ConfigGUI(root)
        root.mainloop()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
