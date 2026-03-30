import time
import requests
import socket
import platform
import psutil
import subprocess
import json
import os
import tempfile
import xml.etree.ElementTree as ET
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import logging
import sys
import argparse

# ============================================================================
# VERSION & UPDATE CONFIGURATION
# ============================================================================
AGENT_VERSION = "1"  # Optimized recursive file scan with limits
UPDATE_CHECK_URL = "http://192.168.105.145:8069/api/agent/version"
UPDATE_DOWNLOAD_URL = "http://192.168.105.145:8069/downloads/AssetAgent_latest.exe"
UPDATE_CHECK_INTERVAL = 60

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
log_dir = os.path.join(os.environ.get('PROGRAMDATA', 'C:\\ProgramData'), 'AssetAgent')
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'asset_agent.log')

# PyInstaller no-console mode and Windows service safety
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r')

log_handlers = [
    logging.FileHandler(log_file_path, encoding='utf-8'),
    logging.StreamHandler(sys.stdout)
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# ============================================================================
# ODOO API CONFIGURATION
# ============================================================================
ODOO_API_URL = "http://192.168.105.145:8069/api/laptop_monitor"
ODOO_DB_NAME = "odoo19"  # Must match db_name in odoo.conf
ODOO_HEADERS = {
    "Content-Type": "application/json",
    "X-Odoo-Database": ODOO_DB_NAME,
}
STATIC_SYNC_INTERVAL = 60
LIVE_SYNC_INTERVAL = 30
SHOW_UI = False

# ============================================================================
# WINDOWS UPDATE CONFIGURATION
# ============================================================================
WINDOWS_UPDATE_BASE_URL = "http://192.168.105.145:8069/api/asset/updates"
WINDOWS_UPDATE_SYNC_INTERVAL = 60  # 1 minute for testing (change to 300 for production)

# ============================================================================
# FILE ACCESS POLICY CONFIGURATION
# ============================================================================
FILE_ACCESS_BASE_URL      = "http://192.168.105.145:8069/api/asset/file_access"
FILE_ACCESS_SYNC_INTERVAL = 60   # Poll policy from Odoo every 60 seconds

def get_monitored_folders():
    """Return Desktop, Documents, Downloads paths for current user."""
    home = os.path.expanduser("~")
    return [
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
    ]

# ============================================================================
# ANTIVIRUS DEPLOYMENT CONFIGURATION
# ============================================================================
ANTIVIRUS_BASE_URL = "http://192.168.105.145:8069/api/antivirus"
ANTIVIRUS_POLL_INTERVAL = 30  # Poll every 30 seconds

# ============================================================================
# SOFTWARE DEPLOYMENT CONFIGURATION
# ============================================================================
SOFTWARE_BASE_URL = "http://192.168.105.145:8069/api/asset/software"
SOFTWARE_SYNC_INTERVAL = 30  # Poll every 30 seconds

# ============================================================================
# APP DEPLOYMENT CONFIGURATION (package-manager-based)
# ============================================================================
APP_DEPLOY_BASE_URL = "http://192.168.105.145:8069/asset_management/api/agent"
APP_DEPLOY_POLL_INTERVAL = 30  # Poll every 30 seconds

# ============================================================================
# FOLDER LOCK CONFIGURATION
# ============================================================================


# ============================================================================
# ENTERPRISE FILESYSTEM INVENTORY CONFIGURATION
# ============================================================================

# Whether to include hidden files (starting with dot or hidden attribute)
EXTRA_FILES_INCLUDE_HIDDEN = False

# ============================================================================
# GLOBAL CACHES AND LOCKS
# ============================================================================
_cached_serial_number = None
_cached_device_model = None
_cached_graphics_card = None
_cached_location = None
_last_location_fetch = 0
_cache_lock = threading.Lock()
_static_sync_lock = threading.Lock()
_live_sync_lock = threading.Lock()
_windows_update_lock = threading.Lock()

FILE_BROWSER_PORT = 8000


# ============================================================================
# AUTO-UPDATE FUNCTIONS
# ============================================================================

def check_for_updates():
    """Check if a new version is available"""
    try:
        logger.info(f"Checking for updates (current version: {AGENT_VERSION})")
        response = requests.get(UPDATE_CHECK_URL, timeout=10, params={"current_version": AGENT_VERSION}, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data = response.json()
            latest_version = data.get("latest_version", AGENT_VERSION)
            download_url = data.get("download_url", UPDATE_DOWNLOAD_URL)

            def parse_version(v):
                return tuple(map(int, v.split('.')))

            if parse_version(latest_version) > parse_version(AGENT_VERSION):
                logger.info(f"INFO New version available: {latest_version}")
                return True, latest_version, download_url
            else:
                logger.info("OK Agent is up to date")
                return False, None, None
        else:
            logger.warning(f"Update check failed: HTTP {response.status_code}")
            return False, None, None
    except Exception as e:
        logger.warning(f"Error checking for updates: {e}")
        return False, None, None


def download_update(download_url):
    """Download the new agent version"""
    try:
        temp_dir = tempfile.gettempdir()
        update_file = os.path.join(temp_dir, "AssetAgent_update.exe")
        logger.info(f"Downloading update from {download_url}")
        response = requests.get(download_url, stream=True, timeout=300)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        with open(update_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and downloaded % (1024 * 1024) == 0:
                        progress = (downloaded / total_size) * 100
                        logger.info(f"Download progress: {progress:.1f}%")
        logger.info(f"OK Update downloaded successfully: {update_file}")
        return update_file
    except Exception as e:
        logger.error(f"Error downloading update: {e}")
        return None


def apply_update(update_file):
    """Apply the update by launching updater and exiting"""
    try:
        if not getattr(sys, 'frozen', False):
            logger.warning("Auto-update is only supported in frozen (EXE) mode.")
            return False
        current_exe = sys.executable
        current_dir = os.path.dirname(current_exe)
        updater_exe = os.path.join(current_dir, "updater.exe")
        if not os.path.exists(updater_exe):
            logger.error(f"Updater not found at {updater_exe}")
            return False
        logger.info(f"Launching updater: {updater_exe}")
        args = [updater_exe, str(os.getpid()), current_exe, update_file]
        subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        logger.info("RESTART Launching updater and exiting current agent...")
        time.sleep(1)
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error applying update: {e}")
        return False


def update_checker_thread():
    """Background thread that periodically checks for updates"""
    logger.info(f"Update checker started (interval: {UPDATE_CHECK_INTERVAL}s)")
    time.sleep(60)
    while True:
        try:
            has_update, new_version, download_url = check_for_updates()
            if has_update:
                logger.info(f"UPDATE Updating to version {new_version}...")
                update_file = download_update(download_url)
                if update_file and os.path.exists(update_file):
                    logger.info("Applying update now...")
                    apply_update(update_file)
                    break
                else:
                    logger.error("ERROR Update download failed, will retry next interval")
            time.sleep(UPDATE_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Error in update checker: {e}")
            time.sleep(UPDATE_CHECK_INTERVAL)


# ============================================================================
# NETWORK / HTTP
# ============================================================================

def send_with_retry(url, payload, max_retries=3, timeout=30):
    """Send HTTP POST request with exponential backoff retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url, json=payload, timeout=timeout,
                headers=ODOO_HEADERS
            )
            if response.status_code == 200:
                logger.info(f"Successfully sent data to {url}")
                return True, response
            else:
                logger.warning(f"Request to {url} failed with status {response.status_code}")
                if response.status_code >= 500 and attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    logger.info(f"Retrying in {wait_time:.2f}s...")
                    time.sleep(wait_time)
                    continue
                return False, response
        except requests.exceptions.Timeout:
            logger.warning(f"Request to {url} timed out (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + (time.time() % 1))
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error to {url}: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + (time.time() % 1))
        except Exception as e:
            logger.error(f"Unexpected error sending to {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + (time.time() % 1))
    logger.error(f"Failed to send data to {url} after {max_retries} attempts")
    return False, None


class NetworkMonitor:
    def __init__(self):
        self.last_bytes_sent = 0
        self.last_bytes_recv = 0
        self.last_check_time = time.time()
        self.lock = threading.Lock()
        self._initialize()

    def _initialize(self):
        try:
            counters = psutil.net_io_counters()
            self.last_bytes_sent = counters.bytes_sent
            self.last_bytes_recv = counters.bytes_recv
            self.last_check_time = time.time()
        except:
            pass

    def get_network_usage(self):
        with self.lock:
            try:
                counters = psutil.net_io_counters()
                current_time = time.time()
                time_delta = current_time - self.last_check_time
                if time_delta < 0.1:
                    return 0, 0
                bytes_sent = counters.bytes_sent - self.last_bytes_sent
                bytes_recv = counters.bytes_recv - self.last_bytes_recv
                upload_bps = (bytes_sent * 8) / time_delta if time_delta > 0 else 0
                download_bps = (bytes_recv * 8) / time_delta if time_delta > 0 else 0
                upload_mbps = round(upload_bps / 1_000_000, 2)
                download_mbps = round(download_bps / 1_000_000, 2)
                self.last_bytes_sent = counters.bytes_sent
                self.last_bytes_recv = counters.bytes_recv
                self.last_check_time = current_time
                return upload_mbps, download_mbps
            except:
                return 0, 0


network_monitor = NetworkMonitor()


def get_local_ip():
    """Get the real LAN IP (Wi-Fi or Ethernet), skip Docker/virtual interfaces."""
    try:
        for iface_name, addrs in psutil.net_if_addrs().items():
            # Skip Docker, VMware, VirtualBox, loopback, Hyper-V interfaces
            skip_keywords = ['docker', 'veth', 'vmnet', 'virbr', 'lo', 'vbox', 'br-', 'virtual', 'hyperv', 'wsl']
            if any(kw in iface_name.lower() for kw in skip_keywords):
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    # Only return real LAN IPs — skip loopback and docker ranges
                    if ip.startswith('192.168.') or ip.startswith('10.'):
                        logger.info(f"Detected LAN IP: {ip} on interface: {iface_name}")
                        return ip
    except Exception as e:
        logger.warning(f"Error detecting LAN IP via psutil: {e}")

    # Fallback: connect to external DNS to detect outbound IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        logger.info(f"Detected IP via socket fallback: {ip}")
        return ip
    except Exception as e:
        logger.warning(f"Socket fallback failed: {e}")
        return '127.0.0.1'


def get_mac_address():
    """Get the MAC address of the active LAN interface (same interface as get_local_ip)."""
    try:
        skip_keywords = ['docker', 'veth', 'vmnet', 'virbr', 'lo', 'vbox', 'br-', 'virtual', 'hyperv', 'wsl']
        for iface_name, addrs in psutil.net_if_addrs().items():
            if any(kw in iface_name.lower() for kw in skip_keywords):
                continue
            # Check if this interface has a real LAN IP
            has_lan_ip = any(
                addr.family == socket.AF_INET and
                (addr.address.startswith('192.168.') or addr.address.startswith('10.'))
                for addr in addrs
            )
            if not has_lan_ip:
                continue
            # Get the MAC address from this interface
            for addr in addrs:
                if addr.family == psutil.AF_LINK and addr.address and addr.address != '00:00:00:00:00:00':
                    logger.info(f"Detected MAC: {addr.address} on interface: {iface_name}")
                    return addr.address.upper()
    except Exception as e:
        logger.warning(f"Error detecting MAC address: {e}")
    return ''




# ============================================================================
# LOCATION DETECTION
# ============================================================================

def get_location_data():
    """Get location using Windows Location Service (WinRT) with caching"""
    global _cached_location, _last_location_fetch
    current_time = time.time()
    with _cache_lock:
        if _cached_location and (current_time - _last_location_fetch < 1800):
            return _cached_location
    try:
        try:
            from winrt.windows.devices.geolocation import Geolocator, PositionStatus
            import asyncio
        except ImportError:
            logger.warning("Windows Location API not available (winrt not installed)")
            result = {"public_ip": "", "location_country": "", "location_region": "",
                      "location_city": "", "location_latitude": 0.0, "location_longitude": 0.0,
                      "location_source": "unavailable"}
            with _cache_lock:
                _cached_location = result
                _last_location_fetch = current_time
            return result

        async def get_windows_location():
            try:
                geolocator = Geolocator()
                status = geolocator.location_status
                if status == PositionStatus.DISABLED or status == PositionStatus.NOT_AVAILABLE:
                    return {"public_ip": "", "location_country": "", "location_region": "",
                            "location_city": "", "location_latitude": 0.0, "location_longitude": 0.0,
                            "location_source": "disabled"}
                position = await asyncio.wait_for(geolocator.get_geoposition_async(), timeout=10.0)
                latitude = position.coordinate.point.position.latitude
                longitude = position.coordinate.point.position.longitude
                return {"public_ip": "", "location_country": "", "location_region": "",
                        "location_city": "", "location_latitude": float(latitude),
                        "location_longitude": float(longitude), "location_source": "windows"}
            except Exception:
                return {"public_ip": "", "location_country": "", "location_region": "",
                        "location_city": "", "location_latitude": 0.0, "location_longitude": 0.0,
                        "location_source": "disabled"}

        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, get_windows_location())
                        result = future.result(timeout=15)
                else:
                    result = loop.run_until_complete(get_windows_location())
            except RuntimeError:
                result = asyncio.run(get_windows_location())
            with _cache_lock:
                _cached_location = result
                _last_location_fetch = current_time
            return result
        except Exception as e:
            logger.warning(f"Error running Windows Location Service: {e}")
            result = {"public_ip": "", "location_country": "", "location_region": "",
                      "location_city": "", "location_latitude": 0.0, "location_longitude": 0.0,
                      "location_source": "unavailable"}
            with _cache_lock:
                _cached_location = result
                _last_location_fetch = current_time
            return result
    except Exception as e:
        logger.error(f"Unexpected error in location detection: {e}")
        result = {"public_ip": "", "location_country": "", "location_region": "",
                  "location_city": "", "location_latitude": 0.0, "location_longitude": 0.0,
                  "location_source": "unavailable"}
        with _cache_lock:
            _cached_location = result
            _last_location_fetch = current_time
        return result


# ============================================================================
# SYSTEM INFORMATION FUNCTIONS (CACHED)
# ============================================================================

def get_serial_number():
    """Get BIOS serial number using multiple Windows methods (cached)"""
    global _cached_serial_number
    with _cache_lock:
        if _cached_serial_number is not None:
            return _cached_serial_number
    try:
        serial = None
        wmic_path = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32\\Wbem\\wmic.exe')
        if os.path.exists(wmic_path):
            result = subprocess.run(
                [wmic_path, "bios", "get", "serialnumber"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                if len(lines) > 1 and lines[1] and "SerialNumber" not in lines[1]:
                    serial = lines[1]
                    if serial.upper() not in ["TO BE FILLED BY O.E.M.", "0", "NONE", "UNKNOWN", "DEFAULT STRING"]:
                        with _cache_lock:
                            _cached_serial_number = serial
                        return serial
        ps_cmd = "Get-CimInstance Win32_BIOS | Select-Object -ExpandProperty SerialNumber"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            serial = result.stdout.strip()
            if serial.upper() not in ["TO BE FILLED BY O.E.M.", "0", "NONE", "UNKNOWN", "DEFAULT STRING"]:
                with _cache_lock:
                    _cached_serial_number = serial
                return serial
        with _cache_lock:
            _cached_serial_number = "UNKNOWN"
        return "UNKNOWN"
    except Exception as e:
        logger.warning(f"Error getting serial number: {e}")
        with _cache_lock:
            _cached_serial_number = "UNKNOWN"
        return "UNKNOWN"


def get_device_model():
    """Get device manufacturer and model (cached)"""
    global _cached_device_model
    with _cache_lock:
        if _cached_device_model is not None:
            return _cached_device_model
    try:
        cmd = 'Get-WmiObject Win32_ComputerSystem | Select-Object Manufacturer, Model | ConvertTo-Json'
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            device_data = json.loads(result.stdout)
            manufacturer = device_data.get('Manufacturer', '').strip()
            model = device_data.get('Model', '').strip()
            if not manufacturer and not model:
                device_model = socket.gethostname()
            elif not manufacturer:
                device_model = model or 'Unknown'
            elif not model:
                device_model = manufacturer or 'Unknown'
            elif model.lower().startswith(manufacturer.lower()):
                device_model = model
            else:
                device_model = f"{manufacturer} {model}"
            with _cache_lock:
                _cached_device_model = device_model
            return device_model
        hostname = socket.gethostname()
        with _cache_lock:
            _cached_device_model = hostname
        return hostname
    except Exception as e:
        logger.warning(f"Error getting device model: {e}")
        hostname = socket.gethostname()
        with _cache_lock:
            _cached_device_model = hostname
        return hostname


def get_graphics_card():
    """Get graphics card information (cached)"""
    global _cached_graphics_card
    with _cache_lock:
        if _cached_graphics_card is not None:
            return _cached_graphics_card
    try:
        cmd = 'Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name'
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_info = ", ".join([line.strip() for line in result.stdout.strip().split('\n') if line.strip()])
            with _cache_lock:
                _cached_graphics_card = gpu_info
            return gpu_info
        with _cache_lock:
            _cached_graphics_card = "No GPU Detected"
        return "No GPU Detected"
    except Exception as e:
        logger.warning(f"Error getting graphics card: {e}")
        with _cache_lock:
            _cached_graphics_card = "Detection Failed"
        return "Detection Failed"


def get_disk_type():
    """Detect if disk is SSD or HDD"""
    try:
        cmd = 'Get-PhysicalDisk | Select-Object MediaType | ConvertTo-Json'
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            disk_data = json.loads(result.stdout)
            if isinstance(disk_data, list) and len(disk_data) > 0:
                media_type = disk_data[0].get('MediaType', 'Unknown')
            elif isinstance(disk_data, dict):
                media_type = disk_data.get('MediaType', 'Unknown')
            else:
                media_type = 'Unknown'
            if media_type == 3 or media_type == "SSD":
                return "SSD"
            elif media_type == 4 or media_type == "HDD":
                return "HDD"
            return str(media_type)
        return "SSD"
    except:
        return "SSD"


def get_storage_volumes():
    """Detect all logical drives"""
    try:
        volumes = []
        import string
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            try:
                if psutil.disk_usage(drive_path):
                    usage = psutil.disk_usage(drive_path)
                    volumes.append({
                        'drive_letter': f"{letter}:",
                        'total_size': round(usage.total / (1024 ** 3), 2),
                        'free_space': round(usage.free / (1024 ** 3), 2),
                        'used_space': round(usage.used / (1024 ** 3), 2),
                        'drive_label': "System" if letter == 'C' else "Data"
                    })
            except:
                continue
        return json.dumps(volumes)
    except:
        return "[]"


def get_installed_apps():
    """Collect installed applications from Control Panel registry sources only"""
    all_apps = {}
    try:
        cmd_standard = '''
        $apps = @()
        $apps += Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* -ErrorAction SilentlyContinue | 
            Select-Object DisplayName, Publisher, DisplayVersion, InstallDate, EstimatedSize, ReleaseType, ParentKeyName | 
            Where-Object {
                $_.DisplayName -ne $null -and 
                $_.ReleaseType -notlike '*Update*' -and
                $_.ParentKeyName -eq $null
            }
        $apps += Get-ItemProperty HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* -ErrorAction SilentlyContinue | 
            Select-Object DisplayName, Publisher, DisplayVersion, InstallDate, EstimatedSize, ReleaseType, ParentKeyName | 
            Where-Object {
                $_.DisplayName -ne $null -and 
                $_.ReleaseType -notlike '*Update*' -and
                $_.ParentKeyName -eq $null
            }
        if ($apps.Count -eq 0) { Write-Output "[]" } else { $apps | ConvertTo-Json }
        '''
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd_standard],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "[]":
            apps_data = json.loads(result.stdout)
            data_iter = apps_data if isinstance(apps_data, list) else [apps_data]
            for app in data_iter:
                if app.get('DisplayName'):
                    name = str(app.get('DisplayName', ''))[:255].strip()
                    normalized_name = name.replace('ARP\\MachineX86\\', '').replace('ARP\\MachineX64\\', '')
                    if normalized_name:
                        if normalized_name not in all_apps:
                            all_apps[normalized_name] = {
                                'name': normalized_name,
                                'publisher': str(app.get('Publisher', ''))[:255].strip(),
                                'version': str(app.get('DisplayVersion', ''))[:100].strip(),
                                'installed_date': str(app.get('InstallDate', '')).strip(),
                                'size': float(app.get('EstimatedSize', 0)) if app.get('EstimatedSize') else 0
                            }
                        else:
                            existing = all_apps[normalized_name]
                            if not existing['publisher'] and app.get('Publisher'):
                                existing['publisher'] = str(app.get('Publisher', ''))[:255].strip()
                            if not existing['version'] and app.get('DisplayVersion'):
                                existing['version'] = str(app.get('DisplayVersion', ''))[:100].strip()
                            if not existing['installed_date'] and app.get('InstallDate'):
                                existing['installed_date'] = str(app.get('InstallDate', '')).strip()
                            if existing['size'] == 0 and app.get('EstimatedSize'):
                                existing['size'] = float(app.get('EstimatedSize', 0))
            logger.info(f"Standard Registry: Found {len(data_iter)} applications")
    except Exception as e:
        logger.warning(f"Standard registry read failed: {e}")

    try:
        cmd_msi = '''
        $msi_apps = @()
        try {
            $userDataPath = "HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Installer\\UserData\\S-1-5-18\\Products\\*"
            Get-Item $userDataPath -ErrorAction SilentlyContinue | ForEach-Object {
                $productKey = $_.PSPath
                $installProps = Get-ItemProperty "$productKey\\InstallProperties" -ErrorAction SilentlyContinue
                if ($installProps -and $installProps.DisplayName) {
                    $msi_apps += $installProps | Select-Object DisplayName, Publisher, DisplayVersion, InstallDate, EstimatedSize
                }
            }
        } catch {}
        try {
            $productsPath = "HKLM:\\Software\\Classes\\Installer\\Products\\*"
            Get-Item $productsPath -ErrorAction SilentlyContinue | ForEach-Object {
                $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
                if ($props -and $props.ProductName) {
                    $msi_apps += [PSCustomObject]@{
                        DisplayName = $props.ProductName
                        Publisher = if ($props.PSObject.Properties['Publisher']) { $props.Publisher } else { '' }
                        DisplayVersion = if ($props.PSObject.Properties['Version']) { $props.Version } else { '' }
                        InstallDate = ''
                        EstimatedSize = 0
                    }
                }
            }
        } catch {}
        if ($msi_apps.Count -eq 0) { Write-Output "[]" } else { $msi_apps | ConvertTo-Json }
        '''
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd_msi],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "[]":
            apps_data = json.loads(result.stdout)
            data_iter = apps_data if isinstance(apps_data, list) else [apps_data]
            for app in data_iter:
                display_name = app.get('DisplayName') or app.get('ProductName')
                if display_name:
                    name = str(display_name)[:255].strip()
                    normalized_name = name.replace('ARP\\MachineX86\\', '').replace('ARP\\MachineX64\\', '')
                    if normalized_name and normalized_name not in all_apps:
                        all_apps[normalized_name] = {
                            'name': normalized_name,
                            'publisher': str(app.get('Publisher', ''))[:255].strip(),
                            'version': str(app.get('DisplayVersion', ''))[:100].strip(),
                            'installed_date': str(app.get('InstallDate', '')).strip(),
                            'size': float(app.get('EstimatedSize', 0)) if app.get('EstimatedSize') else 0
                        }
            logger.info(f"MSI Registry: Found {len(data_iter)} additional applications")
    except Exception as e:
        logger.warning(f"MSI registry read failed: {e}")

    if len(all_apps) == 0:
        logger.warning("No applications found from registry")
        return "[]"
    logger.info(f"OK Total unique applications: {len(all_apps)}")
    return json.dumps(list(all_apps.values()))


def get_windows_version():
    """Detect Windows version"""
    try:
        if sys.platform == "win32":
            if sys.getwindowsversion().build >= 22000:
                return "Windows 11"
            return "Windows 10"
        return f"{platform.system()} {platform.release()}"
    except:
        return f"{platform.system()} {platform.release()}"


def get_battery_capacity():
    """Get battery capacity using powercfg or PowerShell fallback"""
    try:
        if platform.system() != "Windows":
            return 0, 0
        full_charge_mwh = 0
        design_mwh = 0
        temp_dir = tempfile.gettempdir()
        report_path = os.path.join(temp_dir, "battery-report.xml")
        subprocess.run(
            ["powercfg", "/batteryreport", "/output", report_path, "/xml"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if os.path.exists(report_path):
            try:
                tree = ET.parse(report_path)
                root = tree.getroot()
                for element in root.iter():
                    tag_lower = element.tag.lower()
                    if 'fullchargecapacity' in tag_lower:
                        try: full_charge_mwh = int(element.text)
                        except: pass
                    elif 'designcapacity' in tag_lower:
                        try: design_mwh = int(element.text)
                        except: pass
                os.remove(report_path)
            except:
                if os.path.exists(report_path):
                    os.remove(report_path)
        if full_charge_mwh == 0:
            cmd = 'Get-CimInstance Win32_Battery | Select-Object FullChargeCapacity, DesignCapacity | ConvertTo-Json'
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    if isinstance(data, list) and len(data) > 0:
                        data = data[0]
                    full_charge_mwh = data.get('FullChargeCapacity', 0)
                    design_mwh = data.get('DesignCapacity', 0)
                except:
                    pass
        if full_charge_mwh == 0:
            full_charge_mwh = design_mwh
        if full_charge_mwh == 0:
            return 0, 0
        return full_charge_mwh, round(full_charge_mwh / 11.1, 2)
    except:
        return 0, 0


# ============================================================================
# WINDOWS UPDATE FUNCTIONS
# ============================================================================

def scan_windows_updates():
    """
    Scan available Windows updates using PowerShell COM API.
    Returns list of update dicts: kb_number, title, severity, size, version.
    """
    logger.info("Scanning for available Windows updates...")
    ps_cmd = r"""
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    $Results = $Searcher.Search("IsInstalled=0 and Type='Software'")
    $Updates = @()
    foreach ($Update in $Results.Updates) {
        $KBs = @()
        foreach ($KB in $Update.KBArticleIDs) { $KBs += "KB$KB" }
        $KBString = $KBs -join ","
        $Updates += [PSCustomObject]@{
            Title        = $Update.Title
            KBArticleIDs = $KBString
            Description  = $Update.Description
            SeverityText = $Update.MsrcSeverity
            SizeMB       = [math]::Round($Update.MaxDownloadSize / 1MB, 2)
        }
    }
    if ($Updates.Count -eq 0) { Write-Output "[]" } else { $Updates | ConvertTo-Json -Depth 3 }
} catch { Write-Output "[]" }
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"Windows Update scan returned no output: {result.stderr}")
            return []
        raw = result.stdout.strip()
        if raw == "[]":
            logger.info("No pending Windows updates found")
            return []
        updates_raw = json.loads(raw)
        if isinstance(updates_raw, dict):
            updates_raw = [updates_raw]
        severity_map = {
            'critical': 'critical', 'important': 'important',
            'moderate': 'optional', 'low': 'optional', 'security': 'security',
        }
        updates = []
        for u in updates_raw:
            title = u.get('Title', '') or ''
            kb_raw = u.get('KBArticleIDs', '') or ''
            severity_raw = (u.get('SeverityText', '') or '').lower().strip()
            severity = severity_map.get(severity_raw, 'optional')
            if 'security' in title.lower() and severity == 'optional':
                severity = 'security'
            kb_number = kb_raw.split(',')[0].strip() if kb_raw else title[:50]
            if not kb_number:
                kb_number = f"UPDATE-{len(updates)+1}"
            version = ''
            for ver in ['24H2', '23H2', '22H2', '21H2', '21H1', '20H2']:
                if ver in title:
                    version = ver
                    break
            updates.append({
                'kb_number':   kb_number,
                'title':       title[:255],
                'description': (u.get('Description', '') or '')[:500],
                'severity':    severity,
                'size':        f"{u.get('SizeMB', 0)} MB",
                'version':     version,
            })
        logger.info(f"OK Found {len(updates)} pending Windows updates")
        return updates
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse Windows Update JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"Error scanning Windows updates: {e}")
        return []


def scan_installed_updates():
    """
    Scan INSTALLED Windows updates to detect manual installations by user.
    Returns list of KB numbers that are currently installed.
    """
    logger.info("Scanning for installed Windows updates...")
    ps_cmd = r"""
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    $Results = $Searcher.Search("IsInstalled=1 and Type='Software'")
    $InstalledKBs = @()
    foreach ($Update in $Results.Updates) {
        foreach ($KB in $Update.KBArticleIDs) {
            $InstalledKBs += "KB$KB"
        }
    }
    if ($InstalledKBs.Count -eq 0) { Write-Output "[]" } else { $InstalledKBs | ConvertTo-Json }
} catch { Write-Output "[]" }
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"Installed updates scan returned no output")
            return []
        raw = result.stdout.strip()
        if raw == "[]":
            logger.info("No installed updates found")
            return []
        installed_kbs = json.loads(raw)
        if isinstance(installed_kbs, str):
            installed_kbs = [installed_kbs]
        logger.info(f"OK Found {len(installed_kbs)} installed Windows updates")
        return installed_kbs
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse installed updates JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"Error scanning installed updates: {e}")
        return []


def report_updates_to_odoo(serial_number, updates, installed_kbs=None):
    """Send scanned updates to Odoo API, including list of installed KBs."""
    if not updates and not installed_kbs:
        logger.info("No updates to report to Odoo")
        return
    try:
        payload = {
            "serial_number": serial_number,
            "updates": updates,
            "installed_kbs": installed_kbs or []
        }
        url = f"{WINDOWS_UPDATE_BASE_URL}/report"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=30)
        if success:
            logger.info(f"OK Reported {len(updates)} available + {len(installed_kbs or [])} installed updates to Odoo")
        else:
            logger.warning("Failed to report updates to Odoo")
    except Exception as e:
        logger.error(f"Error reporting updates to Odoo: {e}")


def get_update_instructions(serial_number):
    """
    Poll Odoo for update instructions.
    Returns dict with is_locked, blocklist, push_list, uninstall_list, cancel_list.
    """
    default = {"is_locked": False, "blocklist": [], "push_list": [], "uninstall_list": [], "cancel_list": []}
    try:
        url = f"{WINDOWS_UPDATE_BASE_URL}/instructions"
        response = requests.get(
            url, params={"serial_number": serial_number},
            timeout=15, headers=ODOO_HEADERS
        )
        if response.status_code == 200:
            data = response.json()
            logger.info(
                f"Received instructions — locked: {data.get('is_locked')}, "
                f"block: {len(data.get('blocklist', []))}, "
                f"push: {len(data.get('push_list', []))}, "
                f"uninstall: {len(data.get('uninstall_list', []))}, "
                f"cancel: {len(data.get('cancel_list', []))}"
            )
            return data
        else:
            logger.warning(f"Failed to get update instructions: HTTP {response.status_code}")
            return default
    except Exception as e:
        logger.warning(f"Error getting update instructions: {e}")
        return default


def install_windows_update(kb_number):
    """Silently install a specific Windows update by KB number."""
    logger.info(f"Installing Windows update: {kb_number}")
    ps_cmd = f"""
try {{
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    $Results = $Searcher.Search("IsInstalled=0 and Type='Software'")
    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($Update in $Results.Updates) {{
        foreach ($KB in $Update.KBArticleIDs) {{
            if ("KB$KB" -eq "{kb_number}") {{ $ToInstall.Add($Update) | Out-Null }}
        }}
    }}
    if ($ToInstall.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
    $Downloader = $Session.CreateUpdateDownloader()
    $Downloader.Updates = $ToInstall
    $DownloadResult = $Downloader.Download()
    if ($DownloadResult.ResultCode -ne 2) {{ Write-Output "DOWNLOAD_FAILED"; exit }}
    $Installer = $Session.CreateUpdateInstaller()
    $Installer.Updates = $ToInstall
    $InstallResult = $Installer.Install()
    if ($InstallResult.ResultCode -eq 2) {{ Write-Output "SUCCESS" }}
    else {{ Write-Output "FAILED_$($InstallResult.ResultCode)" }}
}} catch {{ Write-Output "ERROR_$($_.Exception.Message)" }}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        output = result.stdout.strip()
        logger.info(f"Install result for {kb_number}: {output}")
        return output == "SUCCESS"
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing {kb_number}")
        return False
    except Exception as e:
        logger.error(f"Error installing {kb_number}: {e}")
        return False


def uninstall_windows_update(kb_number):
    """Silently uninstall a specific Windows update by KB number."""
    logger.info(f"Uninstalling Windows update: {kb_number}")
    kb_digits = kb_number.replace('KB', '').replace('kb', '').strip()
    ps_cmd = f"""
try {{
    $result = Start-Process -FilePath "wusa.exe" `
        -ArgumentList "/uninstall /kb:{kb_digits} /quiet /norestart" `
        -Wait -PassThru -NoNewWindow
    if ($result.ExitCode -eq 0) {{ Write-Output "SUCCESS" }}
    elseif ($result.ExitCode -eq 3010) {{ Write-Output "SUCCESS_REBOOT" }}
    elseif ($result.ExitCode -eq 2359302) {{ Write-Output "NOT_FOUND" }}
    else {{ Write-Output "FAILED_$($result.ExitCode)" }}
}} catch {{ Write-Output "ERROR_$($_.Exception.Message)" }}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        output = result.stdout.strip()
        logger.info(f"Uninstall result for {kb_number}: {output}")
        return output in ("SUCCESS", "SUCCESS_REBOOT")
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout uninstalling {kb_number}")
        return False
    except Exception as e:
        logger.error(f"Error uninstalling {kb_number}: {e}")
        return False


def report_update_result(serial_number, kb_number, status):
    """Report install/uninstall/cancel result back to Odoo."""
    try:
        payload = {"serial_number": serial_number, "kb_number": kb_number, "status": status}
        url = f"{WINDOWS_UPDATE_BASE_URL}/result"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"OK Reported {kb_number} result ({status}) to Odoo")
        else:
            logger.warning(f"Failed to report result for {kb_number}")
    except Exception as e:
        logger.error(f"Error reporting update result: {e}")


def enforce_update_button_lock(is_locked):
    """
    When locked:  disables Windows Update install capability (UI stays visible).
    When unlocked: restores normal Windows Update behavior.
    """
    if is_locked:
        logger.info("[LOCK] Disabling Windows Update install capability (UI remains visible)")
        try:
            ps_cmd = """
            $RegPath = "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU"
            if (!(Test-Path $RegPath)) { New-Item -Path $RegPath -Force | Out-Null }
            Set-ItemProperty -Path $RegPath -Name "NoAutoUpdate" -Value 1 -Type DWord
            Set-ItemProperty -Path $RegPath -Name "SetDisablePauseUXAccess" -Value 1 -Type DWord
            $RegPath3 = "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate"
            if (!(Test-Path $RegPath3)) { New-Item -Path $RegPath3 -Force | Out-Null }
            Set-ItemProperty -Path $RegPath3 -Name "SetDisableUXWUAccess" -Value 1 -Type DWord
            Write-Output "LOCKED"
            """
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                capture_output=True, timeout=15, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if "LOCKED" in result.stdout:
                logger.info("[LOCK] OK Windows Update install capability disabled successfully")
            else:
                logger.warning(f"[LOCK] WARNING Lock may not have applied correctly: {result.stderr}")
        except Exception as e:
            logger.error(f"[LOCK] ERROR Error applying lock: {e}")
    else:
        logger.info("[LOCK] Restoring Windows Update install capability")
        try:
            ps_cmd = """
            $RegPath = "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU"
            if (Test-Path $RegPath) {
                Remove-ItemProperty -Path $RegPath -Name "NoAutoUpdate" -ErrorAction SilentlyContinue
                Remove-ItemProperty -Path $RegPath -Name "SetDisablePauseUXAccess" -ErrorAction SilentlyContinue
            }
            $RegPath3 = "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate"
            if (Test-Path $RegPath3) {
                Remove-ItemProperty -Path $RegPath3 -Name "SetDisableUXWUAccess" -ErrorAction SilentlyContinue
            }
            Write-Output "UNLOCKED"
            """
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                capture_output=True, timeout=15, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if "UNLOCKED" in result.stdout:
                logger.info("[LOCK] OK Windows Update install capability restored")
            else:
                logger.warning(f"[LOCK] WARNING Unlock may not have applied correctly: {result.stderr}")
        except Exception as e:
            logger.error(f"[LOCK] ERROR Error removing lock: {e}")


def execute_update_instructions(serial_number, instructions):
    """Execute admin instructions from Odoo."""
    is_locked      = instructions.get('is_locked', False)
    blocklist      = instructions.get('blocklist', [])
    push_list      = instructions.get('push_list', [])
    uninstall_list = instructions.get('uninstall_list', [])
    cancel_list    = instructions.get('cancel_list', [])

    enforce_update_button_lock(is_locked)

    if is_locked:
        logger.info("Device is locked — no update actions will be performed by agent")
        return

    if blocklist:
        logger.info(f"Blocked updates (suppressed by admin): {blocklist}")

    if cancel_list:
        logger.info(f"[CANCEL] Admin requested cancellation of: {cancel_list}")
        for kb_number in cancel_list:
            try:
                cancellation_success = False
                ps_kill_selective = """
                try {
                    Get-Process -Name powershell -ErrorAction SilentlyContinue | 
                    Where-Object { 
                        $_.Modules.ModuleName -like '*wuapi*' -or 
                        $_.CommandLine -like '*Microsoft.Update*'
                    } | Stop-Process -Force -ErrorAction SilentlyContinue
                    Write-Output "OK"
                } catch { Write-Output "FAILED" }
                """
                try:
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_kill_selective],
                        capture_output=True, timeout=10, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if "OK" in result.stdout:
                        cancellation_success = True
                except Exception as e:
                    logger.warning(f"[CANCEL] Method 1 failed: {e}")

                if not cancellation_success:
                    try:
                        result = subprocess.run(
                            ["taskkill", "/F", "/IM", "powershell.exe"],
                            capture_output=True, timeout=10, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        if result.returncode == 0:
                            cancellation_success = True
                    except Exception as e:
                        logger.warning(f"[CANCEL] Method 2 failed: {e}")

                if not cancellation_success:
                    try:
                        subprocess.run(["net", "stop", "wuauserv"], capture_output=True, timeout=15,
                                       text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        time.sleep(3)
                        start_result = subprocess.run(["net", "start", "wuauserv"], capture_output=True,
                                                      timeout=15, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        if start_result.returncode == 0:
                            cancellation_success = True
                    except Exception as e:
                        logger.warning(f"[CANCEL] Method 3 failed: {e}")

                report_update_result(serial_number, kb_number, 'cancelled')
                logger.info(f"[CANCEL] {'SUCCESS' if cancellation_success else 'ATTEMPTED'} {kb_number}")
            except Exception as e:
                logger.error(f"[CANCEL] ERROR cancelling {kb_number}: {e}")
                report_update_result(serial_number, kb_number, 'failed')

    if push_list:
        logger.info(f"[INSTALL] Admin requested installation of: {push_list}")
        for kb_number in push_list:
            try:
                success = install_windows_update(kb_number)
                status = 'installed' if success else 'failed'
                report_update_result(serial_number, kb_number, status)
                logger.info(f"[INSTALL] {'SUCCESS' if success else 'FAILED'} {kb_number} -> {status}")
            except Exception as e:
                logger.error(f"[INSTALL] ERROR installing {kb_number}: {e}")
                report_update_result(serial_number, kb_number, 'failed')

    if uninstall_list:
        logger.info(f"[UNINSTALL] Admin requested uninstall of: {uninstall_list}")
        for kb_number in uninstall_list:
            try:
                success = uninstall_windows_update(kb_number)
                status = 'uninstalled' if success else 'failed'
                report_update_result(serial_number, kb_number, status)
                logger.info(f"[UNINSTALL] {'SUCCESS' if success else 'FAILED'} {kb_number} -> {status}")
            except Exception as e:
                logger.error(f"[UNINSTALL] ERROR uninstalling {kb_number}: {e}")
                report_update_result(serial_number, kb_number, 'failed')


def windows_update_sync_loop():
    """Background thread for Windows Update sync."""
    logger.info(f"Windows Update sync loop started (interval: {WINDOWS_UPDATE_SYNC_INTERVAL}s)")
    time.sleep(30)

    while True:
        try:
            if not _windows_update_lock.acquire(blocking=False):
                logger.warning("Windows Update sync already in progress, skipping")
                time.sleep(WINDOWS_UPDATE_SYNC_INTERVAL)
                continue
            try:
                logger.info("=" * 40)
                logger.info("Starting Windows Update sync cycle...")
                serial = get_serial_number()
                updates = scan_windows_updates()
                installed_kbs = scan_installed_updates()
                if updates or installed_kbs:
                    report_updates_to_odoo(serial, updates, installed_kbs)
                instructions = get_update_instructions(serial)
                execute_update_instructions(serial, instructions)
                logger.info("Windows Update sync cycle complete")
                logger.info("=" * 40)
            finally:
                _windows_update_lock.release()
        except Exception as e:
            logger.error(f"Error in Windows Update sync loop: {e}")
        time.sleep(WINDOWS_UPDATE_SYNC_INTERVAL)


# ============================================================================
# APPLICATION UNINSTALL FUNCTIONS
# ============================================================================

APP_UNINSTALL_BASE_URL = "http://192.168.105.145:8069/api/asset/apps"
APP_UNINSTALL_SYNC_INTERVAL = 45  # Poll every 45 seconds


def find_application_in_registry(app_name, publisher=None, version=None):
    """
    Search Windows Registry Uninstall keys for an application.
    Returns dict with uninstall_string, product_code, display_name, publisher, version.
    """
    logger.info(f"[APP UNINSTALL] Searching registry for: {app_name} (publisher: {publisher}, version: {version})")
    
    search_paths = [
        r"HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"HKLM\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    
    # Normalize search terms
    app_name_lower = app_name.lower().strip() if app_name else ""
    publisher_lower = publisher.lower().strip() if publisher else ""
    version_lower = version.lower().strip() if version else ""
    
    found_apps = []
    
    for reg_path in search_paths:
        try:
            # List all subkeys (application GUIDs/names)
            list_cmd = f'reg query "{reg_path}" /f "" /s 2>nul'
            result = subprocess.run(
                list_cmd, shell=True, capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode != 0:
                continue
            
            # Parse output to get subkey names
            lines = result.stdout.strip().split('\n')
            subkeys = []
            for line in lines:
                if line.startswith(reg_path.replace('\\', '\\\\')):
                    subkeys.append(line.strip())
            
            # Check each subkey for matching application
            for subkey in subkeys:
                try:
                    # Query all values for this subkey
                    query_cmd = f'reg query "{subkey}" /v DisplayName 2>nul'
                    name_result = subprocess.run(
                        query_cmd, shell=True, capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    
                    if name_result.returncode != 0:
                        continue
                    
                    # Extract display name
                    display_name = ""
                    for line in name_result.stdout.strip().split('\n'):
                        if 'DisplayName' in line and 'REG_SZ' in line:
                            parts = line.split('REG_SZ')
                            if len(parts) > 1:
                                display_name = parts[1].strip()
                                break
                    
                    if not display_name:
                        continue
                    
                    # Check if name matches
                    if app_name_lower not in display_name.lower():
                        continue
                    
                    # Get additional values
                    app_info = {
                        'subkey': subkey,
                        'display_name': display_name,
                        'uninstall_string': '',
                        'product_code': '',
                        'publisher': '',
                        'version': '',
                    }
                    
                    # Get UninstallString
                    us_cmd = f'reg query "{subkey}" /v UninstallString 2>nul'
                    us_result = subprocess.run(
                        us_cmd, shell=True, capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if us_result.returncode == 0:
                        for line in us_result.stdout.strip().split('\n'):
                            if 'UninstallString' in line:
                                parts = line.split('REG_SZ') if 'REG_SZ' in line else line.split('REG_EXPAND_SZ')
                                if len(parts) > 1:
                                    app_info['uninstall_string'] = parts[1].strip()
                                    break
                    
                    # Get Publisher
                    pub_cmd = f'reg query "{subkey}" /v Publisher 2>nul'
                    pub_result = subprocess.run(
                        pub_cmd, shell=True, capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if pub_result.returncode == 0:
                        for line in pub_result.stdout.strip().split('\n'):
                            if 'Publisher' in line and 'REG_SZ' in line:
                                parts = line.split('REG_SZ')
                                if len(parts) > 1:
                                    app_info['publisher'] = parts[1].strip()
                                    break
                    
                    # Get DisplayVersion
                    ver_cmd = f'reg query "{subkey}" /v DisplayVersion 2>nul'
                    ver_result = subprocess.run(
                        ver_cmd, shell=True, capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if ver_result.returncode == 0:
                        for line in ver_result.stdout.strip().split('\n'):
                            if 'DisplayVersion' in line and 'REG_SZ' in line:
                                parts = line.split('REG_SZ')
                                if len(parts) > 1:
                                    app_info['version'] = parts[1].strip()
                                    break
                    
                    # Check for MSI product code in subkey name
                    if '{' in subkey and '}' in subkey:
                        app_info['product_code'] = subkey.split('\\')[-1]
                    
                    found_apps.append(app_info)
                    
                except Exception as e:
                    logger.warning(f"[APP UNINSTALL] Error processing subkey {subkey}: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"[APP UNINSTALL] Error querying {reg_path}: {e}")
            continue
    
    if not found_apps:
        logger.warning(f"[APP UNINSTALL] No matching application found in registry for: {app_name}")
        return None
    
    # Sort by best match (prefer exact matches)
    def match_score(app):
        score = 0
        if app_name_lower == app['display_name'].lower().strip():
            score += 100
        elif app_name_lower in app['display_name'].lower():
            score += 50
        
        if publisher_lower and publisher_lower in app['publisher'].lower():
            score += 20
        if version_lower and version_lower in app['version'].lower():
            score += 10
        
        return score
    
    found_apps.sort(key=match_score, reverse=True)
    best_match = found_apps[0]
    
    logger.info(
        f"[APP UNINSTALL] Found application: {best_match['display_name']} "
        f"(publisher: {best_match['publisher']}, version: {best_match['version']})"
    )
    
    return best_match


def build_uninstall_command(app_info):
    """
    Build the appropriate uninstall command based on uninstaller type.
    Returns tuple: (command_list, is_msi)
    """
    uninstall_string = app_info.get('uninstall_string', '')
    product_code = app_info.get('product_code', '')
    
    # Case 1: MSI installer - use msiexec
    if product_code and ('{' in product_code):
        logger.info(f"[APP UNINSTALL] Using MSI uninstall with product code: {product_code}")
        return (
            ["msiexec", "/x", product_code, "/quiet", "/norestart"],
            True
        )
    
    # Case 2: UninstallString contains msiexec
    if 'msiexec' in uninstall_string.lower():
        # Extract product code or use full command
        if '{' in uninstall_string and '}' in uninstall_string:
            import re
            match = re.search(r'\{[A-Fa-f0-9-]+\}', uninstall_string)
            if match:
                code = match.group(0)
                logger.info(f"[APP UNINSTALL] Using MSI uninstall extracted from string: {code}")
                return (
                    ["msiexec", "/x", code, "/quiet", "/norestart"],
                    True
                )
        # Use full msiexec command with silent flags
        logger.info(f"[APP UNINSTALL] Using msiexec from uninstall string")
        return (
            ["cmd", "/c", f'{uninstall_string} /quiet /norestart'],
            True
        )
    
    # Case 3: EXE uninstaller - detect type and apply appropriate flags
    uninstall_exe = uninstall_string.strip('"').strip("'").split(' ')[0]
    
    if not os.path.exists(uninstall_exe):
        # Try to find the executable
        if os.path.exists(uninstall_exe + '.exe'):
            uninstall_exe = uninstall_exe + '.exe'
        else:
            logger.warning(f"[APP UNINSTALL] Uninstaller not found: {uninstall_exe}")
            return None
    
    # Detect uninstaller type and build command
    uninstaller_name = os.path.basename(uninstall_exe).lower()
    
    # Inno Setup uninstaller
    if 'unins000.exe' in uninstaller_name or 'innosetup' in uninstall_string.lower():
        logger.info("[APP UNINSTALL] Detected Inno Setup uninstaller")
        return (
            [uninstall_exe, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            False
        )
    
    # NSIS (Nullsoft) uninstaller
    if 'nsis' in uninstall_string.lower():
        logger.info("[APP UNINSTALL] Detected NSIS uninstaller")
        return (
            [uninstall_exe, "/S", "/NCRC"],
            False
        )
    
    # InstallShield uninstaller
    if 'installshield' in uninstall_string.lower() or 'setup.iss' in uninstall_string.lower():
        logger.info("[APP UNINSTALL] Detected InstallShield uninstaller")
        return (
            [uninstall_exe, "/s", "/v'/qn /norestart'"],
            False
        )
    
    # Wise Installer
    if 'wise' in uninstall_string.lower():
        logger.info("[APP UNINSTALL] Detected Wise Installer uninstaller")
        return (
            [uninstall_exe, "/s"],
            False
        )
    
    # Generic EXE - try common silent flags
    logger.info("[APP UNINSTALL] Using generic silent uninstall flags")
    return (
        [uninstall_exe, "/S", "/silent", "/quiet", "/qn", "/norestart"],
        False
    )


def uninstall_application(app_name, publisher, version):
    """
    Uninstall an application by name, publisher, and version.
    Returns tuple: (success: bool, error_message: str or None)
    """
    logger.info(
        f"[APP UNINSTALL] Starting uninstall for: {app_name} "
        f"(publisher: {publisher}, version: {version})"
    )
    
    try:
        # Step 1: Find application in registry
        app_info = find_application_in_registry(app_name, publisher, version)
        
        if not app_info:
            error_msg = f"Application '{app_name}' not found in Windows Registry"
            logger.error(f"[APP UNINSTALL] {error_msg}")
            return (False, error_msg)
        
        # Step 2: Build uninstall command
        cmd_info = build_uninstall_command(app_info)
        
        if not cmd_info:
            error_msg = "Could not build uninstall command"
            logger.error(f"[APP UNINSTALL] {error_msg}")
            return (False, error_msg)
        
        command, is_msi = cmd_info
        
        if not command:
            error_msg = "Invalid uninstall command"
            logger.error(f"[APP UNINSTALL] {error_msg}")
            return (False, error_msg)
        
        logger.info(f"[APP UNINSTALL] Executing command: {' '.join(command)}")
        
        # Step 3: Execute uninstall command
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes timeout for uninstall
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        logger.info(f"[APP UNINSTALL] Exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"[APP UNINSTALL] stdout: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"[APP UNINSTALL] stderr: {result.stderr[:500]}")
        
        # Step 4: Check result
        # MSI exit codes: 0 = success, 3010 = success+reboot, 1605 = not found
        # Generic: 0 = success
        success_codes = [0, 3010, 1641]  # 1641 = reboot initiated
        
        if result.returncode in success_codes:
            logger.info(f"[APP UNINSTALL] SUCCESS: {app_name} uninstalled successfully")
            return (True, None)
        elif result.returncode == 1605:
            logger.warning(f"[APP UNINSTALL] Application not found (already uninstalled?)")
            return (True, None)  # Treat as success
        else:
            error_msg = f"Uninstall failed with exit code {result.returncode}"
            logger.error(f"[APP UNINSTALL] FAILED: {error_msg}")
            return (False, error_msg)
            
    except subprocess.TimeoutExpired:
        error_msg = "Uninstall timed out after 10 minutes"
        logger.error(f"[APP UNINSTALL] TIMEOUT: {error_msg}")
        return (False, error_msg)
    except Exception as e:
        error_msg = f"Uninstall error: {str(e)}"
        logger.error(f"[APP UNINSTALL] ERROR: {error_msg}")
        return (False, error_msg)


def get_app_uninstall_instructions(serial_number):
    """
    Poll Odoo for application uninstall instructions.
    Returns list of applications to uninstall.
    """
    default = {"success": True, "uninstall_list": []}
    try:
        url = f"{APP_UNINSTALL_BASE_URL}/uninstall_command"
        response = requests.get(
            url,
            params={"serial_number": serial_number},
            timeout=15,
            headers=ODOO_HEADERS
        )
        
        if response.status_code == 200:
            data = response.json()
            uninstall_list = data.get('uninstall_list', [])
            logger.info(
                f"[APP UNINSTALL] Received {len(uninstall_list)} uninstall commands from Odoo"
            )
            return data
        else:
            logger.warning(
                f"[APP UNINSTALL] Failed to get instructions: HTTP {response.status_code}"
            )
            return default
    except Exception as e:
        logger.warning(f"[APP UNINSTALL] Error getting instructions: {e}")
        return default


def report_app_uninstall_result(serial_number, app_name, app_publisher, app_version, status, error_message=None):
    """
    Report application uninstall result back to Odoo.
    status: 'uninstalled' or 'failed'
    """
    try:
        payload = {
            "serial_number": serial_number,
            "app_name": app_name,
            "app_publisher": app_publisher,
            "app_version": app_version,
            "status": status,
        }
        if error_message:
            payload["error_message"] = error_message
        
        url = f"{APP_UNINSTALL_BASE_URL}/uninstall_result"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        
        if success:
            logger.info(
                f"[APP UNINSTALL] Reported result to Odoo: {app_name} -> {status}"
            )
        else:
            logger.warning(
                f"[APP UNINSTALL] Failed to report result for {app_name}"
            )
    except Exception as e:
        logger.error(f"[APP UNINSTALL] Error reporting result: {e}")


def execute_app_uninstall_instructions(serial_number, instructions):
    """
    Execute application uninstall instructions from Odoo.
    """
    uninstall_list = instructions.get('uninstall_list', [])
    
    if not uninstall_list:
        return
    
    logger.info(f"[APP UNINSTALL] Processing {len(uninstall_list)} uninstall commands")
    
    for app in uninstall_list:
        app_name = app.get('name', '')
        app_publisher = app.get('publisher', '')
        app_version = app.get('version', '')
        
        if not app_name:
            logger.warning("[APP UNINSTALL] Skipping app with no name")
            continue
        
        logger.info(
            f"[APP UNINSTALL] Processing: {app_name} (v{app_version}) by {app_publisher}"
        )
        
        try:
            # Execute uninstall
            success, error_message = uninstall_application(
                app_name, app_publisher, app_version
            )
            
            # Report result
            status = 'uninstalled' if success else 'failed'
            report_app_uninstall_result(
                serial_number,
                app_name,
                app_publisher,
                app_version,
                status,
                error_message
            )
            
            logger.info(
                f"[APP UNINSTALL] {'SUCCESS' if success else 'FAILED'}: "
                f"{app_name} -> {status}"
            )
            
        except Exception as e:
            logger.error(f"[APP UNINSTALL] ERROR processing {app_name}: {e}")
            report_app_uninstall_result(
                serial_number,
                app_name,
                app_publisher,
                app_version,
                'failed',
                str(e)
            )


def app_uninstall_sync_loop():
    """
    Background thread for application uninstall sync.
    Polls Odoo every APP_UNINSTALL_SYNC_INTERVAL seconds for uninstall commands.
    """
    logger.info(
        f"[APP UNINSTALL] Sync loop started (interval: {APP_UNINSTALL_SYNC_INTERVAL}s)"
    )
    time.sleep(30)  # Initial delay
    
    serial = get_serial_number()
    
    while True:
        try:
            # Poll for uninstall instructions
            instructions = get_app_uninstall_instructions(serial)
            
            # Execute any pending uninstalls
            if instructions.get('uninstall_list'):
                execute_app_uninstall_instructions(serial, instructions)
            else:
                logger.debug("[APP UNINSTALL] No pending uninstall commands")
            
        except Exception as e:
            logger.error(f"[APP UNINSTALL] Error in sync loop: {e}")
        
        time.sleep(APP_UNINSTALL_SYNC_INTERVAL)


# ============================================================================
# FILE ACCESS POLICY — BLOCK + NOTIFY + REPORT
# ============================================================================

_file_access_policy   = {}   # {path: True/False}  — paths to block
_file_access_lock     = threading.Lock()
_file_access_watchers = {}   # {path: observer}


def fa_show_notification(blocked_path):
    """Show Windows toast notification when access is blocked."""
    try:
        title   = "Access Blocked by Admin Policy"
        message = f"Access to '{os.path.basename(blocked_path)}' has been blocked by your administrator."
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
             f"""
             Add-Type -AssemblyName System.Windows.Forms;
             $n = New-Object System.Windows.Forms.NotifyIcon;
             $n.Icon = [System.Drawing.SystemIcons]::Shield;
             $n.BalloonTipTitle = '{title}';
             $n.BalloonTipText  = '{message}';
             $n.Visible = $True;
             $n.ShowBalloonTip(5000);
             Start-Sleep -Seconds 6;
             $n.Dispose()
             """],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        logger.info(f"[FILE ACCESS] Notification shown for: {blocked_path}")
    except Exception as e:
        logger.warning(f"[FILE ACCESS] Notification error: {e}")


def fa_is_safe_to_block(path):
    """
    Safety check — never allow blocking entire drives or system folders.
    Only specific files or sub-folders inside user folders are allowed.
    """
    path = os.path.normpath(path).rstrip("\\")
    home        = os.path.expanduser("~")
    system_root = os.environ.get("SystemRoot", "C:\\Windows")

    # Never block drive roots
    if len(path) <= 3 and ":" in path:
        logger.warning(f"[FILE ACCESS] SAFETY: Refused to block drive root: {path}")
        return False

    # Never block these protected paths
    protected = [
        home,
        system_root,
        "C:\\", "D:\\", "E:\\", "F:\\",
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Pictures"),
        os.path.join(home, "Music"),
        os.path.join(home, "Videos"),
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\Windows",
        "C:\\Users",
    ]
    for p in protected:
        if path.lower() == os.path.normpath(p).lower():
            logger.warning(f"[FILE ACCESS] SAFETY: Refused to block protected path: {path}")
            return False

    return True


def fa_block_path(path):
    """Block access to EXACT file or specific folder only — never drives or system folders."""
    try:
        username = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$env:USERNAME"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        ).stdout.strip()

        if not username:
            return False

        result = subprocess.run(
            ["icacls", path, "/deny", f"{username}:F"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            logger.info(f"[FILE ACCESS] Blocked exact path: {path}")
            return True
        else:
            logger.warning(f"[FILE ACCESS] icacls block failed: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"[FILE ACCESS] Block error: {e}")
        return False


def fa_unblock_path(path):
    """Remove DENY — restore access."""
    try:
        username = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$env:USERNAME"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        ).stdout.strip()

        if not username:
            return False

        result = subprocess.run(
            ["icacls", path, "/remove:d", username],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            logger.info(f"[FILE ACCESS] Unblocked exact path: {path}")
            return True
        return False
    except Exception as e:
        logger.error(f"[FILE ACCESS] Unblock error: {e}")
        return False


def fa_report_violation(serial, blocked_path, action_taken="blocked"):
    """Report access violation to Odoo."""
    try:
        payload = {
            "serial_number": serial,
            "path":          blocked_path,
            "folder":        os.path.dirname(blocked_path),
            "filename":      os.path.basename(blocked_path),
            "action_taken":  action_taken,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
        ok, _ = send_with_retry(
            f"{FILE_ACCESS_BASE_URL}/violation",
            payload, max_retries=2, timeout=10
        )
        if ok:
            logger.info(f"[FILE ACCESS] Violation reported: {blocked_path}")
    except Exception as e:
        logger.warning(f"[FILE ACCESS] Report violation error: {e}")


def fa_get_policy(serial):
    """
    Poll Odoo for file access policy.
    Returns list of blocked paths for this device.
    """
    try:
        url      = f"{FILE_ACCESS_BASE_URL}/policy?serial_number={serial}"
        response = requests.get(url, timeout=10, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data  = response.json()
            paths = data.get("blocked_paths", [])
            logger.info(f"[FILE ACCESS] Policy received: {len(paths)} blocked path(s)")
            return paths
        else:
            logger.warning(f"[FILE ACCESS] Policy fetch failed: HTTP {response.status_code}")
            return []
    except Exception as e:
        logger.warning(f"[FILE ACCESS] Policy fetch error: {e}")
        return []


def fa_enforce_policy(serial, blocked_paths):
    """
    Compare current policy with new policy.
    Block newly added paths, unblock removed paths.
    """
    global _file_access_policy

    new_policy = {p: True for p in blocked_paths}

    with _file_access_lock:
        # Unblock paths no longer in policy
        for path in list(_file_access_policy.keys()):
            if path not in new_policy:
                fa_unblock_path(path)
                del _file_access_policy[path]
                logger.info(f"[FILE ACCESS] Policy removed — unblocked: {path}")

        # Block newly added paths — safety check first
        for path in new_policy:
            if path not in _file_access_policy:
                if not fa_is_safe_to_block(path):
                    logger.warning(f"[FILE ACCESS] Skipped unsafe path: {path}")
                    continue
                if os.path.exists(path):
                    ok = fa_block_path(path)
                    if ok:
                        _file_access_policy[path] = True
                        fa_show_notification(path)
                        fa_report_violation(serial, path, action_taken="blocked_by_policy")
                else:
                    logger.warning(f"[FILE ACCESS] Path does not exist: {path}")


def fa_monitor_access(serial):
    """
    Monitor Desktop, Documents, Downloads using watchdog.
    When a file/folder is accessed that is in the blocked policy,
    re-enforce the block and notify + report.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class AccessHandler(FileSystemEventHandler):
            def __init__(self, serial):
                self.serial = serial

            def _check_and_enforce(self, event_path):
                with _file_access_lock:
                    for blocked in _file_access_policy:
                        if event_path.startswith(blocked):
                            logger.warning(f"[FILE ACCESS] Access attempt on blocked path: {event_path}")
                            fa_block_path(blocked)
                            fa_show_notification(event_path)
                            fa_report_violation(self.serial, event_path, action_taken="blocked")

            def on_opened(self, event):
                if not event.is_directory:
                    self._check_and_enforce(event.src_path)

            def on_created(self, event):
                self._check_and_enforce(event.src_path)

            def on_modified(self, event):
                self._check_and_enforce(event.src_path)

        monitored = get_monitored_folders()
        observer  = Observer()
        handler   = AccessHandler(serial)

        for folder in monitored:
            if os.path.exists(folder):
                observer.schedule(handler, folder, recursive=True)
                logger.info(f"[FILE ACCESS] Watching: {folder}")

        observer.start()
        logger.info("[FILE ACCESS] File system monitor started")
        return observer

    except ImportError:
        logger.warning("[FILE ACCESS] watchdog not installed — monitoring disabled. Run: pip install watchdog")
        return None
    except Exception as e:
        logger.error(f"[FILE ACCESS] Monitor start error: {e}")
        return None


_fa_observer = None


def fa_scan_folders(serial):
    """
    Recursively scan Desktop, Documents, Downloads.

    For EVERY file and folder found (at ANY depth):
      - path        = full absolute filesystem path (entry.path)
      - parent_path = os.path.dirname(entry.path)   ← real parent folder
      - parent_folder = 'Desktop' | 'Documents' | 'Downloads'  ← top-level bucket

    The Odoo UI uses parent_path to navigate:
      root tab click  → show items whose parent_path == the root folder path
      folder click    → show items whose parent_path == clicked folder path
    """
    try:
        home = os.path.expanduser("~")
        root_folders = {
            "Desktop":   os.path.join(home, "Desktop"),
            "Documents": os.path.join(home, "Documents"),
            "Downloads": os.path.join(home, "Downloads"),
        }

        records = []
        MAX_RECORDS = 5000  # Safety limit for Odoo payload size
        EXCLUSIONS  = {
            "node_modules", ".git", ".next", "dist", "build",
            ".cache", "AppData", "__pycache__", ".venv", "venv",
            "bin", "obj", ".idea", ".vscode"
        }

        for folder_name, folder_path in root_folders.items():
            if not os.path.exists(folder_path):
                continue
            if len(records) >= MAX_RECORDS: break

            # Add the anchor record for the root itself (Desktop, etc.)
            # This allows the UI to discover the machine's actual base paths.
            try:
                st = os.stat(folder_path)
                records.append({
                    "type": "folder",
                    "name": folder_name,
                    "path": folder_path,
                    "parent_path": os.path.dirname(folder_path),
                    "parent_folder": folder_name,
                    "size_kb": 0,
                    "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
            except: pass

            # Recursively walk
            for dirpath, dirnames, filenames in os.walk(folder_path):
                # Filter dirnames in-place to prune the walk
                dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in EXCLUSIONS]

                # Folder entries
                for dname in dirnames:
                    if len(records) >= MAX_RECORDS: break
                    full_p = os.path.join(dirpath, dname)
                    try:
                        st = os.stat(full_p)
                        records.append({
                            "type": "folder",
                            "name": dname,
                            "path": full_p,
                            "parent_path": os.path.dirname(full_p),
                            "parent_folder": folder_name,
                            "size_kb": 0,
                            "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        })
                    except: continue

                # File entries
                for fname in filenames:
                    if len(records) >= MAX_RECORDS: break
                    full_p = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full_p)
                        records.append({
                            "type": "file",
                            "name": fname,
                            "path": full_p,
                            "parent_path": os.path.dirname(full_p),
                            "parent_folder": folder_name,
                            "size_kb": round(st.st_size / 1024, 2),
                            "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        })
                    except: continue

                if len(records) >= MAX_RECORDS: break

        if records:
            payload = {
                "serial_number": serial,
                "records":       records,
                "scanned_at":    datetime.now(timezone.utc).isoformat(),
            }
            # Large payloads need longer timeout
            ok, _ = send_with_retry(
                f"{FILE_ACCESS_BASE_URL}/scan",
                payload, max_retries=2, timeout=60
            )
            if ok:
                logger.info(f"[FILE ACCESS] Scanned {len(records)} items (optimized) → sent to Odoo")
            else:
                logger.warning("[FILE ACCESS] Scan send failed (timeout?)")
        else:
            logger.warning("[FILE ACCESS] No records found — check folder paths")

    except Exception as e:
        logger.error(f"[FILE ACCESS] Scan error: {e}")


def file_access_sync_loop():
    """
    Every 60s:
    1. Scan Desktop/Documents/Downloads → send to Odoo (UI shows files with lock icons)
    2. Poll policy → enforce blocks/unblocks
    """
    global _fa_observer
    logger.info(f"[FILE ACCESS v1.5] Sync loop started (interval: {FILE_ACCESS_SYNC_INTERVAL}s)")

    serial = get_serial_number()

    # Start filesystem monitor for violation detection
    _fa_observer = fa_monitor_access(serial)

    while True:
        try:
            # Step 1 — scan and report files to Odoo
            fa_scan_folders(serial)
            # Step 2 — get policy and enforce blocks
            blocked_paths = fa_get_policy(serial)
            fa_enforce_policy(serial, blocked_paths)
        except Exception as e:
            logger.error(f"[FILE ACCESS] Sync loop error: {e}")
        time.sleep(FILE_ACCESS_SYNC_INTERVAL)


def collect_static_data():
    """Collect static system information"""
    try:
        battery = psutil.sensors_battery()
        battery_percentage = round(battery.percent, 2) if battery else 0
        _, battery_capacity_mah = get_battery_capacity()
        
        # Get antivirus information
        av_info = get_antivirus_info()
        
        payload = {
            "serial_number":    get_serial_number(),
            "hostname":         socket.gethostname(),
            "device_name":      get_device_model(),
            "processor":        platform.processor() or "Unknown Processor",
            "os_type":          platform.architecture()[0],
            "os_name":          get_windows_version(),
            "ram_size":         round(psutil.virtual_memory().total / (1024 ** 3), 2),
            "rom_size":         round(psutil.disk_usage('C:\\').total / (1024 ** 3), 2),
            "disk_type":        get_disk_type(),
            "graphics_card_raw": get_graphics_card(),
            "battery_capacity": battery_capacity_mah,
            "battery_percentage": battery_percentage,
            "storage_volumes":  get_storage_volumes(),
            "installed_apps":   get_installed_apps(),
            "agent_version":    AGENT_VERSION,
            "local_ip":         get_local_ip(),
            "mac_address":      get_mac_address(),
            "file_browser_port": FILE_BROWSER_PORT,
            # Antivirus information (TASK 1: Agent Side Improvements)
            "antivirus_installed": av_info["antivirus_installed"],
            "antivirus_product": av_info["antivirus_product"],
            "antivirus_version": av_info["antivirus_version"],
            "antivirus_running": av_info["antivirus_running"]
        }
        location_data = get_location_data()
        payload.update(location_data)
        return payload
    except Exception as e:
        logger.error(f"Error collecting static data: {e}", exc_info=True)
        payload = {
            "serial_number": get_serial_number(), "hostname": socket.gethostname(),
            "device_name": "Unknown", "processor": "Unknown", "os_type": "64bit",
            "os_name": "Windows", "ram_size": 0, "rom_size": 0, "disk_type": "Unknown",
            "graphics_card_raw": "Unknown", "battery_capacity": 0, "battery_percentage": 0,
            "storage_volumes": "[]", "installed_apps": "[]", "agent_version": AGENT_VERSION,
            "local_ip": get_local_ip(), "file_browser_port": FILE_BROWSER_PORT,
            # Antivirus information (error fallback)
            "antivirus_installed": False,
            "antivirus_product": "Error",
            "antivirus_version": "unknown",
            "antivirus_running": False
        }
        location_data = get_location_data()
        payload.update(location_data)
        return payload


def collect_live_data():
    """Collect live system metrics"""
    serial_number = get_serial_number()
    hostname = socket.gethostname()
    current_heartbeat = datetime.now(timezone.utc).isoformat()
    try:
        cpu_usage = round(psutil.cpu_percent(interval=0.5), 2)
        ram = psutil.virtual_memory()
        ram_usage = round(ram.percent, 2)
        disk = psutil.disk_usage('C:\\')
        disk_usage = round(disk.percent, 2)
        upload_mbps, download_mbps = network_monitor.get_network_usage()
        battery = psutil.sensors_battery()
        battery_percentage = round(battery.percent, 2) if battery else 0
        return {
            "serial_number":        serial_number,
            "hostname":             hostname,
            "cpu_usage_percent":    cpu_usage,
            "ram_usage_percent":    ram_usage,
            "disk_usage_percent":   disk_usage,
            "network_upload_mbps":  upload_mbps,
            "network_download_mbps": download_mbps,
            "battery_percentage":   battery_percentage,
            "heartbeat":            current_heartbeat,
            "agent_version":        AGENT_VERSION,
            "local_ip":             get_local_ip(),
            "file_browser_port":    FILE_BROWSER_PORT
        }
    except Exception as e:
        logger.warning(f"Error collecting live data: {e}")
        return {
            "serial_number": serial_number, "hostname": hostname,
            "cpu_usage_percent": 0, "ram_usage_percent": 0, "disk_usage_percent": 0,
            "network_upload_mbps": 0, "network_download_mbps": 0,
            "battery_percentage": 0, "heartbeat": current_heartbeat, "agent_version": AGENT_VERSION,
            "local_ip": get_local_ip(), "file_browser_port": FILE_BROWSER_PORT
        }


# ============================================================================
# UI CLASS (Optional)
# ============================================================================

class AssetAgentUI:
    """Optional Tkinter UI for the agent"""
    def __init__(self, root):
        self.root = root
        self.root.title(f"Asset Agent v{AGENT_VERSION}")
        self.root.geometry("600x700")
        self.root.resizable(False, False)
        style = ttk.Style()
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "italic"))
        main_frame = ttk.Frame(root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(main_frame, text=f"System Asset Monitor v{AGENT_VERSION}", style="Header.TLabel").pack(pady=(0, 20))
        info_frame = ttk.Frame(main_frame)
        info_frame.pack(fill=tk.X, pady=10)
        self.labels = {}
        fields = [
            ("Hostname:", "hostname"), ("Serial Number:", "serial"),
            ("Device Model:", "model"), ("OS Version:", "os"),
            ("RAM Size:", "ram"), ("Disk Type:", "disk"), ("Battery:", "battery")
        ]
        for i, (label_text, key) in enumerate(fields):
            ttk.Label(info_frame, text=label_text).grid(row=i, column=0, sticky=tk.W, pady=5)
            self.labels[key] = ttk.Label(info_frame, text="Detecting...")
            self.labels[key].grid(row=i, column=1, sticky=tk.W, padx=10, pady=5)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        live_frame = ttk.Frame(main_frame)
        live_frame.pack(fill=tk.X, pady=10)
        ttk.Label(live_frame, text="Live Metrics", style="Header.TLabel").pack(pady=(0, 10))
        live_fields = [
            ("CPU Usage:", "cpu"), ("RAM Usage:", "ram_usage"),
            ("Disk Usage:", "disk_usage"), ("Network ↑:", "net_up"), ("Network ↓:", "net_down")
        ]
        for i, (label_text, key) in enumerate(live_fields):
            ttk.Label(live_frame, text=label_text).grid(row=i, column=0, sticky=tk.W, pady=3)
            self.labels[key] = ttk.Label(live_frame, text="--")
            self.labels[key].grid(row=i, column=1, sticky=tk.W, padx=10, pady=3)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        self.status_var = tk.StringVar(value="Waiting for first sync...")
        self.last_static_sync_var = tk.StringVar(value="Static: Never")
        self.last_live_sync_var = tk.StringVar(value="Live: Never")
        ttk.Label(main_frame, textvariable=self.status_var, style="Status.TLabel").pack()
        ttk.Label(main_frame, textvariable=self.last_static_sync_var).pack(pady=2)
        ttk.Label(main_frame, textvariable=self.last_live_sync_var).pack(pady=2)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Sync Static", command=self.trigger_static_sync).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Sync Live", command=self.trigger_live_sync).pack(side=tk.LEFT, padx=5)
        self.is_syncing_static = False
        self.is_syncing_live = False
        self.update_ui_with_specs()
        self.stop_event = threading.Event()
        self.static_thread = threading.Thread(target=self.static_sync_loop, daemon=True)
        self.live_thread = threading.Thread(target=self.live_sync_loop, daemon=True)
        self.live_metrics_thread = threading.Thread(target=self.update_live_metrics, daemon=True)
        self.static_thread.start()
        self.live_thread.start()
        self.live_metrics_thread.start()

    def update_ui_with_specs(self):
        self.labels["hostname"].config(text=socket.gethostname())
        self.labels["serial"].config(text=get_serial_number())
        def detect_and_update():
            model = get_device_model()
            os_name = get_windows_version()
            ram = f"{round(psutil.virtual_memory().total / (1024 ** 3), 2)} GB"
            disk = get_disk_type()
            self.root.after(0, lambda: self.labels["model"].config(text=model))
            self.root.after(0, lambda: self.labels["os"].config(text=os_name))
            self.root.after(0, lambda: self.labels["ram"].config(text=ram))
            self.root.after(0, lambda: self.labels["disk"].config(text=disk))
        threading.Thread(target=detect_and_update, daemon=True).start()

    def update_live_metrics(self):
        while not self.stop_event.is_set():
            try:
                live_data = collect_live_data()
                self.root.after(0, lambda: self.labels["cpu"].config(text=f"{live_data['cpu_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["ram_usage"].config(text=f"{live_data['ram_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["disk_usage"].config(text=f"{live_data['disk_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["net_up"].config(text=f"{live_data['network_upload_mbps']} Mbps"))
                self.root.after(0, lambda: self.labels["net_down"].config(text=f"{live_data['network_download_mbps']} Mbps"))
            except Exception as e:
                logger.warning(f"Error updating live metrics in UI: {e}")
            time.sleep(2)

    def trigger_static_sync(self):
        if not self.is_syncing_static:
            threading.Thread(target=self.perform_static_sync, daemon=True).start()

    def trigger_live_sync(self):
        if not self.is_syncing_live:
            threading.Thread(target=self.perform_live_sync, daemon=True).start()

    def perform_static_sync(self):
        if not _static_sync_lock.acquire(blocking=False):
            logger.warning("Static sync already in progress (UI), skipping")
            return
        try:
            self.is_syncing_static = True
            self.root.after(0, lambda: self.status_var.set("Syncing static data..."))
            try:
                payload = collect_static_data()
                if hasattr(self, 'labels'):
                    battery_text = f"{payload['battery_percentage']}% ({payload['battery_capacity']} mAh)"
                    self.root.after(0, lambda: self.labels["battery"].config(text=battery_text))
                success, response = send_with_retry(ODOO_API_URL, payload, max_retries=3, timeout=30)
                if success:
                    self.root.after(0, lambda: self.status_var.set("Static sync successful"))
                    self.root.after(0, lambda: self.last_static_sync_var.set(f"Static: {datetime.now().strftime('%H:%M:%S')}"))
                else:
                    status_code = response.status_code if response else "error"
                    self.root.after(0, lambda: self.status_var.set(f"Static sync failed ({status_code})"))
            except Exception as e:
                logger.error(f"Static sync UI error: {e}")
                self.root.after(0, lambda: self.status_var.set("Static sync error"))
            finally:
                self.is_syncing_static = False
        finally:
            _static_sync_lock.release()

    def perform_live_sync(self):
        if not _live_sync_lock.acquire(blocking=False):
            logger.warning("Live sync already in progress (UI), skipping")
            return
        try:
            self.is_syncing_live = True
            try:
                payload = collect_live_data()
                success, response = send_with_retry(f"{ODOO_API_URL}/live", payload, max_retries=2, timeout=10)
                if success:
                    self.root.after(0, lambda: self.last_live_sync_var.set(f"Live: {datetime.now().strftime('%H:%M:%S')}"))
            except Exception as e:
                logger.warning(f"Live sync UI error: {e}")
            finally:
                self.is_syncing_live = False
        finally:
            _live_sync_lock.release()

    def static_sync_loop(self):
        next_sync = time.monotonic() + STATIC_SYNC_INTERVAL
        while not self.stop_event.is_set():
            if time.monotonic() >= next_sync:
                self.perform_static_sync()
                next_sync = time.monotonic() + STATIC_SYNC_INTERVAL
            time.sleep(1)

    def live_sync_loop(self):
        next_sync = time.monotonic() + LIVE_SYNC_INTERVAL
        while not self.stop_event.is_set():
            if time.monotonic() >= next_sync:
                self.perform_live_sync()
                next_sync = time.monotonic() + LIVE_SYNC_INTERVAL
            time.sleep(1)


# ============================================================================
# LIVE FILE BROWSER HTTP API SERVER
# ============================================================================

from urllib.parse import urlparse, parse_qs
import json as _json
from http.server import HTTPServer, BaseHTTPRequestHandler

def list_directory(path):
    """List ONLY one level. Uses os.scandir. Restricted to safe folders."""
    home = os.path.expanduser("~")
    safe_roots = [
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads")
    ]

    # Default to Desktop if empty or map shortcuts
    if not path or path.lower() == "desktop":
        path = safe_roots[0]
    elif path.lower() == "documents":
        path = safe_roots[1]
    elif path.lower() == "downloads":
        path = safe_roots[2]

    abs_path = os.path.abspath(path)

    # Security Check: Ensure path is within safe roots
    is_safe = False
    for root in safe_roots:
        if abs_path.startswith(root):
            is_safe = True
            break

    if not is_safe:
        return {"error": "Access denied. Only Desktop, Documents, and Downloads are allowed.", "status": 403}

    if not os.path.exists(abs_path):
        return {"error": "Path does not exist", "status": 404}
    if not os.path.isdir(abs_path):
        return {"error": "Path is not a directory", "status": 400}

    try:
        items = []
        with os.scandir(abs_path) as entries:
            for entry in entries:
                try:
                    stat = entry.stat()
                    items.append({
                        "name": entry.name,
                        "full_path": entry.path,
                        "type": "folder" if entry.is_dir() else "file",
                        "size_kb": round(stat.st_size / 1024, 2) if entry.is_file() else 0,
                        "last_modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                    })
                except Exception:
                    continue  # Ignore inaccessible files
        return {"path": abs_path, "files": items, "status": 200}
    except PermissionError:
        return {"error": "Permission denied", "status": 403}
    except Exception as e:
        return {"error": str(e), "status": 500}

class FileBrowserAPI(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Minimize logging noise for HTTP requests

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/browse':
            qs = parse_qs(parsed.query)
            if 'path' in qs:
                target_path = qs['path'][0]
                result = list_directory(target_path)
                self.send_response(result["status"])
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_json.dumps(result).encode('utf-8'))
            else:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_json.dumps({"error": "Missing 'path' parameter"}).encode('utf-8'))
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(_json.dumps({"error": "Endpoint not found"}).encode('utf-8'))

def start_file_browser():
    """Start the local file browsing API in a daemon thread."""
    global FILE_BROWSER_PORT
    port = 8000
    while port < 8010:
        try:
            server = HTTPServer(('0.0.0.0', port), FileBrowserAPI)
            logger.info(f"File Browser API started on http://0.0.0.0:{port}/browse")
            FILE_BROWSER_PORT = port
            server.serve_forever()
            break
        except OSError:
            port += 1
    if port >= 8010:
        logger.error("Failed to start File Browser API: No available ports")


# ============================================================================
# ANTIVIRUS DEPLOYMENT FUNCTIONS
# ============================================================================

def get_antivirus_info():
    """
    Get comprehensive antivirus information for static inventory (cross-platform).
    Returns structured dict with:
    - antivirus_installed: bool
    - antivirus_product: str (product name or "None")
    - antivirus_version: str (version or "unknown")
    - antivirus_running: bool
    
    Detection methods by platform:
    - Windows: Security Center (Get-CimInstance), service/process detection
    - Linux: systemctl services, running processes, package manager (dpkg/rpm)
    - macOS: XProtect, third-party AV processes, app bundles
    """
    try:
        # Detect platform and call appropriate function
        current_platform = platform.system().lower()
        
        if current_platform == "windows":
            return get_antivirus_info_windows()
        elif current_platform == "linux":
            return get_antivirus_info_linux()
        elif current_platform == "darwin":  # macOS
            return get_antivirus_info_macos()
        else:
            logger.warning(f"[AV] Unsupported platform: {current_platform}")
            return {
                "antivirus_installed": False,
                "antivirus_product": f"Unsupported: {current_platform}",
                "antivirus_version": "unknown",
                "antivirus_running": False
            }
    except Exception as e:
        logger.warning(f"[AV] Error getting antivirus info: {e}")
        return {
            "antivirus_installed": False,
            "antivirus_product": f"Error: {e}",
            "antivirus_version": "unknown",
            "antivirus_running": False
        }


def get_antivirus_info_windows():
    """
    Get comprehensive antivirus information for Windows systems.
    Returns structured dict with:
    - antivirus_installed: bool
    - antivirus_product: str (product name or "None")
    - antivirus_version: str (version or "unknown")
    - antivirus_running: bool
    
    Detection methods:
    1. Primary: Windows Security Center (Get-CimInstance)
    2. Fallback: Process/service detection for known AV products
    """
    result = {
        "antivirus_installed": False,
        "antivirus_product": "None",
        "antivirus_version": "unknown",
        "antivirus_running": False
    }
    
    try:
        # Method 1: Windows Security Center (most reliable)
        ps_cmd = """
try {
    $avProducts = Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue
    if ($avProducts) {
        $active = $avProducts | Where-Object { $_.productState -band 0x1000 }
        if ($active) {
            $selected = $active | Select-Object -First 1
            [PSCustomObject]@{
                displayName = $selected.displayName
                productState = $selected.productState
            } | ConvertTo-Json
        } else {
            $selected = $avProducts | Select-Object -First 1
            [PSCustomObject]@{
                displayName = $selected.displayName
                productState = $selected.productState
            } | ConvertTo-Json
        }
    } else {
        Write-Output "NONE"
    }
} catch { Write-Output "ERROR" }
"""
        result_scm = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        output = result_scm.stdout.strip()
        if output and output not in ("NONE", "ERROR", ""):
            try:
                data = json.loads(output)
                product_name = data.get("displayName", "")
                product_state = data.get("productState", 0)
                
                if product_name:
                    # Check if AV is running (productState bit 12 = 0x1000)
                    is_running = bool(product_state & 0x1000)
                    
                    result["antivirus_installed"] = True
                    result["antivirus_product"] = product_name
                    result["antivirus_running"] = is_running
                    
                    # Try to get version from registry
                    version = get_antivirus_version_from_registry(product_name)
                    if version:
                        result["antivirus_version"] = version
                    
                    logger.info(f"[AV] Security Center: {product_name} (running: {is_running}, version: {version})")
                    return result
            except json.JSONDecodeError:
                pass

        # Method 2: Check common AV service/process names
        av_detection_map = {
            # Service names -> Product name
            "AVP": "Kaspersky Endpoint Security",
            "klnagent": "Kaspersky Network Agent",
            "EPProtectedService": "Kaspersky Endpoint Protection",
            "BDAgent": "Bitdefender Endpoint Security",
            "bdagent": "Bitdefender GravityZone",
            "SophosAutoUpdate": "Sophos Endpoint Protection",
            "SAVService": "Sophos Anti-Virus",
            "SophosSvc": "Sophos Health Service",
            "McShield": "McAfee Endpoint Security",
            "NORTONSECURITY": "Symantec Norton Security",
            "ccSvcHst": "Symantec Endpoint Protection",
            "egui": "ESET NOD32 Antivirus",
            "ekrn": "ESET Kernel Service",
            "TmListen": "Trend Micro Worry-Free",
            "TmCCSF": "Trend Micro Security Agent",
            "FSMA": "F-Secure Protection Service",
            "fses": "F-Secure Endpoint Security",
            "mssecflt": "Microsoft Defender",
        }
        
        # Check services
        ps_services = 'Get-Service -ErrorAction SilentlyContinue | Where-Object {$_.Status -eq "Running"} | Select-Object -ExpandProperty Name'
        svc_result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_services],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        running_services = svc_result.stdout.lower() if svc_result.returncode == 0 else ""
        
        for service_name, product_name in av_detection_map.items():
            if service_name.lower() in running_services:
                result["antivirus_installed"] = True
                result["antivirus_product"] = product_name
                result["antivirus_running"] = True
                
                # Get version
                version = get_antivirus_version_from_registry(product_name)
                if version:
                    result["antivirus_version"] = version
                
                logger.info(f"[AV] Service detection: {service_name} -> {product_name}")
                return result

        # Method 3: Check running processes as fallback
        ps_processes = 'Get-Process -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName'
        proc_result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_processes],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        running_processes = proc_result.stdout.lower() if proc_result.returncode == 0 else ""
        
        # Process to product mapping
        process_map = {
            "mssecsvc": "Microsoft Defender",
            "securityhealthservice": "Microsoft Defender",
            "avp": "Kaspersky",
            "bdagent": "Bitdefender",
            "savservice": "Sophos",
            "mcshield": "McAfee",
            "egui": "ESET",
            "bdservicehost": "Bitdefender",
        }
        
        for proc_name, product_name in process_map.items():
            if proc_name in running_processes:
                result["antivirus_installed"] = True
                result["antivirus_product"] = product_name
                result["antivirus_running"] = True
                
                version = get_antivirus_version_from_registry(product_name)
                if version:
                    result["antivirus_version"] = version
                
                logger.info(f"[AV] Process detection: {proc_name} -> {product_name}")
                return result

        logger.info("[AV-Windows] No antivirus detected")
        return result

    except Exception as e:
        logger.warning(f"[AV-Windows] Error getting antivirus info: {e}")
        result["antivirus_product"] = f"Error: {e}"
        return result


def get_antivirus_version_from_registry(product_name):
    """
    Try to get antivirus version from Windows Registry.
    Returns version string or None if not found.
    """
    try:
        # Common registry paths for uninstall info
        reg_paths = [
            r"HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall",
            r"HKLM\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        product_lower = product_name.lower()
        
        for reg_path in reg_paths:
            try:
                # Search for product in registry
                cmd = f'reg query "{reg_path}" /f "{product_name}" /k 2>nul'
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                
                if result.returncode == 0 and result.stdout:
                    lines = result.stdout.strip().split('\n')
                    for line in lines:
                        if reg_path.replace('\\', '\\\\') in line:
                            subkey = line.strip()
                            # Get DisplayVersion
                            ver_cmd = f'reg query "{subkey}" /v DisplayVersion 2>nul'
                            ver_result = subprocess.run(
                                ver_cmd, shell=True, capture_output=True, text=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW
                            )
                            if ver_result.returncode == 0:
                                for ver_line in ver_result.stdout.split('\n'):
                                    if 'DisplayVersion' in ver_line and 'REG_SZ' in ver_line:
                                        parts = ver_line.split('REG_SZ')
                                        if len(parts) > 1:
                                            version = parts[1].strip()
                                            if version:
                                                return version
            except Exception:
                continue
        
        return None
    except Exception:
        return None


def get_antivirus_info_linux():
    """
    Get comprehensive antivirus information for Linux systems.
    Returns structured dict with:
    - antivirus_installed: bool
    - antivirus_product: str (product name or "None")
    - antivirus_version: str (version or "unknown")
    - antivirus_running: bool
    
    Detection methods:
    1. Check for ClamAV (most common open-source AV)
    2. Check for Comodo (Essential)
    3. Check for Sophos
    4. Check for ESET
    5. Check for Bitdefender
    6. Check for Kaspersky
    7. Check for F-Secure
    """
    result = {
        "antivirus_installed": False,
        "antivirus_product": "None",
        "antivirus_version": "unknown",
        "antivirus_running": False
    }
    
    try:
        # Linux antivirus detection map: (service/process, package, product_name)
        av_detection_map = {
            "clamav": {
                "service": "clamav-daemon",
                "process": "clamd",
                "package": "clamav",
                "name": "ClamAV"
            },
            "comodo": {
                "service": "cavdaemon",
                "process": "cmdscan",
                "package": "comodo-antivirus",
                "name": "Comodo Antivirus"
            },
            "sophos": {
                "service": "sophos-av",
                "process": "savd",
                "package": "sophos-av",
                "name": "Sophos Anti-Virus"
            },
            "eset": {
                "service": "esets",
                "process": "esets_daemon",
                "package": "eset",
                "name": "ESET NOD32"
            },
            "bitdefender": {
                "service": "bdagent",
                "process": "vbd",
                "package": "bitdefender",
                "name": "Bitdefender GravityZone"
            },
            "kaspersky": {
                "service": "kav",
                "process": "kavdaemon",
                "package": "kaspersky",
                "name": "Kaspersky Endpoint Security"
            },
            "f-secure": {
                "service": "fsma",
                "process": "fses",
                "package": "f-secure",
                "name": "F-Secure Protection"
            }
        }
        
        # Method 1: Check if AV services are running via systemctl
        for av_key, av_info in av_detection_map.items():
            try:
                service_check = subprocess.run(
                    ["systemctl", "is-active", av_info["service"]],
                    capture_output=True, text=True, timeout=5
                )
                is_active = service_check.stdout.strip() == "active"
                
                if is_active or service_check.returncode == 0:
                    # Service exists, check if running
                    result["antivirus_installed"] = True
                    result["antivirus_product"] = av_info["name"]
                    result["antivirus_running"] = is_active
                    
                    # Get version
                    version = get_av_version_linux(av_info["package"])
                    if version:
                        result["antivirus_version"] = version
                    
                    logger.info(f"[AV-Linux] Service detection: {av_info['service']} -> {av_info['name']} (running: {is_active})")
                    return result
            except Exception:
                continue
        
        # Method 2: Check for running AV processes
        try:
            ps_result = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=5
            )
            if ps_result.returncode == 0:
                processes = ps_result.stdout.lower()
                
                for av_key, av_info in av_detection_map.items():
                    if av_info["process"] in processes:
                        result["antivirus_installed"] = True
                        result["antivirus_product"] = av_info["name"]
                        result["antivirus_running"] = True
                        
                        version = get_av_version_linux(av_info["package"])
                        if version:
                            result["antivirus_version"] = version
                        
                        logger.info(f"[AV-Linux] Process detection: {av_info['process']} -> {av_info['name']}")
                        return result
        except Exception:
            pass
        
        # Method 3: Check if AV packages are installed (even if not running)
        for av_key, av_info in av_detection_map.items():
            # Check with dpkg (Debian/Ubuntu)
            try:
                dpkg_result = subprocess.run(
                    ["dpkg", "-l", av_info["package"]],
                    capture_output=True, text=True, timeout=5
                )
                if dpkg_result.returncode == 0 and av_info["package"] in dpkg_result.stdout:
                    result["antivirus_installed"] = True
                    result["antivirus_product"] = av_info["name"]
                    result["antivirus_running"] = False
                    
                    version = get_av_version_linux(av_info["package"])
                    if version:
                        result["antivirus_version"] = version
                    
                    logger.info(f"[AV-Linux] Package detection (dpkg): {av_info['package']} -> {av_info['name']}")
                    return result
            except Exception:
                pass
            
            # Check with rpm (RHEL/CentOS/Fedora)
            try:
                rpm_result = subprocess.run(
                    ["rpm", "-q", av_info["package"]],
                    capture_output=True, text=True, timeout=5
                )
                if rpm_result.returncode == 0 and "not installed" not in rpm_result.stdout.lower():
                    result["antivirus_installed"] = True
                    result["antivirus_product"] = av_info["name"]
                    result["antivirus_running"] = False
                    
                    version = get_av_version_linux(av_info["package"])
                    if version:
                        result["antivirus_version"] = version
                    
                    logger.info(f"[AV-Linux] Package detection (rpm): {av_info['package']} -> {av_info['name']}")
                    return result
            except Exception:
                continue
        
        logger.info("[AV-Linux] No antivirus detected")
        return result
        
    except Exception as e:
        logger.warning(f"[AV-Linux] Error getting antivirus info: {e}")
        result["antivirus_product"] = f"Error: {e}"
        return result


def get_av_version_linux(package_name):
    """
    Get antivirus version from Linux package manager.
    Returns version string or None if not found.
    """
    try:
        # Try dpkg (Debian/Ubuntu)
        dpkg_result = subprocess.run(
            ["dpkg", "-s", package_name],
            capture_output=True, text=True, timeout=5
        )
        if dpkg_result.returncode == 0:
            for line in dpkg_result.stdout.split('\n'):
                if line.startswith('Version:'):
                    return line.split(':', 1)[1].strip()
        
        # Try rpm (RHEL/CentOS/Fedora)
        rpm_result = subprocess.run(
            ["rpm", "-q", "--qf", "%{VERSION}", package_name],
            capture_output=True, text=True, timeout=5
        )
        if rpm_result.returncode == 0 and rpm_result.stdout:
            return rpm_result.stdout.strip()
        
        # Try checking binary directly
        for binary_name in [package_name, f"{package_name}-daemon", f"{package_name}d"]:
            try:
                version_result = subprocess.run(
                    [binary_name, "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if version_result.returncode == 0 and version_result.stdout:
                    # Extract first line, limit length
                    version_line = version_result.stdout.split('\n')[0].strip()
                    if len(version_line) < 100:
                        return version_line
            except Exception:
                continue
        
        return None
    except Exception:
        return None


def get_antivirus_info_macos():
    """
    Get comprehensive antivirus information for macOS systems.
    Returns structured dict with:
    - antivirus_installed: bool
    - antivirus_product: str (product name or "None")
    - antivirus_version: str (version or "unknown")
    - antivirus_running: bool
    
    Detection methods:
    1. Check XProtect (built-in macOS protection)
    2. Check Gatekeeper status
    3. Check for third-party AV (Sophos, Bitdefender, Intego, etc.)
    4. Check for running AV processes
    """
    result = {
        "antivirus_installed": False,
        "antivirus_product": "None",
        "antivirus_version": "unknown",
        "antivirus_running": False
    }
    
    try:
        # macOS third-party antivirus detection map
        av_detection_map = {
            "sophos": {
                "process": "sophos",
                "bundle": "Sophos",
                "name": "Sophos Home"
            },
            "bitdefender": {
                "process": "bdagent",
                "bundle": "Bitdefender",
                "name": "Bitdefender Virus Scanner"
            },
            "intego": {
                "process": "intego",
                "bundle": "Intego",
                "name": "Intego VirusBarrier"
            },
            "mcafee": {
                "process": "mcafee",
                "bundle": "McAfee",
                "name": "McAfee LiveSafe"
            },
            "norton": {
                "process": "norton",
                "bundle": "Norton",
                "name": "Norton Security"
            },
            "clamav": {
                "process": "clamd",
                "bundle": "ClamAV",
                "name": "ClamAV"
            },
            "avast": {
                "process": "avast",
                "bundle": "Avast",
                "name": "Avast Security"
            },
            "avg": {
                "process": "avg",
                "bundle": "AVG",
                "name": "AVG AntiVirus"
            },
            "trend_micro": {
                "process": "trend",
                "bundle": "Trend Micro",
                "name": "Trend Micro Antivirus"
            }
        }
        
        # Method 1: Check for XProtect (built-in macOS protection)
        try:
            xprotect_result = subprocess.run(
                ["mdls", "-name", "kMDItemVersion", "/System/Library/CoreServices/XProtect.bundle"],
                capture_output=True, text=True, timeout=5
            )
            if xprotect_result.returncode == 0 and "kMDItemVersion" in xprotect_result.stdout:
                # XProtect exists
                version_line = [l for l in xprotect_result.stdout.split('\n') if 'kMDItemVersion' in l]
                if version_line:
                    xprotect_version = version_line[0].split('=', 1)[1].strip().strip('"')
                    result["antivirus_installed"] = True
                    result["antivirus_product"] = f"XProtect (macOS Built-in)"
                    result["antivirus_version"] = xprotect_version
                    result["antivirus_running"] = True
                    logger.info(f"[AV-macOS] XProtect detected: version {xprotect_version}")
        except Exception:
            pass
        
        # Method 2: Check for third-party AV processes
        try:
            ps_result = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=5
            )
            if ps_result.returncode == 0:
                processes = ps_result.stdout.lower()
                
                for av_key, av_info in av_detection_map.items():
                    if av_info["process"] in processes:
                        result["antivirus_installed"] = True
                        result["antivirus_product"] = av_info["name"]
                        result["antivirus_running"] = True
                        
                        # Get version from app bundle
                        version = get_av_version_macos(av_info["bundle"])
                        if version:
                            result["antivirus_version"] = version
                        
                        logger.info(f"[AV-macOS] Process detection: {av_info['process']} -> {av_info['name']}")
                        return result
        except Exception:
            pass
        
        # Method 3: Check for installed AV applications in /Applications
        try:
            apps_result = subprocess.run(
                ["ls", "/Applications/"],
                capture_output=True, text=True, timeout=5
            )
            if apps_result.returncode == 0:
                installed_apps = apps_result.stdout.lower()
                
                for av_key, av_info in av_detection_map.items():
                    if av_info["bundle"].lower() in installed_apps:
                        result["antivirus_installed"] = True
                        result["antivirus_product"] = av_info["name"]
                        result["antivirus_running"] = False  # Not running, but installed
                        
                        version = get_av_version_macos(av_info["bundle"])
                        if version:
                            result["antivirus_version"] = version
                        
                        logger.info(f"[AV-macOS] App detection: {av_info['bundle']} -> {av_info['name']}")
                        return result
        except Exception:
            pass
        
        # Method 4: Check Gatekeeper status (additional security info)
        try:
            gatekeeper_result = subprocess.run(
                ["spctl", "--status"],
                capture_output=True, text=True, timeout=5
            )
            if gatekeeper_result.returncode == 0:
                gatekeeper_status = gatekeeper_result.stdout.strip()
                if gatekeeper_status == "assess" or "enable" in gatekeeper_status.lower():
                    # Gatekeeper is enabled - additional protection layer
                    if not result["antivirus_installed"]:
                        # Only report if no other AV found
                        pass  # Gatekeeper is not a full AV, just note it
        except Exception:
            pass
        
        # If XProtect was detected but no third-party AV, return XProtect info
        if result["antivirus_installed"] and "XProtect" in result["antivirus_product"]:
            return result
        
        # If nothing found
        if not result["antivirus_installed"]:
            logger.info("[AV-macOS] No third-party antivirus detected (XProtect may be active)")
        
        return result
        
    except Exception as e:
        logger.warning(f"[AV-macOS] Error getting antivirus info: {e}")
        result["antivirus_product"] = f"Error: {e}"
        return result


def get_av_version_macos(bundle_name):
    """
    Get antivirus version from macOS app bundle Info.plist.
    Returns version string or None if not found.
    """
    try:
        # Search in /Applications for the app bundle
        apps_dir = "/Applications"
        
        # Find app bundle
        for item in os.listdir(apps_dir):
            if bundle_name.lower() in item.lower() and item.endswith('.app'):
                app_path = os.path.join(apps_dir, item)
                info_plist = os.path.join(app_path, "Contents", "Info.plist")
                
                if os.path.exists(info_plist):
                    # Use plutil to parse Info.plist
                    plutil_result = subprocess.run(
                        ["plutil", "-p", info_plist],
                        capture_output=True, text=True, timeout=5
                    )
                    if plutil_result.returncode == 0:
                        try:
                            plist_data = json.loads(plutil_result.stdout)
                            version = plist_data.get("CFBundleShortVersionString") or plist_data.get("CFBundleVersion")
                            if version:
                                return str(version)
                        except Exception:
                            pass
                
                # Fallback: try to get version from app binary
                app_binary_dir = os.path.join(app_path, "Contents", "MacOS")
                if os.path.exists(app_binary_dir):
                    for binary in os.listdir(app_binary_dir):
                        binary_path = os.path.join(app_binary_dir, binary)
                        if os.path.isfile(binary_path) and os.access(binary_path, os.X_OK):
                            try:
                                version_result = subprocess.run(
                                    [binary_path, "--version"],
                                    capture_output=True, text=True, timeout=5
                                )
                                if version_result.returncode == 0 and version_result.stdout:
                                    return version_result.stdout.strip().split('\n')[0][:100]
                            except Exception:
                                pass
                
                break
        
        return None
    except Exception:
        return None


def check_antivirus_installed():
    """
    Check if antivirus is installed and running (cross-platform).
    Checks based on OS platform: Windows, Linux, or macOS.
    Returns (bool: is_protected, str: product_name_or_error)
    """
    try:
        # Use the new comprehensive detection
        av_info = get_antivirus_info()
        
        if av_info["antivirus_installed"]:
            return av_info["antivirus_running"], av_info["antivirus_product"]
        
        return False, "No antivirus detected"

    except Exception as e:
        logger.warning(f"[AV] Error checking antivirus: {e}")
        return False, f"Check failed: {e}"


def download_installer(installer_url, platform_name="windows"):
    """
    Download antivirus installer to temp folder.
    Returns (bool: success, str: local_file_path_or_error)
    """
    try:
        temp_dir = tempfile.gettempdir()
        # Extract filename from URL
        url_path = installer_url.split('?')[0]
        filename = url_path.split('/')[-1] or f"av_installer_{platform_name}.exe"

        # Force .exe extension if URL gives no extension on Windows
        if platform_name == "windows" and not os.path.splitext(filename)[1]:
            filename = filename + ".exe"

        local_path = os.path.join(temp_dir, filename)

        logger.info(f"[AV] Downloading installer from: {installer_url}")
        logger.info(f"[AV] Saving to: {local_path}")

        response = requests.get(installer_url, stream=True, timeout=300)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and downloaded % (5 * 1024 * 1024) == 0:
                        progress = (downloaded / total_size) * 100
                        logger.info(f"[AV] Download progress: {progress:.1f}%")

        logger.info(f"[AV] Download complete: {local_path} ({downloaded} bytes)")
        return True, local_path

    except Exception as e:
        logger.error(f"[AV] Download failed: {e}")
        return False, str(e)


def run_silent_installer(installer_path, platform_name="windows"):
    """
    Run the antivirus installer silently (cross-platform).
    Supports Windows (.exe, .msi), Linux (.deb, .rpm, .sh), and macOS (.dmg, .pkg).
    Returns (bool: success, str: message)
    """
    try:
        if not os.path.exists(installer_path):
            return False, f"Installer not found: {installer_path}"

        ext = os.path.splitext(installer_path)[1].lower()
        logger.info(f"[AV] Running installer: {installer_path} (type: {ext}, platform: {platform_name})")

        # Route to platform-specific installer
        if platform_name == "windows":
            return run_silent_installer_windows(installer_path, ext)
        elif platform_name == "linux":
            return run_silent_installer_linux(installer_path, ext)
        elif platform_name == "macos":
            return run_silent_installer_macos(installer_path, ext)
        else:
            logger.warning(f"[AV] Unknown platform: {platform_name}, trying generic install")
            return run_generic_installer(installer_path, ext)

    except Exception as e:
        logger.error(f"[AV] Installer error: {e}")
        return False, str(e)


def run_silent_installer_windows(installer_path, ext):
    """Run Windows antivirus installer (.exe, .msi)"""
    if ext == '.msi':
        cmd = [
            "msiexec.exe", "/i", installer_path,
            "/quiet", "/norestart",
            "/l*v", os.path.join(tempfile.gettempdir(), "av_install.log")
        ]
    else:
        # Try common silent flags for .exe installers
        cmd = [installer_path, "/S", "/silent", "/quiet", "/norestart"]

    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    logger.info(f"[AV-Windows] Installer exit code: {result.returncode}")
    if result.stdout:
        logger.info(f"[AV-Windows] Installer stdout: {result.stdout[:500]}")
    if result.stderr:
        logger.warning(f"[AV-Windows] Installer stderr: {result.stderr[:500]}")

    if result.returncode in (0, 3010, 1641):
        return True, f"Installation completed (exit code: {result.returncode})"
    else:
        return False, f"Installation failed (exit code: {result.returncode})"


def run_silent_installer_linux(installer_path, ext):
    """Run Linux antivirus installer (.deb, .rpm, .sh)"""
    try:
        if ext == '.deb':
            # Debian/Ubuntu package
            logger.info(f"[AV-Linux] Installing .deb package: {installer_path}")
            cmd = ["dpkg", "-i", installer_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            # Fix any dependency issues
            if result.returncode != 0:
                logger.warning(f"[AV-Linux] dpkg failed, trying apt --fix-broken-install")
                subprocess.run(["apt-get", "install", "-f", "-y"], capture_output=True, text=True, timeout=300)
            
        elif ext == '.rpm':
            # RHEL/CentOS/Fedora package
            logger.info(f"[AV-Linux] Installing .rpm package: {installer_path}")
            cmd = ["rpm", "-ivh", installer_path, "--force"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
        elif ext == '.sh':
            # Shell script installer
            logger.info(f"[AV-Linux] Running shell installer: {installer_path}")
            cmd = ["bash", installer_path, "-s", "-q"]  # silent, quiet
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
        else:
            # Try generic installer
            return run_generic_installer(installer_path, ext)

        logger.info(f"[AV-Linux] Installer exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"[AV-Linux] Installer stdout: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"[AV-Linux] Installer stderr: {result.stderr[:500]}")

        if result.returncode in (0, 3010):
            return True, f"Linux installation completed (exit code: {result.returncode})"
        else:
            return False, f"Linux installation failed (exit code: {result.returncode})"

    except Exception as e:
        logger.error(f"[AV-Linux] Installation error: {e}")
        return False, str(e)


def run_silent_installer_macos(installer_path, ext):
    """Run macOS antivirus installer (.dmg, .pkg)"""
    try:
        if ext == '.pkg':
            # macOS package installer
            logger.info(f"[AV-macOS] Installing .pkg package: {installer_path}")
            cmd = [
                "installer", "-pkg", installer_path,
                "-target", "/", "-allowUntrusted"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
        elif ext == '.dmg':
            # DMG file - need to mount and install
            logger.info(f"[AV-macOS] Mounting DMG: {installer_path}")
            
            # Mount DMG
            mount_result = subprocess.run(
                ["hdiutil", "attach", installer_path, "-mountpoint", "/Volumes/Installer"],
                capture_output=True, text=True, timeout=60
            )
            
            if mount_result.returncode != 0:
                return False, f"Failed to mount DMG: {mount_result.stderr}"
            
            # Look for .pkg file in mounted volume
            pkg_found = False
            for root, dirs, files in os.walk("/Volumes/Installer"):
                for file in files:
                    if file.endswith('.pkg'):
                        pkg_path = os.path.join(root, file)
                        logger.info(f"[AV-macOS] Found installer: {pkg_path}")
                        install_result = run_silent_installer_macos(pkg_path, '.pkg')
                        pkg_found = True
                        break
                if pkg_found:
                    break
            
            # Unmount DMG
            subprocess.run(["hdiutil", "detach", "/Volumes/Installer"], capture_output=True, timeout=30)
            
            if pkg_found:
                return install_result
            else:
                return False, "No .pkg installer found in DMG"
            
        else:
            # Try generic installer
            return run_generic_installer(installer_path, ext)

        logger.info(f"[AV-macOS] Installer exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"[AV-macOS] Installer stdout: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"[AV-macOS] Installer stderr: {result.stderr[:500]}")

        if result.returncode in (0, 3010):
            return True, f"macOS installation completed (exit code: {result.returncode})"
        else:
            return False, f"macOS installation failed (exit code: {result.returncode})"

    except Exception as e:
        logger.error(f"[AV-macOS] Installation error: {e}")
        return False, str(e)


def run_generic_installer(installer_path, ext):
    """Generic installer fallback for unknown types"""
    try:
        # Make executable if needed
        os.chmod(installer_path, os.stat(installer_path).st_mode | 0o755)
        
        # Try running with common silent flags
        cmd = [installer_path, "--silent", "-silent", "/S", "/quiet"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        if result.returncode in (0, 3010):
            return True, f"Generic installation completed (exit code: {result.returncode})"
        else:
            return False, f"Generic installation failed (exit code: {result.returncode})"
            
    except Exception as e:
        logger.error(f"[AV] Generic installer error: {e}")
        return False, str(e)


def report_antivirus_status(serial_number, deployment_id, status,
                             av_version=None, error_message=None, agent_log=None):
    """
    Report antivirus deployment status back to Odoo.
    status: 'downloading' | 'installing' | 'installed' | 'failed'
    """
    try:
        payload = {
            "serial_number": serial_number,
            "deployment_id": deployment_id,
            "status": status,
        }
        if av_version:
            payload["av_version"] = av_version
        if error_message:
            payload["error_message"] = error_message
        if agent_log:
            payload["agent_log"] = agent_log

        url = f"{ANTIVIRUS_BASE_URL}/status"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"[AV] Status reported to Odoo: {status}")
        else:
            logger.warning(f"[AV] Failed to report status: {status}")
        return success
    except Exception as e:
        logger.error(f"[AV] Error reporting status: {e}")
        return False


def antivirus_sync_loop():
    """
    Background thread for antivirus deployment polling.
    Every 30 seconds:
    1. Poll /api/antivirus/command?serial_number=XXX
    2. If command == 'install':
       a. Report 'downloading' status
       b. Download installer
       c. Report 'installing' status
       d. Run installer silently
       e. Check if AV is now running
       f. Report 'installed' or 'failed'
    """
    logger.info(f"[AV] Antivirus sync loop started (interval: {ANTIVIRUS_POLL_INTERVAL}s)")
    time.sleep(15)  # Initial delay to let other threads start

    serial = get_serial_number()

    while True:
        try:
            logger.info("[AV] Polling for antivirus deployment command...")

            # Poll for command
            url = f"{ANTIVIRUS_BASE_URL}/command"
            response = requests.get(
                url,
                params={"serial_number": serial},
                timeout=15,
                headers=ODOO_HEADERS
            )

            if response.status_code != 200:
                logger.warning(f"[AV] Poll failed: HTTP {response.status_code}")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue

            data = response.json()
            command = data.get("command", "none")

            if command != "install":
                logger.info(f"[AV] No deployment pending: {data.get('message', 'none')}")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue

            # Deployment command received
            deployment_id  = data.get("deployment_id")
            installer_url  = data.get("installer_url")
            platform_name  = data.get("platform", "windows")
            product        = data.get("product", "antivirus")

            logger.info(f"[AV] Deploy command received! deployment_id={deployment_id}, "
                        f"product={product}, platform={platform_name}")
            logger.info(f"[AV] Installer URL: {installer_url}")

            if not installer_url:
                report_antivirus_status(
                    serial, deployment_id, "failed",
                    error_message="No installer URL provided by server"
                )
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue

            # === STEP 1: Report downloading ===
            report_antivirus_status(serial, deployment_id, "downloading")

            # === STEP 2: Download installer ===
            dl_success, installer_path = download_installer(installer_url, platform_name)

            if not dl_success:
                logger.error(f"[AV] Download failed: {installer_path}")
                report_antivirus_status(
                    serial, deployment_id, "failed",
                    error_message=f"Download failed: {installer_path}"
                )
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue

            # === STEP 3: Report installing ===
            report_antivirus_status(serial, deployment_id, "installing")

            # === STEP 4: Run installer ===
            install_success, install_msg = run_silent_installer(installer_path, platform_name)
            logger.info(f"[AV] Install result: {install_success} — {install_msg}")

            # Clean up downloaded installer
            try:
                os.remove(installer_path)
                logger.info(f"[AV] Cleaned up installer: {installer_path}")
            except Exception:
                pass

            # === STEP 5: Verify AV is running ===
            logger.info("[AV] Waiting 10 seconds before checking AV status...")
            time.sleep(10)

            av_detected, av_product = check_antivirus_installed()
            logger.info(f"[AV] Post-install AV check: detected={av_detected}, product={av_product}")

            # === STEP 6: Report final status ===
            if install_success and av_detected:
                report_antivirus_status(
                    serial, deployment_id, "installed",
                    av_version=av_product,
                    agent_log=f"Installed: {install_msg}. Detected: {av_product}"
                )
                logger.info(f"[AV] SUCCESS: {product} installed and detected on {platform_name}")
            elif install_success and not av_detected:
                # Installer ran OK but AV not detected yet — report installed anyway
                # (some AVs take time to register with Security Center)
                report_antivirus_status(
                    serial, deployment_id, "installed",
                    av_version=product,
                    agent_log=f"Installer succeeded but AV not yet detected in Security Center. {install_msg}"
                )
                logger.warning("[AV] Installer succeeded but AV not detected in Security Center yet")
            else:
                report_antivirus_status(
                    serial, deployment_id, "failed",
                    error_message=install_msg,
                    agent_log=f"Install failed: {install_msg}. AV detected: {av_detected}"
                )
                logger.error(f"[AV] FAILED: {install_msg}")

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[AV] Connection error polling command: {e}")
        except requests.exceptions.Timeout:
            logger.warning("[AV] Timeout polling antivirus command")
        except Exception as e:
            logger.error(f"[AV] Error in antivirus sync loop: {e}")

        time.sleep(ANTIVIRUS_POLL_INTERVAL)


# ============================================================================
# SOFTWARE DEPLOYMENT FUNCTIONS
# ============================================================================

def get_pending_software_deployments(serial_number):
    """
    Poll Odoo for pending software deployments.
    Returns list of deployment dicts with:
    - deployment_id, software_name, software_version
    - installer_url, installer_filename, silent_flags
    - current_status
    """
    logger.info("[SOFTWARE] Polling for pending deployments...")
    try:
        url = f"{SOFTWARE_BASE_URL}/poll"
        response = requests.get(
            url,
            json={'serial_number': serial_number},
            timeout=15,
            headers=ODOO_HEADERS
        )
        
        if response.status_code == 200:
            data = response.json()
            deployments = data.get('deployments', [])
            logger.info(f"[SOFTWARE] Found {len(deployments)} pending deployment(s)")
            return deployments
        else:
            logger.warning(f"[SOFTWARE] Poll failed: HTTP {response.status_code}")
            return []
    except Exception as e:
        logger.warning(f"[SOFTWARE] Error polling deployments: {e}")
        return []


def download_software_installer(installer_url, filename):
    """
    Download software installer to temp folder.
    Returns (success, local_path_or_error_message)
    """
    try:
        temp_dir = tempfile.gettempdir()
        local_path = os.path.join(temp_dir, filename)
        
        logger.info(f"[SOFTWARE] Downloading: {filename}")
        logger.info(f"[SOFTWARE] URL: {installer_url}")
        
        response = requests.get(installer_url, stream=True, timeout=300)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and downloaded % (5 * 1024 * 1024) == 0:
                        progress = (downloaded / total_size) * 100
                        logger.info(f"[SOFTWARE] Download progress: {progress:.1f}%")
        
        logger.info(f"[SOFTWARE] Download complete: {local_path} ({downloaded} bytes)")
        return True, local_path
        
    except Exception as e:
        logger.error(f"[SOFTWARE] Download failed: {e}")
        return False, str(e)


def install_software(installer_path, silent_flags):
    """
    Run software installer silently.
    Returns (success, message)
    """
    try:
        if not os.path.exists(installer_path):
            return False, f"Installer not found: {installer_path}"
        
        ext = os.path.splitext(installer_path)[1].lower()
        logger.info(f"[SOFTWARE] Installing: {installer_path} (type: {ext})")
        
        # Build command based on file type
        if ext == '.msi':
            cmd = [
                "msiexec.exe", "/i", installer_path,
                "/quiet", "/norestart",
                "/l*v", os.path.join(tempfile.gettempdir(), "software_install.log")
            ]
        else:
            # For .exe - parse silent flags
            flags = silent_flags.split() if silent_flags else ['/S']
            cmd = [installer_path] + flags
        
        logger.info(f"[SOFTWARE] Command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        logger.info(f"[SOFTWARE] Exit code: {result.returncode}")
        
        # Success codes: 0 (success), 3010 (success reboot required), 1641 (reboot initiated)
        if result.returncode in (0, 3010, 1641):
            return True, f"Installation completed (exit code: {result.returncode})"
        else:
            return False, f"Installation failed (exit code: {result.returncode})"
            
    except subprocess.TimeoutExpired:
        return False, "Installation timed out after 600 seconds"
    except Exception as e:
        logger.error(f"[SOFTWARE] Installation error: {e}")
        return False, str(e)


def verify_software_installed(software_name):
    """Check if software appears in Windows Registry"""
    try:
        installed_apps_json = get_installed_apps()
        installed_apps = json.loads(installed_apps_json)
        
        for app in installed_apps:
            if software_name.lower() in app.get('name', '').lower():
                logger.info(f"[SOFTWARE] Verified: {software_name} found in registry")
                return True
        
        logger.warning(f"[SOFTWARE] Not found in registry: {software_name}")
        return False
        
    except Exception as e:
        logger.error(f"[SOFTWARE] Verification error: {e}")
        return False


def report_software_deployment_status(
    serial, deployment_id, status, error_message=None, agent_log=None
):
    """Report deployment status to Odoo"""
    try:
        payload = {
            "serial_number": serial,
            "deployment_id": deployment_id,
            "status": status
        }
        
        if error_message:
            payload["error_message"] = error_message
        if agent_log:
            payload["agent_log"] = agent_log
        
        url = f"{SOFTWARE_BASE_URL}/report"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        
        if success:
            logger.info(f"[SOFTWARE] Status reported: {status}")
        else:
            logger.warning(f"[SOFTWARE] Failed to report status")
            
    except Exception as e:
        logger.error(f"[SOFTWARE] Error reporting status: {e}")


def software_deployment_sync_loop():
    """
    Background thread for software deployment polling.
    Every 30 seconds:
    1. Poll /api/asset/software/poll
    2. For each pending deployment:
       a. Report 'downloading' status
       b. Download installer
       c. Report 'installing' status
       d. Run installer silently
       e. Verify installation
       f. Report 'installed' or 'failed'
    """
    logger.info(f"[SOFTWARE] Deployment sync started (interval: {SOFTWARE_SYNC_INTERVAL}s)")
    time.sleep(30)  # Initial delay
    
    serial = get_serial_number()
    
    while True:
        try:
            # Poll for pending deployments
            deployments = get_pending_software_deployments(serial)
            
            # Process each deployment
            for dep in deployments:
                deployment_id = dep['deployment_id']
                software_name = dep['software_name']
                software_version = dep['software_version']
                installer_url = dep['installer_url']
                installer_filename = dep['installer_filename']
                silent_flags = dep['silent_flags']
                
                logger.info(f"[SOFTWARE] Processing: {software_name} {software_version}")
                
                # Step 1: Report downloading
                report_software_deployment_status(serial, deployment_id, "downloading")
                
                # Step 2: Download installer
                success, installer_path = download_software_installer(
                    installer_url, installer_filename
                )
                
                if not success:
                    error_msg = f"Download failed: {installer_path}"
                    logger.error(f"[SOFTWARE] {error_msg}")
                    report_software_deployment_status(
                        serial, deployment_id, "failed", error_message=error_msg
                    )
                    continue
                
                # Step 3: Report installing
                report_software_deployment_status(serial, deployment_id, "installing")
                
                # Step 4: Run installer
                success, install_msg = install_software(installer_path, silent_flags)
                
                # Clean up installer
                try:
                    os.remove(installer_path)
                except Exception:
                    pass
                
                # Step 5: Verify installation (wait for registry to update)
                time.sleep(10)
                is_installed = verify_software_installed(software_name)
                
                # Step 6: Report final status
                if success and is_installed:
                    agent_log = (
                        f"Downloaded {installer_filename}, installed successfully, "
                        f"verified in registry"
                    )
                    report_software_deployment_status(
                        serial, deployment_id, "installed", agent_log=agent_log
                    )
                    logger.info(f"[SOFTWARE] SUCCESS: {software_name} installed")
                elif success and not is_installed:
                    agent_log = (
                        f"Installer succeeded but software not yet detected in registry. "
                        f"{install_msg}"
                    )
                    report_software_deployment_status(
                        serial, deployment_id, "installed", agent_log=agent_log
                    )
                    logger.warning(
                        f"[SOFTWARE] Installer succeeded but not yet verified: {software_name}"
                    )
                else:
                    report_software_deployment_status(
                        serial, deployment_id, "failed", error_message=install_msg
                    )
                    logger.error(f"[SOFTWARE] FAILED: {install_msg}")
            
        except Exception as e:
            logger.error(f"[SOFTWARE] Error in sync loop: {e}")
        
        time.sleep(SOFTWARE_SYNC_INTERVAL)


# ============================================================================
# APP DEPLOYMENT SYNC (package-manager-based)
# ============================================================================

def get_pending_app_deployments(serial_number):
    """Poll Odoo for pending package-manager app deployments."""
    logger.info("[APP DEPLOY] Polling for pending deployments...")
    try:
        url = f"{APP_DEPLOY_BASE_URL}/poll"
        response = requests.get(
            url,
            params={'serial_number': serial_number},
            timeout=15,
            headers=ODOO_HEADERS
        )
        if response.status_code == 200:
            data = response.json()
            deployments = data.get('deployments', [])
            logger.info(f"[APP DEPLOY] Found {len(deployments)} pending deployment(s)")
            return deployments
        else:
            logger.warning(f"[APP DEPLOY] Poll failed: HTTP {response.status_code}")
            return []
    except Exception as e:
        logger.warning(f"[APP DEPLOY] Error polling: {e}")
        return []


def report_app_deployment_status(deployment_id, status, error_message=None):
    """Report app deployment status back to Odoo."""
    try:
        payload = {'deployment_id': deployment_id, 'status': status}
        if error_message:
            payload['error_message'] = error_message
        url = f"{APP_DEPLOY_BASE_URL}/deployment_status"
        success, _ = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"[APP DEPLOY] Status reported: {status} for deployment {deployment_id}")
        else:
            logger.warning(f"[APP DEPLOY] Failed to report status for deployment {deployment_id}")
    except Exception as e:
        logger.error(f"[APP DEPLOY] Error reporting status: {e}")


def run_install_command(command, timeout=300):
    """
    Execute the install/uninstall command via shell.
    Returns (success: bool, output: str).
    """
    logger.info(f"[APP DEPLOY] Running: {command}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == 'Windows' else 0
        )
        output = (result.stdout or '') + (result.stderr or '')
        success = result.returncode == 0
        logger.info(f"[APP DEPLOY] Command exit code: {result.returncode}")
        if not success:
            logger.warning(f"[APP DEPLOY] Command output: {output[:500]}")
        return success, output.strip()
    except subprocess.TimeoutExpired:
        msg = f"Command timed out after {timeout}s: {command}"
        logger.error(f"[APP DEPLOY] {msg}")
        return False, msg
    except Exception as e:
        msg = f"Command execution error: {e}"
        logger.error(f"[APP DEPLOY] {msg}")
        return False, msg


def run_url_installer(dep):
    """
    Download an installer from a URL and execute it natively.
    Handles exe/msi/deb/rpm/pkg/dmg/appimage/zip.
    Returns (success: bool, output: str).
    """
    import urllib.request
    import shutil

    url       = dep.get('installer_url', '')
    itype     = dep.get('installer_type', 'exe').lower()
    args      = dep.get('installer_args', '') or ''
    app_name  = dep.get('application_name', 'app')

    if not url:
        return False, 'No installer URL provided'

    # Derive filename from URL
    from urllib.parse import urlparse as _urlparse
    path = _urlparse(url).path
    fname = path.rstrip('/').split('/')[-1] or f'installer_{itype}'
    tmp_path = os.path.join(tempfile.gettempdir(), fname)

    logger.info(f"[URL INSTALL] Downloading {url} → {tmp_path}")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AssetAgent/1.5'})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, 'wb') as f:
            shutil.copyfileobj(resp, f)
        logger.info(f"[URL INSTALL] Download complete: {os.path.getsize(tmp_path)} bytes")
    except Exception as e:
        return False, f"Download failed: {e}"

    try:
        sys_platform = platform.system()

        if itype == 'exe':
            if sys_platform != 'Windows':
                return False, 'EXE installer requires Windows'
            # /S — standard NSIS silent flag (most common Windows installer framework)
            result = subprocess.run(
                [tmp_path, '/S'],
                capture_output=True, text=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

        elif itype == 'msi':
            if sys_platform != 'Windows':
                return False, 'MSI installer requires Windows'
            # /quiet /norestart — standard MSI silent installation flags
            result = subprocess.run(
                ['msiexec', '/i', tmp_path, '/quiet', '/norestart'],
                capture_output=True, text=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

        elif itype == 'deb':
            result = subprocess.run(
                ['dpkg', '-i', tmp_path],
                capture_output=True, text=True, timeout=300
            )

        elif itype == 'rpm':
            result = subprocess.run(
                ['rpm', '-ivh', tmp_path],
                capture_output=True, text=True, timeout=300
            )

        elif itype == 'pkg':
            result = subprocess.run(
                ['installer', '-pkg', tmp_path, '-target', '/'],
                capture_output=True, text=True, timeout=600
            )

        elif itype == 'dmg':
            # Attach, copy .app, detach
            attach = subprocess.run(
                ['hdiutil', 'attach', tmp_path, '-nobrowse', '-quiet'],
                capture_output=True, text=True, timeout=60
            )
            if attach.returncode != 0:
                return False, f"hdiutil attach failed: {attach.stderr}"
            mount = attach.stdout.strip().split('\n')[-1].split('\t')[-1].strip()
            logger.info(f"[URL INSTALL] DMG mounted at: {mount}")
            apps = [f for f in os.listdir(mount) if f.endswith('.app')]
            if apps:
                subprocess.run(['cp', '-R', os.path.join(mount, apps[0]), '/Applications/'],
                               timeout=120)
            subprocess.run(['hdiutil', 'detach', mount, '-quiet'], timeout=30)
            result = type('R', (), {'returncode': 0, 'stdout': f'Installed {apps}', 'stderr': ''})()

        elif itype == 'appimage':
            os.chmod(tmp_path, 0o755)
            result = type('R', (), {'returncode': 0, 'stdout': f'AppImage ready at {tmp_path}', 'stderr': ''})()

        elif itype == 'zip':
            import zipfile
            dest = tmp_path.replace('.zip', '').replace('.ZIP', '')
            os.makedirs(dest, exist_ok=True)
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                zf.extractall(dest)
            result = type('R', (), {'returncode': 0, 'stdout': f'Extracted to {dest}', 'stderr': ''})()

        else:
            return False, f"Unsupported installer type: {itype}"

        output = (getattr(result, 'stdout', '') or '') + (getattr(result, 'stderr', '') or '')
        success = result.returncode == 0
        if success:
            logger.info(f"[URL INSTALL] SUCCESS: {app_name}")
        else:
            logger.warning(f"[URL INSTALL] FAILED rc={result.returncode}: {output[:300]}")
        return success, output.strip()

    except subprocess.TimeoutExpired:
        return False, f"Installer timed out for {app_name}"
    except Exception as e:
        return False, f"Installer error: {e}"
    finally:
        try:
            if itype != 'appimage':  # keep AppImage in place
                os.remove(tmp_path)
        except Exception:
            pass


def app_deployment_sync_loop():
    """
    Background thread for package-manager app deployment polling.
    Every APP_DEPLOY_POLL_INTERVAL seconds:
      1. Poll /asset_management/api/agent/poll for pending deployments
      2. For each deployment:
         a. Report status → in_progress
         b. Run install_command via subprocess
         c. Report status → success or failed
    """
    logger.info(f"[APP DEPLOY] Sync loop started (interval: {APP_DEPLOY_POLL_INTERVAL}s)")
    time.sleep(15)  # Short initial delay before first poll

    serial = get_serial_number()

    while True:
        try:
            deployments = get_pending_app_deployments(serial)

            for dep in deployments:
                deployment_id      = dep.get('deployment_id')
                app_name           = dep.get('application_name', 'Unknown')
                install_command    = dep.get('install_command', '')
                package_manager    = dep.get('package_manager', '')
                application_source = dep.get('application_source', 'preset')
                action_type        = dep.get('action_type', 'install')
                installer_url      = dep.get('installer_url', '')

                logger.info(
                    f"[APP DEPLOY] Processing: {app_name} | "
                    f"source={application_source} action={action_type} pm={package_manager}"
                )

                # Step 1: Mark as in-progress
                report_app_deployment_status(deployment_id, 'in_progress')

                # Step 2: Execute — URL installer gets its own native handler
                if application_source == 'url' and action_type == 'install' and installer_url:
                    logger.info(f"[APP DEPLOY] Using native URL installer for {app_name}")
                    success, output = run_url_installer(dep)
                elif install_command:
                    # Package manager / custom / uninstall commands run via shell
                    # URL-based commands generated by the wizard also land here as fallback
                    timeout = 900 if application_source == 'url' else 300
                    success, output = run_install_command(install_command, timeout=timeout)
                else:
                    logger.warning(f"[APP DEPLOY] No command for deployment {deployment_id}")
                    report_app_deployment_status(deployment_id, 'failed', 'No command provided')
                    continue

                # Step 3: Report final result
                if success:
                    logger.info(f"[APP DEPLOY] SUCCESS: {app_name}")
                    report_app_deployment_status(deployment_id, 'success')
                else:
                    logger.error(f"[APP DEPLOY] FAILED: {app_name} — {output[:200]}")
                    report_app_deployment_status(deployment_id, 'failed', output[:500])

        except Exception as e:
            logger.error(f"[APP DEPLOY] Error in sync loop: {e}")

        time.sleep(APP_DEPLOY_POLL_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

def main():
    global SHOW_UI
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Asset Agent")
    parser.add_argument("--ui", action="store_true", help="Enable the graphical user interface")
    args, unknown = parser.parse_known_args()
    
    if args.ui:
        SHOW_UI = True

    logger.info("=" * 60)
    logger.info(f"Asset Agent v{AGENT_VERSION} Starting")
    logger.info("=" * 60)

    if SHOW_UI:
        try:
            root = tk.Tk()
            app = AssetAgentUI(root)
            threading.Thread(target=update_checker_thread, daemon=True).start()
            threading.Thread(target=windows_update_sync_loop, daemon=True).start()
            threading.Thread(target=file_access_sync_loop, daemon=True).start()
            threading.Thread(target=start_file_browser, daemon=True).start()
            threading.Thread(target=antivirus_sync_loop, daemon=True).start()
            threading.Thread(target=app_uninstall_sync_loop, daemon=True).start()
            threading.Thread(target=software_deployment_sync_loop, daemon=True).start()
            threading.Thread(target=app_deployment_sync_loop, daemon=True).start()
            logger.info("Windows Update sync started")
            logger.info("[FILE ACCESS] Policy sync started")
            logger.info(f"[AV] Antivirus deployment sync started (interval: {ANTIVIRUS_POLL_INTERVAL}s)")
            logger.info(f"[APP UNINSTALL] Sync started (interval: {APP_UNINSTALL_SYNC_INTERVAL}s)")
            logger.info(f"[SOFTWARE] Deployment sync started (interval: {SOFTWARE_SYNC_INTERVAL}s)")
            logger.info(f"[APP DEPLOY] App deployment sync started (interval: {APP_DEPLOY_POLL_INTERVAL}s)")
            root.mainloop()
        except Exception as e:
            logger.error(f"UI startup failed: {e}. Falling back to headless mode")
            SHOW_UI = False

    if not SHOW_UI:
        logger.info("Running in Headless Mode")
        logger.info("=" * 60)

        # Start auto-update checker
        threading.Thread(target=update_checker_thread, daemon=True).start()
        logger.info(f"Auto-update checker started (checks every {UPDATE_CHECK_INTERVAL}s)")

        # Start Windows Update sync
        threading.Thread(target=windows_update_sync_loop, daemon=True).start()
        logger.info(f"Windows Update sync started (interval: {WINDOWS_UPDATE_SYNC_INTERVAL}s)")

        # Start File Access Policy sync
        threading.Thread(target=file_access_sync_loop, daemon=True).start()
        logger.info(f"[FILE ACCESS] Policy sync started (interval: {FILE_ACCESS_SYNC_INTERVAL}s)")

        # Start Antivirus deployment sync
        threading.Thread(target=antivirus_sync_loop, daemon=True).start()
        logger.info(f"[AV] Antivirus deployment sync started (interval: {ANTIVIRUS_POLL_INTERVAL}s)")

        # Start Software Deployment sync
        threading.Thread(target=software_deployment_sync_loop, daemon=True).start()
        logger.info(f"[SOFTWARE] Deployment sync started (interval: {SOFTWARE_SYNC_INTERVAL}s)")

        # Start Application Uninstall sync
        threading.Thread(target=app_uninstall_sync_loop, daemon=True).start()
        logger.info(f"[APP UNINSTALL] Sync started (interval: {APP_UNINSTALL_SYNC_INTERVAL}s)")

        # Start App Deployment sync (package-manager-based)
        threading.Thread(target=app_deployment_sync_loop, daemon=True).start()
        logger.info(f"[APP DEPLOY] App deployment sync started (interval: {APP_DEPLOY_POLL_INTERVAL}s)")

        # Start File Browser API
        threading.Thread(target=start_file_browser, daemon=True).start()

        # Initial system detection
        logger.info("Performing initial system detection...")
        serial   = get_serial_number()
        hostname = socket.gethostname()
        model    = get_device_model()
        logger.info(f"Device: {model}")
        logger.info(f"Hostname: {hostname}")
        logger.info(f"Serial: {serial}")
        logger.info(f"Version: {AGENT_VERSION}")

        # Initial static sync
        logger.info("Performing initial static sync...")
        try:
            payload = collect_static_data()
            success, response = send_with_retry(ODOO_API_URL, payload, max_retries=3, timeout=30)
            if success:
                logger.info("OK Initial static sync completed successfully")
            else:
                logger.warning("Initial static sync failed, will retry in next interval")
        except Exception as e:
            logger.error(f"Initial static sync error: {e}")

        # Initial live sync
        logger.info("Performing initial live sync (heartbeat)...")
        try:
            payload = collect_live_data()
            success, response = send_with_retry(f"{ODOO_API_URL}/live", payload, max_retries=3, timeout=10)
            if success:
                logger.info("OK Initial heartbeat sent successfully")
            else:
                logger.warning("Initial heartbeat failed, will retry in next interval")
        except Exception as e:
            logger.error(f"Initial heartbeat error: {e}")

        logger.info(f"Starting continuous monitoring loop...")
        logger.info(f"Static sync interval:   {STATIC_SYNC_INTERVAL}s")
        logger.info(f"Live sync interval:     {LIVE_SYNC_INTERVAL}s")
        logger.info(f"WU sync interval:       {WINDOWS_UPDATE_SYNC_INTERVAL}s")

        next_static_sync = time.monotonic() + STATIC_SYNC_INTERVAL
        next_live_sync   = time.monotonic() + LIVE_SYNC_INTERVAL

        try:
            while True:
                current_time = time.monotonic()

                # Static sync
                if current_time >= next_static_sync:
                    if _static_sync_lock.acquire(blocking=False):
                        try:
                            logger.info("Performing static sync...")
                            try:
                                payload = collect_static_data()
                                success, response = send_with_retry(ODOO_API_URL, payload, max_retries=3, timeout=30)
                                if success:
                                    logger.info("OK Static sync completed")
                                else:
                                    logger.warning("Static sync failed, will retry")
                            except Exception as e:
                                logger.error(f"Static sync error: {e}", exc_info=True)
                            finally:
                                next_static_sync = time.monotonic() + STATIC_SYNC_INTERVAL
                        finally:
                            _static_sync_lock.release()
                    else:
                        logger.warning("Static sync already in progress, skipping")
                        next_static_sync = time.monotonic() + STATIC_SYNC_INTERVAL

                # Live sync
                if current_time >= next_live_sync:
                    if _live_sync_lock.acquire(blocking=False):
                        try:
                            try:
                                payload = collect_live_data()
                                success, response = send_with_retry(f"{ODOO_API_URL}/live", payload, max_retries=2, timeout=10)
                                if not success:
                                    logger.warning("Live sync failed, will retry")
                            except Exception as e:
                                logger.error(f"Live sync error: {e}", exc_info=True)
                            finally:
                                next_live_sync = time.monotonic() + LIVE_SYNC_INTERVAL
                        finally:
                            _live_sync_lock.release()
                    else:
                        logger.warning("Live sync already in progress, skipping")
                        next_live_sync = time.monotonic() + LIVE_SYNC_INTERVAL

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal, stopping agent...")
        except Exception as e:
            logger.critical(f"Fatal error in main loop: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    main()