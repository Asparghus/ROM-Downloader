# ROM Downloader & DAT Scraper

A robust, multi-threaded tool to download ROMs based on DAT files (e.g., Redump, No-Intro, etc). It automatically scrapes **Archive.org** for the files, validates them against your DATs, and organizes them into folders.

## Features

* **GUI & CLI:** Easy-to-use graphical interface for configuration, or a command-line interface for automation.
* **Auto-Scraping:** Crawls Archive.org URLs (directories, "Details" pages, or user profiles) to find matching files.
* **Fuzzy Matching:** Finds files even if the naming convention on the server differs slightly from the DAT file.
* **Persistent Config:** Automatically saves your settings to `config.json` so you don't have to re-enter them.
* **Multi-threaded:** Fast parallel downloads (configurable amount of threads).
* **Smart Parsing:** Handles XML DAT files, detects MIA (Missing in Action) files, and ignores them.
* **Recursive Crawling:** Scans nested folder structures on remote servers (configurable amount of sub-directories).
* **Progress Tracking:** Real-time progress bars with colorized console output.
* **URL_Mapping Source Filter (Disable sources) 

## Installation

### Option A: Standalone Executable
1. Navigate to the [Releases](../../releases) page of this repository.
2. Download the latest `ROM_Downloader.exe`.
3. Run it directly (no Python installation required).

### Option B: Run from Source
1. Install Python 3.7+.
2. Clone this repository.
3. Install dependencies:
   ```bash
   pip install requests beautifulsoup4 colorama tqdm
   ```
4. Run the script:
   ```bash
   python rom_downloader.py
   ```

## Usage

### 1. GUI Mode (Default)
Simply run the executable or script without any arguments.
* A configuration window will appear.
* Select your **Source** (DAT file or folder) and **Download Folder**.
* Adjust settings (Threads, Timeout, etc.).
* Click **"SAVE SETTINGS & START"**.
* The GUI will close, and a console window will open to show progress bars.

### 2. CLI Mode (Automation)
You can run the tool via command line. It will load defaults from `config.json` but allows overrides via flags.

```bash
# Basic usage
python rom_downloader.py "C:\Path\To\DATs"

# Override download folder and threads
python rom_downloader.py "C:\ROMs\DATs" --output "D:\Games" --threads 8

# Interactive mode with custom settings
python rom_downloader.py ./dats --interactive --threads 8 --fuzzy 0.95

# Keep empty DATs and enable debug logging
python rom_downloader.py ./dats --keep-empty --debug
```

## Configuration Files

### `url_mapping.txt`
The tool needs to know where to look for files for each DAT. It will will create a text file named `url_mapping.txt` in the same folder as the executable when you run it for the first time with interactive mode on.
It will ask you for a link for each new dat it finds. Those links will be saved and used when the script is run again.

**Format:** `DAT Name="URL"`

```
# Example Mapping File
No-Intro - Console="https://archive.org/download/console-collection"
Redump - Video Game="https://archive.org/details/video_game_redump_usa"
TOSEC Computer=none
```

* **Key:** The "name" inside the DAT file "header" (or the filename without extension and date).
* **Value:** The direct HTTP link to the directory listing, Archive.org "Details" page, or user profile (`@username`).
* Use `none` to skip a DAT entirely.
* Supports multiple URLs per DAT (one per line with the same name).
* Archive.org profile URLs (`@username`) are automatically expanded to find all collections.

### `config.json` & CLI Arguments

This `config.json` file is generated automatically when you save settings in the GUI. You can also edit it manually or override values via command-line arguments.

**CLI Usage:** `python rom_downloader.py <input_path> [options]`

| Config Key | CLI Argument | Description | Default |
|------------|--------------|-------------|---------|
| `INPUT_PATH` | `input_path` (positional) | DAT file or folder containing DATs (required) | - |
| `DOWNLOAD_FOLDER` | `--output` | Download destination folder | `./Downloads` |
| `URL_MAPPING_FILE` | `--mapping-file` | Path to URL mapping file | `url_mapping.txt` |
| `INTERACTIVE_MODE` | `--interactive` | Prompt for missing URLs instead of skipping | false |
| `MAIN_THREADS` | `--threads` | Number of games to process simultaneously | 4 |
| `SUB_THREAD_WORKERS` | `--sub-threads` | Number of file downloads per game | 3 |
| `RECURSION_DEPTH` | `--depth` | How many subfolders deep to scan | 2 |
| `FUZZY_THRESHOLD` | `--fuzzy` | Filename matching sensitivity (0.0 - 1.0) | 0.90 |
| `DELETE_EMPTY_DATS` | `--keep-empty` | Don't delete empty DATs (flag inverts config) | true |
| `TIMEOUT` | `--timeout` | Network timeout in seconds | 60 |
| `MAX_RETRIES` | `--retries` | Max retry attempts per request | 3 |
| `RETRY_DELAY` | `--retry-delay` | Delay between retries in seconds | 5 |
| `FOLDER_PERMISSIONS` | `--folder-perm` | Folder permissions in octal (e.g., 777) | 0o777 |
| `FILE_PERMISSIONS` | `--file-perm` | File permissions in octal (e.g., 666) | 0o666 |
| - | `--debug` | Enable debug logging | false |

## DAT File Format

Supports standard XML DAT files with `<game>` or `<machine>` elements:

```xml
<datafile>
  <header>
    <name>Video Game Collection</name>
  </header>
  <game name="Game VII">
    <rom name="Game VII (USA)"/>
  </game>
  <game name="Racing Game">
    <disk name="Racing Game (USA)"/>
  </game>
</datafile>
```

* Skips games marked with `mia="yes"` (missing in action)
* Handles both ROM and disk-based games

## How It Works

1. **Parse DAT**: Extracts game names and required files from XML
2. **Resolve URLs**: Maps DAT name to download sources via `url_mapping.txt`
3. **Crawl Sources**: Recursively scans remote directories and indexes available files
4. **Match Files**: Uses exact or fuzzy matching to find required files
5. **Download**: Multi-threaded downloads with progress tracking and automatic retries
6. **Verify**: Checks file sizes and integrity
7. **Cleanup**: Removes completed DATs (optional)

## Output Structure

```
Downloads/
├── Video Game Collection/
│   ├── Game VII (USA).bin
│   ├── Game VII (USA).cue
│   └── Racing Game (USA).chd
└── Console Collection/
    ├── Plataform Game (USA).zip
    └── RPG Game (USA).zip
```

## Building from Source

To build the `.exe` yourself using PyInstaller:

1. Install PyInstaller:
   ```bash
   pip install pyinstaller
   ```

2. Run the build command:
   ```bash
   pyinstaller --onefile --name "ROM_Downloader" rom_downloader.py
   ```

3. The output file will be located in the `dist` folder.

## Notes

* Archive.org URLs are automatically converted to download endpoints
* Profile URLs (`@username`) are crawled for all available collections
* Existing files are skipped (previously download to the output folder)
* Color-coded console output for easy status tracking

## License

This tool is provided as-is for personal use. Ensure you have the legal right to download any content.

## Troubleshooting

**Missing dependencies**: Run `pip install requests beautifulsoup4 colorama tqdm`

**Permission errors**: Adjust `FOLDER_PERMISSIONS` and `FILE_PERMISSIONS` settings

**Timeouts**: Increase `TIMEOUT` value or reduce `MAIN_THREADS`

**No matches found**: Lower `FUZZY_THRESHOLD` or check URL mapping file

**Rate limiting**: Reduce thread counts or add delays between requests
