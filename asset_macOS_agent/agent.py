import time
import requests
import socket
import platform
import psutil
import subprocess
import json
import os
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json as _json
import logging
import sys
import argparse
import plistlib
import re

# ============================================================================
# VERSION & UPDATE CONFIGURATION
# ============================================================================
AGENT_VERSION = "1.1.0"
UPDATE_CHECK_URL = "https://sneakily-interalar-yon.ngrok-free.dev/api/agent/version"
UPDATE_DOWNLOAD_URL = "https://sneakily-interalar-yon.ngrok-free.dev/downloads/AssetAgent_latest_macos"
UPDATE_CHECK_INTERVAL = 60  # seconds

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
log_dir = os.path.expanduser('~/Library/Logs/AssetAgent')
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'asset_agent.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# ODOO API CONFIGURATION
# ============================================================================
ODOO_API_URL = "https://sneakily-interalar-yon.ngrok-free.dev/api/laptop_monitor"
ODOO_DB_NAME = "odoo19"
ODOO_HEADERS = {
    "Content-Type": "application/json",
    "X-Odoo-Database": ODOO_DB_NAME,
}
STATIC_SYNC_INTERVAL = 60
LIVE_SYNC_INTERVAL = 30
SHOW_UI = False

# ============================================================================
# macOS SOFTWARE UPDATE CONFIGURATION
# ============================================================================
MACOS_UPDATE_BASE_URL = "https://sneakily-interalar-yon.ngrok-free.dev/api/asset/updates"
MACOS_UPDATE_SYNC_INTERVAL = 60

# ============================================================================
# FILE ACCESS POLICY CONFIGURATION
# ============================================================================
FILE_ACCESS_BASE_URL      = "https://sneakily-interalar-yon.ngrok-free.dev/api/asset/file_access"
FILE_ACCESS_SYNC_INTERVAL = 60

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
ANTIVIRUS_BASE_URL    = "https://sneakily-interalar-yon.ngrok-free.dev/api/antivirus"
ANTIVIRUS_POLL_INTERVAL = 30

# ============================================================================
# SOFTWARE DEPLOYMENT CONFIGURATION
# ============================================================================
SOFTWARE_BASE_URL      = "https://sneakily-interalar-yon.ngrok-free.dev/api/asset/software"
SOFTWARE_SYNC_INTERVAL = 30

# ============================================================================
# APP DEPLOYMENT CONFIGURATION (package-manager-based)
# ============================================================================
APP_DEPLOY_BASE_URL       = "https://sneakily-interalar-yon.ngrok-free.dev/asset_management/api/agent"
APP_DEPLOY_POLL_INTERVAL  = 30

# ============================================================================
# APP UNINSTALL CONFIGURATION
# ============================================================================
APP_UNINSTALL_BASE_URL      = "https://sneakily-interalar-yon.ngrok-free.dev/api/asset/apps"
APP_UNINSTALL_SYNC_INTERVAL = 45

# ============================================================================
# FILE BROWSER CONFIGURATION
# ============================================================================
FILE_BROWSER_PORT = 8000

# ============================================================================
# GLOBAL CACHES AND LOCKS
# ============================================================================
_cached_serial_number = None
_cached_device_model  = None
_cached_graphics_card = None
_cached_location      = None
_last_location_fetch  = 0
_cache_lock           = threading.Lock()
_static_sync_lock     = threading.Lock()
_live_sync_lock       = threading.Lock()
_macos_update_lock    = threading.Lock()
_file_access_policy   = {}
_file_access_lock     = threading.Lock()
_fa_observer          = None


# ============================================================================
# AUTO-UPDATE FUNCTIONS
# ============================================================================

def check_for_updates():
    """Check if a new version is available"""
    try:
        logger.info(f"Checking for updates (current version: {AGENT_VERSION})")
        response = requests.get(
            UPDATE_CHECK_URL, timeout=10,
            params={"current_version": AGENT_VERSION, "platform": "macos"},
            headers=ODOO_HEADERS
        )
        if response.status_code == 200:
            data = response.json()
            latest_version = data.get("latest_version", AGENT_VERSION)
            download_url   = data.get("download_url", UPDATE_DOWNLOAD_URL)

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
        temp_dir    = tempfile.gettempdir()
        update_file = os.path.join(temp_dir, "AssetAgent_update")
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
        os.chmod(update_file, 0o755)
        logger.info(f"OK Update downloaded successfully: {update_file}")
        return update_file
    except Exception as e:
        logger.error(f"Error downloading update: {e}")
        return None


def apply_update(update_file):
    """Apply the update by replacing current binary and restarting"""
    try:
        if not getattr(sys, 'frozen', False):
            logger.warning("Auto-update is only supported in frozen (binary) mode.")
            return False

        current_exe  = sys.executable
        backup_exe   = current_exe + ".backup"
        update_script = os.path.join(tempfile.gettempdir(), "update_agent.sh")

        with open(update_script, 'w') as f:
            f.write(f"""#!/bin/bash
sleep 2
cp "{current_exe}" "{backup_exe}"
cp "{update_file}" "{current_exe}"
chmod +x "{current_exe}"
"{current_exe}" &
rm -f "{update_file}"
rm -f "$0"
""")
        os.chmod(update_script, 0o755)
        logger.info("RESTART Launching update script and exiting current agent...")
        subprocess.Popen(['/bin/bash', update_script],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
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
# NETWORK MONITOR
# ============================================================================

class NetworkMonitor:
    def __init__(self):
        self.last_bytes_sent  = 0
        self.last_bytes_recv  = 0
        self.last_check_time  = time.time()
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
                counters     = psutil.net_io_counters()
                current_time = time.time()
                time_delta   = current_time - self.last_check_time
                if time_delta < 0.1:
                    return 0, 0
                bytes_sent   = counters.bytes_sent - self.last_bytes_sent
                bytes_recv   = counters.bytes_recv - self.last_bytes_recv
                upload_bps   = (bytes_sent * 8) / time_delta if time_delta > 0 else 0
                download_bps = (bytes_recv * 8) / time_delta if time_delta > 0 else 0
                upload_mbps   = round(upload_bps   / 1_000_000, 2)
                download_mbps = round(download_bps / 1_000_000, 2)
                self.last_bytes_sent = counters.bytes_sent
                self.last_bytes_recv = counters.bytes_recv
                self.last_check_time = current_time
                return upload_mbps, download_mbps
            except:
                return 0, 0


network_monitor = NetworkMonitor()


# ============================================================================
# NETWORK / HTTP
# ============================================================================

def send_with_retry(url, payload, max_retries=3, timeout=30):
    """Send HTTP POST request with exponential backoff retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=timeout, headers=ODOO_HEADERS)
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


def get_local_ip():
    """Get the real LAN IP (Wi-Fi or Ethernet), skip virtual interfaces."""
    try:
        for iface_name, addrs in psutil.net_if_addrs().items():
            skip_keywords = ['docker', 'veth', 'vmnet', 'virbr', 'lo', 'vbox', 'br-', 'virtual', 'utun', 'awdl', 'llw']
            if any(kw in iface_name.lower() for kw in skip_keywords):
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith('192.168.') or ip.startswith('10.'):
                        logger.info(f"Detected LAN IP: {ip} on interface: {iface_name}")
                        return ip
    except Exception as e:
        logger.warning(f"Error detecting LAN IP via psutil: {e}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.warning(f"Socket fallback failed: {e}")
        return '127.0.0.1'


# ============================================================================
# macOS SYSTEM INFORMATION FUNCTIONS
# ============================================================================

def run_command(cmd, shell=False):
    """Run a shell command and return output"""
    try:
        result = subprocess.run(
            cmd if isinstance(cmd, list) else cmd,
            shell=shell, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception as e:
        logger.warning(f"Command failed: {cmd} - {e}")
        return ""


def get_serial_number():
    """Get Mac serial number"""
    global _cached_serial_number
    with _cache_lock:
        if _cached_serial_number:
            return _cached_serial_number
        try:
            serial = run_command(['ioreg', '-l', '-w', '0'])
            match  = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', serial)
            if match:
                _cached_serial_number = match.group(1)
                return _cached_serial_number
            output = run_command(['system_profiler', 'SPHardwareDataType'])
            for line in output.split('\n'):
                if 'Serial Number' in line:
                    s = line.split(':')[-1].strip()
                    if s and s != '(system)':
                        _cached_serial_number = s
                        return _cached_serial_number
            _cached_serial_number = "UNKNOWN"
            return _cached_serial_number
        except Exception as e:
            logger.error(f"Error getting serial number: {e}")
            _cached_serial_number = "UNKNOWN"
            return _cached_serial_number


def get_device_model():
    """Get Mac model identifier"""
    global _cached_device_model
    with _cache_lock:
        if _cached_device_model:
            return _cached_device_model
        try:
            model = run_command(['sysctl', '-n', 'hw.model'])
            if model:
                _cached_device_model = model
                return _cached_device_model
            output = run_command(['system_profiler', 'SPHardwareDataType'])
            for line in output.split('\n'):
                if 'Model Identifier' in line:
                    m = line.split(':')[-1].strip()
                    if m:
                        _cached_device_model = m
                        return _cached_device_model
            _cached_device_model = "Unknown Mac"
            return _cached_device_model
        except Exception as e:
            logger.error(f"Error getting device model: {e}")
            _cached_device_model = "Unknown Mac"
            return _cached_device_model


def get_cpu_info():
    """Get CPU information"""
    try:
        output = run_command(['sysctl', '-n', 'machdep.cpu.brand_string'])
        if output:
            return output
        output = run_command(['system_profiler', 'SPHardwareDataType'])
        for line in output.split('\n'):
            if 'Processor Name' in line or 'Chip' in line:
                return line.split(':')[-1].strip()
        return platform.processor() or "Unknown CPU"
    except Exception as e:
        logger.error(f"Error getting CPU info: {e}")
        return "Unknown CPU"


def get_graphics_card():
    """Get GPU information"""
    global _cached_graphics_card
    with _cache_lock:
        if _cached_graphics_card:
            return _cached_graphics_card
        try:
            output = run_command(['system_profiler', 'SPDisplaysDataType'])
            for line in output.split('\n'):
                line = line.strip()
                if 'Chipset Model' in line:
                    gpu = line.split(':')[-1].strip()
                    if gpu:
                        _cached_graphics_card = gpu
                        return _cached_graphics_card
            output = run_command(['sysctl', '-n', 'machdep.cpu.brand_string'])
            if 'Apple' in output or 'M1' in output or 'M2' in output or 'M3' in output or 'M4' in output:
                _cached_graphics_card = "Apple Silicon GPU"
                return _cached_graphics_card
            _cached_graphics_card = "Unknown GPU"
            return _cached_graphics_card
        except Exception as e:
            logger.error(f"Error getting GPU info: {e}")
            _cached_graphics_card = "Unknown GPU"
            return _cached_graphics_card


def get_os_version():
    """Get macOS version"""
    try:
        version = platform.mac_ver()[0]
        if version:
            return f"macOS {version}"
        output = run_command(['sw_vers', '-productVersion'])
        if output:
            return f"macOS {output}"
        return "macOS Unknown"
    except Exception as e:
        logger.error(f"Error getting OS version: {e}")
        return "macOS Unknown"


def get_mac_address():
    """Get primary MAC address"""
    try:
        output = run_command(['ifconfig', 'en0'])
        match  = re.search(r'ether\s+([0-9a-f:]{17})', output, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        output = run_command(['ifconfig'])
        match  = re.search(r'ether\s+([0-9a-f:]{17})', output, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        return "00:00:00:00:00:00"
    except Exception as e:
        logger.error(f"Error getting MAC address: {e}")
        return "00:00:00:00:00:00"


def get_disk_type():
    """Detect if primary disk is SSD or HDD on macOS"""
    try:
        output = run_command(['system_profiler', 'SPStorageDataType'])
        if 'Solid State' in output or 'SSD' in output or 'Flash' in output:
            return 'SSD'
        elif 'Hard Disk' in output or 'HDD' in output or 'Rotational' in output:
            return 'HDD'
        # Apple Silicon Macs always have SSD
        model = run_command(['sysctl', '-n', 'machdep.cpu.brand_string'])
        if 'Apple' in model:
            return 'SSD'
        return 'SSD'
    except Exception as e:
        logger.warning(f"Error detecting disk type: {e}")
        return 'Unknown'


def get_storage_volumes():
    """Get all mounted storage volumes"""
    try:
        volumes = []
        partitions = psutil.disk_partitions(all=False)
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                volumes.append({
                    'device':     p.device,
                    'mountpoint': p.mountpoint,
                    'fstype':     p.fstype,
                    'total_gb':   round(usage.total / (1024 ** 3), 2),
                    'used_gb':    round(usage.used  / (1024 ** 3), 2),
                    'free_gb':    round(usage.free  / (1024 ** 3), 2),
                    'percent':    usage.percent
                })
            except:
                continue
        return json.dumps(volumes)
    except Exception as e:
        logger.warning(f"Error getting storage volumes: {e}")
        return "[]"


def get_battery_info():
    """Get battery information"""
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return {'percentage': 0, 'capacity': 0, 'health': 'N/A',
                    'status': 'No Battery', 'time_remaining': 0}

        output       = run_command(['system_profiler', 'SPPowerDataType', '-xml'])
        capacity_mah = 0
        health       = 'Unknown'
        cycle_count  = 0

        try:
            plist = plistlib.loads(output.encode())
            if plist and len(plist) > 0:
                items = plist[0].get('_items', [])
                for item in items:
                    battery_info = item.get('sppower_battery_health_info', {})
                    cycle_count  = battery_info.get('sppower_battery_cycle_count', 0)
                    max_capacity = battery_info.get('sppower_battery_max_capacity', 0)
                    if max_capacity:
                        capacity_mah = int(max_capacity)
                    health = battery_info.get('sppower_battery_health', 'Unknown')
        except Exception as e:
            logger.warning(f"Could not parse battery plist: {e}")

        if battery.power_plugged:
            status = "Charging"
        else:
            status = "Discharging"
        time_remaining = 0
        if not battery.power_plugged:
            if battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
                time_remaining = battery.secsleft // 60

        return {
            'percentage':     round(battery.percent),
            'capacity':       capacity_mah,
            'health':         health,
            'status':         status,
            'time_remaining': time_remaining,
            'cycle_count':    cycle_count
        }
    except Exception as e:
        logger.error(f"Error getting battery info: {e}")
        return {'percentage': 0, 'capacity': 0, 'health': 'Unknown',
                'status': 'Unknown', 'time_remaining': 0}


def get_location_data():
    """Get approximate location via IP geolocation (cached 1 hour)"""
    global _cached_location, _last_location_fetch
    with _cache_lock:
        current_time = time.time()
        if _cached_location and (current_time - _last_location_fetch) < 3600:
            return _cached_location
        try:
            response = requests.get('https://ipapi.co/json/', timeout=5)
            if response.status_code == 200:
                data = response.json()
                location = {
                    'location_city':    data.get('city',         'Unknown'),
                    'location_region':  data.get('region',       'Unknown'),
                    'location_country': data.get('country_name', 'Unknown'),
                    'latitude':         data.get('latitude',     0),
                    'longitude':        data.get('longitude',    0)
                }
                _cached_location      = location
                _last_location_fetch  = current_time
                return location
        except Exception as e:
            logger.warning(f"Error getting location: {e}")
        if _cached_location:
            return _cached_location
        return {'location_city': 'Unknown', 'location_region': 'Unknown',
                'location_country': 'Unknown', 'latitude': 0, 'longitude': 0}


def get_network_info():
    """Get network connectivity information"""
    try:
        output    = run_command(['ifconfig', 'en0'])
        ip_match  = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', output)
        ip_address = ip_match.group(1) if ip_match else "0.0.0.0"
        ssid = run_command(['networksetup', '-getairportnetwork', 'en0'])
        if 'Current Wi-Fi Network:' in ssid:
            ssid = ssid.split(':')[-1].strip()
        else:
            ssid = "Not Connected"
        try:
            requests.get('https://www.google.com', timeout=3)
            connected = True
        except:
            connected = False
        return {'ip_address': ip_address, 'ssid': ssid, 'connected': connected,
                'mac_address': get_mac_address()}
    except Exception as e:
        logger.error(f"Error getting network info: {e}")
        return {'ip_address': '0.0.0.0', 'ssid': 'Unknown',
                'connected': False, 'mac_address': get_mac_address()}


def get_installed_apps():
    """Get list of installed macOS applications via system_profiler"""
    try:
        result = subprocess.run(
            ['system_profiler', 'SPApplicationsDataType', '-json'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "[]"
        data = json.loads(result.stdout)
        raw_apps = data.get('SPApplicationsDataType', [])
        apps = []
        for app in raw_apps:
            name = app.get('_name', '').strip()
            if not name:
                continue
            apps.append({
                'name':           name[:255],
                'publisher':      app.get('signed_by', [''])[0][:255] if app.get('signed_by') else '',
                'version':        str(app.get('version', ''))[:100],
                'installed_date': '',
                'size':           0
            })
        logger.info(f"OK Total unique applications: {len(apps)}")
        return json.dumps(apps)
    except Exception as e:
        logger.warning(f"Error getting installed apps: {e}")
        return "[]"


# ============================================================================
# macOS SOFTWARE UPDATE FUNCTIONS
# ============================================================================

def scan_macos_updates():
    """
    Scan available macOS software updates using softwareupdate CLI.
    Returns list of update dicts: kb_number (label), title, severity, size, version.
    """
    logger.info("Scanning for available macOS software updates...")
    try:
        result = subprocess.run(
            ['softwareupdate', '--list', '--all'],
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr
        if not output.strip():
            return []

        updates   = []
        lines     = output.split('\n')
        i         = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('*') or line.startswith('-'):
                # Extract label and title
                label = ''
                title = ''
                # Label is on line with *, title is the rest or next line
                if ',' in line:
                    parts = line.lstrip('*- ').split(',')
                    label = parts[0].strip()
                    title = parts[0].strip()
                else:
                    label = line.lstrip('*- ').strip()
                    title = label

                # Check next line for size / recommended info
                size_str = ''
                severity = 'optional'
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if 'Recommended' in next_line or 'RECOMMENDED' in next_line:
                        severity = 'important'
                    size_match = re.search(r'([\d.]+\s*[KMG]B)', next_line, re.IGNORECASE)
                    if size_match:
                        size_str = size_match.group(1)
                    if 'Security' in next_line or 'security' in next_line:
                        severity = 'security'

                if label:
                    updates.append({
                        'kb_number':   label[:100],
                        'title':       title[:255],
                        'description': '',
                        'severity':    severity,
                        'size':        size_str,
                        'version':     '',
                    })
            i += 1

        logger.info(f"OK Found {len(updates)} pending macOS software updates")
        return updates
    except Exception as e:
        logger.error(f"Error scanning macOS updates: {e}")
        return []


def scan_installed_macos_updates():
    """
    Scan INSTALLED macOS software updates.
    Returns list of update label strings that are currently installed.
    """
    logger.info("Scanning for installed macOS software updates...")
    try:
        result = subprocess.run(
            ['softwareupdate', '--history'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return []
        installed = []
        for line in (result.stdout + result.stderr).split('\n'):
            line = line.strip()
            if line and not line.startswith('-') and not line.startswith('Software') and not line.startswith('Display'):
                parts = line.split()
                if parts:
                    installed.append(parts[0])
        logger.info(f"OK Found {len(installed)} installed macOS updates")
        return installed
    except Exception as e:
        logger.error(f"Error scanning installed macOS updates: {e}")
        return []


def report_macos_updates_to_odoo(serial_number, updates, installed_updates=None):
    """Send scanned macOS updates to Odoo API."""
    if not updates and not installed_updates:
        logger.info("No macOS updates to report to Odoo")
        return
    try:
        payload = {
            "serial_number":  serial_number,
            "updates":        updates,
            "installed_kbs":  installed_updates or []
        }
        url = f"{MACOS_UPDATE_BASE_URL}/report"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=30)
        if success:
            logger.info(f"OK Reported {len(updates)} available + {len(installed_updates or [])} installed updates to Odoo")
        else:
            logger.warning("Failed to report macOS updates to Odoo")
    except Exception as e:
        logger.error(f"Error reporting macOS updates to Odoo: {e}")


def get_macos_update_instructions(serial_number):
    """Poll Odoo for macOS update instructions."""
    default = {"is_locked": False, "blocklist": [], "push_list": [], "uninstall_list": [], "cancel_list": []}
    try:
        url = f"{MACOS_UPDATE_BASE_URL}/instructions"
        response = requests.get(url, params={"serial_number": serial_number},
                                timeout=15, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data = response.json()
            logger.info(
                f"Received macOS update instructions — locked: {data.get('is_locked')}, "
                f"push: {len(data.get('push_list', []))}"
            )
            return data
        else:
            logger.warning(f"Failed to get macOS update instructions: HTTP {response.status_code}")
            return default
    except Exception as e:
        logger.warning(f"Error getting macOS update instructions: {e}")
        return default


def install_macos_update(label):
    """Silently install a specific macOS update by label."""
    logger.info(f"Installing macOS update: {label}")
    try:
        result = subprocess.run(
            ['softwareupdate', '--install', label, '--no-scan'],
            capture_output=True, text=True, timeout=600
        )
        output = (result.stdout + result.stderr).lower()
        if result.returncode == 0 or 'successfully installed' in output or 'no updates are available' in output:
            logger.info(f"macOS update installed: {label}")
            return True
        logger.warning(f"macOS update install failed for {label}: {result.stderr[:200]}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing macOS update {label}")
        return False
    except Exception as e:
        logger.error(f"Error installing macOS update {label}: {e}")
        return False


def enforce_macos_update_lock(is_locked):
    """
    Lock/unlock automatic software update on macOS using softwareupdate preferences.
    """
    if is_locked:
        logger.info("[LOCK] Disabling macOS automatic software update")
        try:
            subprocess.run(
                ['defaults', 'write', '/Library/Preferences/com.apple.SoftwareUpdate',
                 'AutomaticCheckEnabled', '-bool', 'false'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['defaults', 'write', '/Library/Preferences/com.apple.SoftwareUpdate',
                 'AutomaticDownload', '-bool', 'false'],
                capture_output=True, timeout=10
            )
            logger.info("[LOCK] OK macOS automatic update disabled")
        except Exception as e:
            logger.error(f"[LOCK] ERROR applying lock: {e}")
    else:
        logger.info("[LOCK] Restoring macOS automatic software update")
        try:
            subprocess.run(
                ['defaults', 'write', '/Library/Preferences/com.apple.SoftwareUpdate',
                 'AutomaticCheckEnabled', '-bool', 'true'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['defaults', 'write', '/Library/Preferences/com.apple.SoftwareUpdate',
                 'AutomaticDownload', '-bool', 'true'],
                capture_output=True, timeout=10
            )
            logger.info("[LOCK] OK macOS automatic update restored")
        except Exception as e:
            logger.error(f"[LOCK] ERROR removing lock: {e}")


def report_macos_update_result(serial_number, label, status):
    """Report install/cancel result back to Odoo."""
    try:
        payload = {"serial_number": serial_number, "kb_number": label, "status": status}
        url = f"{MACOS_UPDATE_BASE_URL}/result"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"OK Reported {label} result ({status}) to Odoo")
        else:
            logger.warning(f"Failed to report result for {label}")
    except Exception as e:
        logger.error(f"Error reporting macOS update result: {e}")


def execute_macos_update_instructions(serial_number, instructions):
    """Execute admin instructions from Odoo for macOS updates."""
    is_locked      = instructions.get('is_locked', False)
    push_list      = instructions.get('push_list', [])
    uninstall_list = instructions.get('uninstall_list', [])

    enforce_macos_update_lock(is_locked)

    if is_locked:
        logger.info("Device is locked — no update actions will be performed by agent")
        return

    if push_list:
        logger.info(f"[INSTALL] Admin requested installation of: {push_list}")
        for label in push_list:
            try:
                success = install_macos_update(label)
                status  = 'installed' if success else 'failed'
                report_macos_update_result(serial_number, label, status)
                logger.info(f"[INSTALL] {'SUCCESS' if success else 'FAILED'} {label} -> {status}")
            except Exception as e:
                logger.error(f"[INSTALL] ERROR installing {label}: {e}")
                report_macos_update_result(serial_number, label, 'failed')

    if uninstall_list:
        logger.info(f"[UNINSTALL] macOS does not natively support update uninstall — skipping: {uninstall_list}")
        for label in uninstall_list:
            report_macos_update_result(serial_number, label, 'failed')


def macos_update_sync_loop():
    """Background thread for macOS Software Update sync."""
    logger.info(f"macOS Software Update sync loop started (interval: {MACOS_UPDATE_SYNC_INTERVAL}s)")
    time.sleep(30)
    while True:
        try:
            if not _macos_update_lock.acquire(blocking=False):
                logger.warning("macOS Update sync already in progress, skipping")
                time.sleep(MACOS_UPDATE_SYNC_INTERVAL)
                continue
            try:
                logger.info("=" * 40)
                logger.info("Starting macOS Software Update sync cycle...")
                serial          = get_serial_number()
                updates         = scan_macos_updates()
                installed_upds  = scan_installed_macos_updates()
                if updates or installed_upds:
                    report_macos_updates_to_odoo(serial, updates, installed_upds)
                instructions = get_macos_update_instructions(serial)
                execute_macos_update_instructions(serial, instructions)
                logger.info("macOS Software Update sync cycle complete")
                logger.info("=" * 40)
            finally:
                _macos_update_lock.release()
        except Exception as e:
            logger.error(f"Error in macOS Update sync loop: {e}")
        time.sleep(MACOS_UPDATE_SYNC_INTERVAL)


# ============================================================================
# APPLICATION UNINSTALL FUNCTIONS (macOS)
# ============================================================================

def find_application_on_macos(app_name, publisher=None, version=None):
    """
    Search /Applications and ~/Applications for the app.
    Returns dict with app_path, display_name or None.
    """
    logger.info(f"[APP UNINSTALL] Searching for: {app_name}")
    search_dirs = ['/Applications', os.path.expanduser('~/Applications')]
    app_name_lower = app_name.lower().strip()

    found_apps = []
    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue
        try:
            for item in os.listdir(search_dir):
                if not item.endswith('.app'):
                    continue
                name_no_ext = item[:-4].lower()
                if app_name_lower in name_no_ext or name_no_ext in app_name_lower:
                    app_path = os.path.join(search_dir, item)
                    found_apps.append({
                        'display_name': item[:-4],
                        'app_path':     app_path,
                    })
        except Exception as e:
            logger.warning(f"[APP UNINSTALL] Error searching {search_dir}: {e}")

    # Also check homebrew cask
    try:
        brew_result = subprocess.run(
            ['brew', 'list', '--cask'],
            capture_output=True, text=True, timeout=15
        )
        if brew_result.returncode == 0:
            for cask in brew_result.stdout.split('\n'):
                if app_name_lower in cask.lower().strip():
                    found_apps.append({
                        'display_name': cask.strip(),
                        'app_path':     None,
                        'brew_cask':    cask.strip()
                    })
    except Exception:
        pass

    if not found_apps:
        logger.warning(f"[APP UNINSTALL] No matching application found for: {app_name}")
        return None

    # Prefer exact match
    for app in found_apps:
        if app['display_name'].lower() == app_name_lower:
            return app
    return found_apps[0]


def uninstall_application_macos(app_name, publisher=None, version=None):
    """
    Uninstall an application on macOS.
    Tries: brew cask uninstall → move to Trash → rm -rf.
    Returns (success: bool, error_message: str or None)
    """
    logger.info(f"[APP UNINSTALL] Starting uninstall for: {app_name}")
    try:
        app_info = find_application_on_macos(app_name, publisher, version)
        if not app_info:
            return False, f"Application '{app_name}' not found"

        # Try brew cask first
        if app_info.get('brew_cask'):
            cask = app_info['brew_cask']
            logger.info(f"[APP UNINSTALL] Using brew to uninstall cask: {cask}")
            result = subprocess.run(
                ['brew', 'uninstall', '--cask', '--force', cask],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                logger.info(f"[APP UNINSTALL] SUCCESS via brew: {cask}")
                return True, None
            logger.warning(f"[APP UNINSTALL] brew uninstall failed: {result.stderr[:200]}")

        # Try rm -rf on .app bundle
        if app_info.get('app_path') and os.path.exists(app_info['app_path']):
            app_path = app_info['app_path']
            logger.info(f"[APP UNINSTALL] Removing app bundle: {app_path}")
            result = subprocess.run(
                ['rm', '-rf', app_path],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                logger.info(f"[APP UNINSTALL] SUCCESS: {app_path} removed")
                return True, None
            else:
                # Try with sudo via osascript (prompts admin password)
                error_msg = f"rm -rf failed (exit {result.returncode}): {result.stderr[:200]}"
                logger.error(f"[APP UNINSTALL] FAILED: {error_msg}")
                return False, error_msg

        return False, "Could not build uninstall command"
    except subprocess.TimeoutExpired:
        return False, "Uninstall timed out"
    except Exception as e:
        return False, f"Uninstall error: {str(e)}"


def get_app_uninstall_instructions(serial_number):
    """Poll Odoo for application uninstall instructions."""
    default = {"success": True, "uninstall_list": []}
    try:
        url = f"{APP_UNINSTALL_BASE_URL}/uninstall_command"
        response = requests.get(url, params={"serial_number": serial_number},
                                timeout=15, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data = response.json()
            logger.info(f"[APP UNINSTALL] Received {len(data.get('uninstall_list', []))} uninstall commands from Odoo")
            return data
        else:
            logger.warning(f"[APP UNINSTALL] Failed to get instructions: HTTP {response.status_code}")
            return default
    except Exception as e:
        logger.warning(f"[APP UNINSTALL] Error getting instructions: {e}")
        return default


def report_app_uninstall_result(serial_number, app_name, app_publisher, app_version, status, error_message=None):
    """Report application uninstall result back to Odoo."""
    try:
        payload = {
            "serial_number": serial_number, "app_name": app_name,
            "app_publisher": app_publisher, "app_version": app_version, "status": status,
        }
        if error_message:
            payload["error_message"] = error_message
        url = f"{APP_UNINSTALL_BASE_URL}/uninstall_result"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"[APP UNINSTALL] Reported result to Odoo: {app_name} -> {status}")
        else:
            logger.warning(f"[APP UNINSTALL] Failed to report result for {app_name}")
    except Exception as e:
        logger.error(f"[APP UNINSTALL] Error reporting result: {e}")


def execute_app_uninstall_instructions(serial_number, instructions):
    """Execute application uninstall instructions from Odoo."""
    uninstall_list = instructions.get('uninstall_list', [])
    if not uninstall_list:
        return
    logger.info(f"[APP UNINSTALL] Processing {len(uninstall_list)} uninstall commands")
    for app in uninstall_list:
        app_name      = app.get('name', '')
        app_publisher = app.get('publisher', '')
        app_version   = app.get('version', '')
        if not app_name:
            logger.warning("[APP UNINSTALL] Skipping app with no name")
            continue
        try:
            success, error_message = uninstall_application_macos(app_name, app_publisher, app_version)
            status = 'uninstalled' if success else 'failed'
            report_app_uninstall_result(serial_number, app_name, app_publisher, app_version,
                                        status, error_message)
            logger.info(f"[APP UNINSTALL] {'SUCCESS' if success else 'FAILED'}: {app_name} -> {status}")
        except Exception as e:
            logger.error(f"[APP UNINSTALL] ERROR processing {app_name}: {e}")
            report_app_uninstall_result(serial_number, app_name, app_publisher, app_version, 'failed', str(e))


def app_uninstall_sync_loop():
    """Background thread for application uninstall sync."""
    logger.info(f"[APP UNINSTALL] Sync loop started (interval: {APP_UNINSTALL_SYNC_INTERVAL}s)")
    time.sleep(30)
    serial = get_serial_number()
    while True:
        try:
            instructions = get_app_uninstall_instructions(serial)
            if instructions.get('uninstall_list'):
                execute_app_uninstall_instructions(serial, instructions)
            else:
                logger.debug("[APP UNINSTALL] No pending uninstall commands")
        except Exception as e:
            logger.error(f"[APP UNINSTALL] Error in sync loop: {e}")
        time.sleep(APP_UNINSTALL_SYNC_INTERVAL)


# ============================================================================
# FILE ACCESS POLICY — BLOCK + NOTIFY + REPORT (macOS)
# ============================================================================

def fa_show_notification(blocked_path):
    """Show macOS notification when access is blocked."""
    try:
        title   = "Access Blocked by Admin Policy"
        message = f"Access to '{os.path.basename(blocked_path)}' has been blocked by your administrator."
        subprocess.Popen(
            ['osascript', '-e',
             f'display notification "{message}" with title "{title}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        logger.info(f"[FILE ACCESS] Notification shown for: {blocked_path}")
    except Exception as e:
        logger.warning(f"[FILE ACCESS] Notification error: {e}")


def fa_is_safe_to_block(path):
    """Safety check — never block entire drives or system/home folders."""
    path = os.path.normpath(path)
    home = os.path.expanduser("~")
    protected = [
        home,
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Pictures"),
        os.path.join(home, "Music"),
        os.path.join(home, "Movies"),
        '/Applications', '/System', '/Library', '/usr', '/private',
        '/', '/Volumes'
    ]
    for p in protected:
        if path == os.path.normpath(p):
            logger.warning(f"[FILE ACCESS] SAFETY: Refused to block protected path: {path}")
            return False
    if len(path) <= 1:
        return False
    return True


def fa_block_path(path):
    """Block access to EXACT file or specific folder — macOS chmod 000."""
    try:
        result = subprocess.run(
            ['chmod', '-R', '000', path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info(f"[FILE ACCESS] Blocked exact path: {path}")
            return True
        else:
            logger.warning(f"[FILE ACCESS] chmod block failed: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"[FILE ACCESS] Block error: {e}")
        return False


def fa_unblock_path(path):
    """Remove block — restore access via chmod 644/755."""
    try:
        if os.path.isdir(path):
            # Restore directory permissions
            result = subprocess.run(
                ['chmod', '-R', 'u+rwX,go+rX', path],
                capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ['chmod', '644', path],
                capture_output=True, text=True, timeout=30
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
        ok, _ = send_with_retry(f"{FILE_ACCESS_BASE_URL}/violation", payload,
                                max_retries=2, timeout=10)
        if ok:
            logger.info(f"[FILE ACCESS] Violation reported: {blocked_path}")
    except Exception as e:
        logger.warning(f"[FILE ACCESS] Report violation error: {e}")


def fa_get_policy(serial):
    """Poll Odoo for file access policy."""
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
    """Compare current policy with new policy. Block new, unblock removed."""
    global _file_access_policy
    new_policy = {p: True for p in blocked_paths}
    with _file_access_lock:
        for path in list(_file_access_policy.keys()):
            if path not in new_policy:
                fa_unblock_path(path)
                del _file_access_policy[path]
                logger.info(f"[FILE ACCESS] Policy removed — unblocked: {path}")
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
    """Monitor Desktop, Documents, Downloads using watchdog."""
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


def fa_scan_folders(serial):
    """
    Recursively scan Desktop, Documents, Downloads and send to Odoo.
    """
    try:
        home = os.path.expanduser("~")
        root_folders = {
            "Desktop":   os.path.join(home, "Desktop"),
            "Documents": os.path.join(home, "Documents"),
            "Downloads": os.path.join(home, "Downloads"),
        }
        records     = []
        MAX_RECORDS = 5000
        EXCLUSIONS  = {
            "node_modules", ".git", ".next", "dist", "build",
            ".cache", "__pycache__", ".venv", "venv", "bin", "obj",
            ".idea", ".vscode", ".DS_Store"
        }
        for folder_name, folder_path in root_folders.items():
            if not os.path.exists(folder_path):
                continue
            if len(records) >= MAX_RECORDS:
                break
            try:
                st = os.stat(folder_path)
                records.append({
                    "type": "folder", "name": folder_name, "path": folder_path,
                    "parent_path": os.path.dirname(folder_path), "parent_folder": folder_name,
                    "size_kb": 0,
                    "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
            except:
                pass
            for dirpath, dirnames, filenames in os.walk(folder_path):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith('.') and d not in EXCLUSIONS]
                for dname in dirnames:
                    if len(records) >= MAX_RECORDS:
                        break
                    full_p = os.path.join(dirpath, dname)
                    try:
                        st = os.stat(full_p)
                        records.append({
                            "type": "folder", "name": dname, "path": full_p,
                            "parent_path": os.path.dirname(full_p), "parent_folder": folder_name,
                            "size_kb": 0,
                            "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        })
                    except:
                        continue
                for fname in filenames:
                    if len(records) >= MAX_RECORDS:
                        break
                    full_p = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full_p)
                        records.append({
                            "type": "file", "name": fname, "path": full_p,
                            "parent_path": os.path.dirname(full_p), "parent_folder": folder_name,
                            "size_kb": round(st.st_size / 1024, 2),
                            "last_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        })
                    except:
                        continue
                if len(records) >= MAX_RECORDS:
                    break
        if records:
            payload = {
                "serial_number": serial, "records": records,
                "scanned_at":    datetime.now(timezone.utc).isoformat(),
            }
            ok, _ = send_with_retry(f"{FILE_ACCESS_BASE_URL}/scan",
                                    payload, max_retries=2, timeout=60)
            if ok:
                logger.info(f"[FILE ACCESS] Scanned {len(records)} items → sent to Odoo")
            else:
                logger.warning("[FILE ACCESS] Scan send failed (timeout?)")
        else:
            logger.warning("[FILE ACCESS] No records found — check folder paths")
    except Exception as e:
        logger.error(f"[FILE ACCESS] Scan error: {e}")


def file_access_sync_loop():
    """Every 60s: scan files → send to Odoo; poll policy → enforce blocks/unblocks."""
    global _fa_observer
    logger.info(f"[FILE ACCESS] Sync loop started (interval: {FILE_ACCESS_SYNC_INTERVAL}s)")
    serial = get_serial_number()
    _fa_observer = fa_monitor_access(serial)
    while True:
        try:
            fa_scan_folders(serial)
            blocked_paths = fa_get_policy(serial)
            fa_enforce_policy(serial, blocked_paths)
        except Exception as e:
            logger.error(f"[FILE ACCESS] Sync loop error: {e}")
        time.sleep(FILE_ACCESS_SYNC_INTERVAL)


# ============================================================================
# ANTIVIRUS DETECTION FUNCTIONS (macOS)
# ============================================================================

def get_av_version_macos(bundle_name):
    """Get antivirus version from macOS app bundle Info.plist."""
    try:
        apps_dir = "/Applications"
        for item in os.listdir(apps_dir):
            if bundle_name.lower() in item.lower() and item.endswith('.app'):
                app_path   = os.path.join(apps_dir, item)
                info_plist = os.path.join(app_path, "Contents", "Info.plist")
                if os.path.exists(info_plist):
                    plutil_result = subprocess.run(
                        ['plutil', '-convert', 'json', '-o', '-', info_plist],
                        capture_output=True, text=True, timeout=5
                    )
                    if plutil_result.returncode == 0:
                        try:
                            plist_data = json.loads(plutil_result.stdout)
                            version = (plist_data.get("CFBundleShortVersionString")
                                       or plist_data.get("CFBundleVersion"))
                            if version:
                                return str(version)
                        except Exception:
                            pass
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


def get_antivirus_info_macos():
    """
    Get comprehensive antivirus information for macOS.
    Detection methods:
    1. XProtect (built-in macOS protection)
    2. Third-party AV processes
    3. Installed AV app bundles in /Applications
    4. Gatekeeper status
    """
    result = {
        "antivirus_installed": False,
        "antivirus_product":   "None",
        "antivirus_version":   "unknown",
        "antivirus_running":   False
    }
    try:
        av_detection_map = {
            "sophos":      {"process": "sophos",   "bundle": "Sophos",       "name": "Sophos Home"},
            "bitdefender": {"process": "bdagent",  "bundle": "Bitdefender",  "name": "Bitdefender Virus Scanner"},
            "intego":      {"process": "intego",   "bundle": "Intego",       "name": "Intego VirusBarrier"},
            "mcafee":      {"process": "mcafee",   "bundle": "McAfee",       "name": "McAfee LiveSafe"},
            "norton":      {"process": "norton",   "bundle": "Norton",       "name": "Norton Security"},
            "clamav":      {"process": "clamd",    "bundle": "ClamAV",       "name": "ClamAV"},
            "avast":       {"process": "avast",    "bundle": "Avast",        "name": "Avast Security"},
            "avg":         {"process": "avg",      "bundle": "AVG",          "name": "AVG AntiVirus"},
            "trend_micro": {"process": "trend",    "bundle": "Trend Micro",  "name": "Trend Micro Antivirus"},
            "eset":        {"process": "esets",    "bundle": "ESET",         "name": "ESET Cyber Security"},
            "kaspersky":   {"process": "kaspersky","bundle": "Kaspersky",    "name": "Kaspersky Internet Security"},
            "malwarebytes":{"process": "malwarebytes","bundle": "Malwarebytes","name": "Malwarebytes"},
        }

        # Method 1: Check XProtect
        try:
            xprotect_result = subprocess.run(
                ["mdls", "-name", "kMDItemVersion",
                 "/System/Library/CoreServices/XProtect.bundle"],
                capture_output=True, text=True, timeout=5
            )
            if xprotect_result.returncode == 0 and "kMDItemVersion" in xprotect_result.stdout:
                version_line = [l for l in xprotect_result.stdout.split('\n') if 'kMDItemVersion' in l]
                if version_line:
                    xprotect_version = version_line[0].split('=', 1)[1].strip().strip('"')
                    result["antivirus_installed"] = True
                    result["antivirus_product"]   = "XProtect (macOS Built-in)"
                    result["antivirus_version"]   = xprotect_version
                    result["antivirus_running"]   = True
                    logger.info(f"[AV-macOS] XProtect detected: version {xprotect_version}")
        except Exception:
            pass

        # Method 2: Check for third-party AV processes
        try:
            ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
            if ps_result.returncode == 0:
                processes = ps_result.stdout.lower()
                for av_key, av_info in av_detection_map.items():
                    if av_info["process"] in processes:
                        result["antivirus_installed"] = True
                        result["antivirus_product"]   = av_info["name"]
                        result["antivirus_running"]   = True
                        version = get_av_version_macos(av_info["bundle"])
                        if version:
                            result["antivirus_version"] = version
                        logger.info(f"[AV-macOS] Process detection: {av_info['process']} -> {av_info['name']}")
                        return result
        except Exception:
            pass

        # Method 3: Check installed AV apps in /Applications
        try:
            apps_result = subprocess.run(['ls', '/Applications/'],
                                         capture_output=True, text=True, timeout=5)
            if apps_result.returncode == 0:
                installed_apps = apps_result.stdout.lower()
                for av_key, av_info in av_detection_map.items():
                    if av_info["bundle"].lower() in installed_apps:
                        result["antivirus_installed"] = True
                        result["antivirus_product"]   = av_info["name"]
                        result["antivirus_running"]   = False
                        version = get_av_version_macos(av_info["bundle"])
                        if version:
                            result["antivirus_version"] = version
                        logger.info(f"[AV-macOS] App detection: {av_info['bundle']} -> {av_info['name']}")
                        return result
        except Exception:
            pass

        if not result["antivirus_installed"]:
            logger.info("[AV-macOS] No third-party antivirus detected (XProtect may be active)")

        return result
    except Exception as e:
        logger.warning(f"[AV-macOS] Error getting antivirus info: {e}")
        result["antivirus_product"] = f"Error: {e}"
        return result


def get_antivirus_info():
    """Get comprehensive antivirus information (macOS)."""
    try:
        return get_antivirus_info_macos()
    except Exception as e:
        logger.warning(f"[AV] Error getting antivirus info: {e}")
        return {
            "antivirus_installed": False,
            "antivirus_product":   f"Error: {e}",
            "antivirus_version":   "unknown",
            "antivirus_running":   False
        }


def check_antivirus_installed():
    """Check if antivirus is installed and running."""
    try:
        av_info = get_antivirus_info()
        if av_info["antivirus_installed"]:
            return av_info["antivirus_running"], av_info["antivirus_product"]
        return False, "No antivirus detected"
    except Exception as e:
        logger.warning(f"[AV] Error checking antivirus: {e}")
        return False, f"Check failed: {e}"


def download_installer(installer_url, platform_name="macos"):
    """Download antivirus installer to temp folder."""
    try:
        temp_dir  = tempfile.gettempdir()
        url_path  = installer_url.split('?')[0]
        filename  = url_path.split('/')[-1] or f"av_installer_{platform_name}"
        local_path = os.path.join(temp_dir, filename)
        logger.info(f"[AV] Downloading installer from: {installer_url}")
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


def run_silent_installer_macos(installer_path, ext):
    """Run macOS antivirus installer (.dmg, .pkg)"""
    try:
        if ext == '.pkg':
            logger.info(f"[AV-macOS] Installing .pkg package: {installer_path}")
            cmd = ["installer", "-pkg", installer_path, "-target", "/", "-allowUntrusted"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        elif ext == '.dmg':
            logger.info(f"[AV-macOS] Mounting DMG: {installer_path}")
            mount_result = subprocess.run(
                ["hdiutil", "attach", installer_path, "-mountpoint", "/Volumes/Installer"],
                capture_output=True, text=True, timeout=60
            )
            if mount_result.returncode != 0:
                return False, f"Failed to mount DMG: {mount_result.stderr}"
            pkg_found = False
            install_result = (False, "No .pkg found in DMG")
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
            subprocess.run(["hdiutil", "detach", "/Volumes/Installer"],
                           capture_output=True, timeout=30)
            return install_result
        else:
            os.chmod(installer_path, 0o755)
            cmd    = [installer_path, "--silent"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        logger.info(f"[AV-macOS] Installer exit code: {result.returncode}")
        if result.returncode in (0, 3010):
            return True, f"macOS installation completed (exit code: {result.returncode})"
        else:
            return False, f"macOS installation failed (exit code: {result.returncode})"
    except Exception as e:
        logger.error(f"[AV-macOS] Installation error: {e}")
        return False, str(e)


def run_silent_installer(installer_path, platform_name="macos"):
    """Run the antivirus installer silently."""
    try:
        if not os.path.exists(installer_path):
            return False, f"Installer not found: {installer_path}"
        ext = os.path.splitext(installer_path)[1].lower()
        logger.info(f"[AV] Running installer: {installer_path} (type: {ext})")
        return run_silent_installer_macos(installer_path, ext)
    except Exception as e:
        logger.error(f"[AV] Installer error: {e}")
        return False, str(e)


def report_antivirus_status(serial_number, deployment_id, status,
                             av_version=None, error_message=None, agent_log=None):
    """Report antivirus deployment status back to Odoo."""
    try:
        payload = {"serial_number": serial_number, "deployment_id": deployment_id, "status": status}
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
    """Background thread for antivirus deployment polling."""
    logger.info(f"[AV] Antivirus sync loop started (interval: {ANTIVIRUS_POLL_INTERVAL}s)")
    time.sleep(15)
    serial = get_serial_number()
    while True:
        try:
            logger.info("[AV] Polling for antivirus deployment command...")
            url      = f"{ANTIVIRUS_BASE_URL}/command"
            response = requests.get(url, params={"serial_number": serial},
                                    timeout=15, headers=ODOO_HEADERS)
            if response.status_code != 200:
                logger.warning(f"[AV] Poll failed: HTTP {response.status_code}")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue
            data    = response.json()
            command = data.get("command", "none")
            if command != "install":
                logger.info(f"[AV] No deployment pending: {data.get('message', 'none')}")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue
            deployment_id = data.get("deployment_id")
            installer_url = data.get("installer_url")
            platform_name = data.get("platform", "macos")
            product       = data.get("product", "antivirus")
            logger.info(f"[AV] Deploy command received! deployment_id={deployment_id}, "
                        f"product={product}, platform={platform_name}")
            if not installer_url:
                report_antivirus_status(serial, deployment_id, "failed",
                                        error_message="No installer URL provided by server")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue
            report_antivirus_status(serial, deployment_id, "downloading")
            dl_success, installer_path = download_installer(installer_url, platform_name)
            if not dl_success:
                report_antivirus_status(serial, deployment_id, "failed",
                                        error_message=f"Download failed: {installer_path}")
                time.sleep(ANTIVIRUS_POLL_INTERVAL)
                continue
            report_antivirus_status(serial, deployment_id, "installing")
            install_success, install_msg = run_silent_installer(installer_path, platform_name)
            try:
                os.remove(installer_path)
            except Exception:
                pass
            logger.info("[AV] Waiting 10 seconds before checking AV status...")
            time.sleep(10)
            av_detected, av_product = check_antivirus_installed()
            if install_success and av_detected:
                report_antivirus_status(serial, deployment_id, "installed",
                                        av_version=av_product,
                                        agent_log=f"Installed: {install_msg}. Detected: {av_product}")
                logger.info(f"[AV] SUCCESS: {product} installed and detected")
            elif install_success and not av_detected:
                report_antivirus_status(serial, deployment_id, "installed",
                                        av_version=product,
                                        agent_log=f"Installer succeeded. {install_msg}")
                logger.warning("[AV] Installer succeeded but AV not yet detected")
            else:
                report_antivirus_status(serial, deployment_id, "failed",
                                        error_message=install_msg)
                logger.error(f"[AV] FAILED: {install_msg}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[AV] Connection error polling command: {e}")
        except requests.exceptions.Timeout:
            logger.warning("[AV] Timeout polling antivirus command")
        except Exception as e:
            logger.error(f"[AV] Error in antivirus sync loop: {e}")
        time.sleep(ANTIVIRUS_POLL_INTERVAL)


# ============================================================================
# SOFTWARE DEPLOYMENT FUNCTIONS (macOS)
# ============================================================================

def get_pending_software_deployments(serial_number):
    """Poll Odoo for pending software deployments."""
    logger.info("[SOFTWARE] Polling for pending deployments...")
    try:
        url = f"{SOFTWARE_BASE_URL}/poll"
        response = requests.get(url, json={'serial_number': serial_number},
                                timeout=15, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data        = response.json()
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
    """Download software installer to temp folder."""
    try:
        temp_dir   = tempfile.gettempdir()
        local_path = os.path.join(temp_dir, filename)
        logger.info(f"[SOFTWARE] Downloading: {filename}")
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


def install_software_macos(installer_path, silent_flags):
    """Run software installer silently on macOS (.pkg, .dmg, .app)."""
    try:
        if not os.path.exists(installer_path):
            return False, f"Installer not found: {installer_path}"
        ext = os.path.splitext(installer_path)[1].lower()
        logger.info(f"[SOFTWARE] Installing: {installer_path} (type: {ext})")
        if ext == '.pkg':
            cmd = ["installer", "-pkg", installer_path, "-target", "/"]
        elif ext == '.dmg':
            success, msg = run_silent_installer_macos(installer_path, '.dmg')
            return success, msg
        else:
            os.chmod(installer_path, 0o755)
            flags = silent_flags.split() if silent_flags else ['--silent']
            cmd   = [installer_path] + flags
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        logger.info(f"[SOFTWARE] Exit code: {result.returncode}")
        if result.returncode in (0, 3010, 1641):
            return True, f"Installation completed (exit code: {result.returncode})"
        else:
            return False, f"Installation failed (exit code: {result.returncode})"
    except subprocess.TimeoutExpired:
        return False, "Installation timed out after 600 seconds"
    except Exception as e:
        logger.error(f"[SOFTWARE] Installation error: {e}")
        return False, str(e)


def verify_software_installed_macos(software_name):
    """Check if software appears in /Applications or system_profiler."""
    try:
        installed_apps_json = get_installed_apps()
        installed_apps      = json.loads(installed_apps_json)
        for app in installed_apps:
            if software_name.lower() in app.get('name', '').lower():
                logger.info(f"[SOFTWARE] Verified: {software_name} found")
                return True
        logger.warning(f"[SOFTWARE] Not found: {software_name}")
        return False
    except Exception as e:
        logger.error(f"[SOFTWARE] Verification error: {e}")
        return False


def report_software_deployment_status(serial, deployment_id, status,
                                       error_message=None, agent_log=None):
    """Report deployment status to Odoo."""
    try:
        payload = {"serial_number": serial, "deployment_id": deployment_id, "status": status}
        if error_message:
            payload["error_message"] = error_message
        if agent_log:
            payload["agent_log"] = agent_log
        url = f"{SOFTWARE_BASE_URL}/report"
        success, response = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"[SOFTWARE] Status reported: {status}")
        else:
            logger.warning("[SOFTWARE] Failed to report status")
    except Exception as e:
        logger.error(f"[SOFTWARE] Error reporting status: {e}")


def software_deployment_sync_loop():
    """Background thread for software deployment polling."""
    logger.info(f"[SOFTWARE] Deployment sync started (interval: {SOFTWARE_SYNC_INTERVAL}s)")
    time.sleep(30)
    serial = get_serial_number()
    while True:
        try:
            deployments = get_pending_software_deployments(serial)
            for dep in deployments:
                deployment_id      = dep['deployment_id']
                software_name      = dep['software_name']
                software_version   = dep['software_version']
                installer_url      = dep['installer_url']
                installer_filename = dep['installer_filename']
                silent_flags       = dep['silent_flags']
                logger.info(f"[SOFTWARE] Processing: {software_name} {software_version}")
                report_software_deployment_status(serial, deployment_id, "downloading")
                success, installer_path = download_software_installer(installer_url, installer_filename)
                if not success:
                    report_software_deployment_status(serial, deployment_id, "failed",
                                                      error_message=f"Download failed: {installer_path}")
                    continue
                report_software_deployment_status(serial, deployment_id, "installing")
                success, install_msg = install_software_macos(installer_path, silent_flags)
                try:
                    os.remove(installer_path)
                except Exception:
                    pass
                time.sleep(10)
                is_installed = verify_software_installed_macos(software_name)
                if success and is_installed:
                    report_software_deployment_status(
                        serial, deployment_id, "installed",
                        agent_log=f"Downloaded {installer_filename}, installed successfully, verified"
                    )
                    logger.info(f"[SOFTWARE] SUCCESS: {software_name} installed")
                elif success and not is_installed:
                    report_software_deployment_status(
                        serial, deployment_id, "installed",
                        agent_log=f"Installer succeeded but not yet detected. {install_msg}"
                    )
                    logger.warning(f"[SOFTWARE] Installer succeeded but not yet verified: {software_name}")
                else:
                    report_software_deployment_status(
                        serial, deployment_id, "failed", error_message=install_msg
                    )
                    logger.error(f"[SOFTWARE] FAILED: {install_msg}")
        except Exception as e:
            logger.error(f"[SOFTWARE] Error in sync loop: {e}")
        time.sleep(SOFTWARE_SYNC_INTERVAL)


# ============================================================================
# APP DEPLOYMENT SYNC (package-manager-based: brew, mas, etc.)
# ============================================================================

def get_pending_app_deployments(serial_number):
    """Poll Odoo for pending package-manager app deployments."""
    logger.info("[APP DEPLOY] Polling for pending deployments...")
    try:
        url = f"{APP_DEPLOY_BASE_URL}/poll"
        response = requests.get(url, params={'serial_number': serial_number},
                                timeout=15, headers=ODOO_HEADERS)
        if response.status_code == 200:
            data        = response.json()
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
        url     = f"{APP_DEPLOY_BASE_URL}/deployment_status"
        success, _ = send_with_retry(url, payload, max_retries=3, timeout=15)
        if success:
            logger.info(f"[APP DEPLOY] Status reported: {status} for deployment {deployment_id}")
        else:
            logger.warning(f"[APP DEPLOY] Failed to report status for deployment {deployment_id}")
    except Exception as e:
        logger.error(f"[APP DEPLOY] Error reporting status: {e}")


def run_install_command(command, timeout=300):
    """Execute the install/uninstall command via shell."""
    logger.info(f"[APP DEPLOY] Running: {command}")
    try:
        result  = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        output  = (result.stdout or '') + (result.stderr or '')
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


def run_url_installer_macos(dep):
    """
    Download an installer from a URL and execute it natively on macOS.
    Handles pkg/dmg/appimage/zip.
    Returns (success: bool, output: str).
    """
    import urllib.request
    import shutil

    url       = dep.get('installer_url', '')
    itype     = dep.get('installer_type', 'pkg').lower()
    app_name  = dep.get('application_name', 'app')

    if not url:
        return False, 'No installer URL provided'

    from urllib.parse import urlparse as _urlparse
    path     = _urlparse(url).path
    fname    = path.rstrip('/').split('/')[-1] or f'installer_{itype}'
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
        if itype == 'pkg':
            result = subprocess.run(
                ['installer', '-pkg', tmp_path, '-target', '/'],
                capture_output=True, text=True, timeout=600
            )
        elif itype == 'dmg':
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
            return False, f"Unsupported installer type for macOS: {itype}"

        output  = (getattr(result, 'stdout', '') or '') + (getattr(result, 'stderr', '') or '')
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
            if itype != 'appimage':
                os.remove(tmp_path)
        except Exception:
            pass


def app_deployment_sync_loop():
    """Background thread for package-manager app deployment polling."""
    logger.info(f"[APP DEPLOY] Sync loop started (interval: {APP_DEPLOY_POLL_INTERVAL}s)")
    time.sleep(15)
    serial = get_serial_number()
    while True:
        try:
            deployments = get_pending_app_deployments(serial)
            for dep in deployments:
                deployment_id      = dep.get('deployment_id')
                app_name           = dep.get('application_name', 'Unknown')
                install_command    = dep.get('install_command', '')
                application_source = dep.get('application_source', 'preset')
                action_type        = dep.get('action_type', 'install')
                installer_url      = dep.get('installer_url', '')
                logger.info(
                    f"[APP DEPLOY] Processing: {app_name} | "
                    f"source={application_source} action={action_type}"
                )
                report_app_deployment_status(deployment_id, 'in_progress')
                if application_source == 'url' and action_type == 'install' and installer_url:
                    logger.info(f"[APP DEPLOY] Using native URL installer for {app_name}")
                    success, output = run_url_installer_macos(dep)
                elif install_command:
                    timeout = 900 if application_source == 'url' else 300
                    success, output = run_install_command(install_command, timeout=timeout)
                else:
                    logger.warning(f"[APP DEPLOY] No command for deployment {deployment_id}")
                    report_app_deployment_status(deployment_id, 'failed', 'No command provided')
                    continue
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
# LIVE FILE BROWSER HTTP API SERVER (macOS)
# ============================================================================

def list_directory(path):
    """List ONE level only. Restricted to Desktop, Documents, Downloads."""
    home = os.path.expanduser("~")
    safe_roots = [
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads")
    ]
    if not path or path.lower() == "desktop":
        path = safe_roots[0]
    elif path.lower() == "documents":
        path = safe_roots[1]
    elif path.lower() == "downloads":
        path = safe_roots[2]

    abs_path = os.path.abspath(path)
    is_safe  = any(abs_path.startswith(root) for root in safe_roots)
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
                        "name":          entry.name,
                        "full_path":     entry.path,
                        "type":          "folder" if entry.is_dir() else "file",
                        "size_kb":       round(stat.st_size / 1024, 2) if entry.is_file() else 0,
                        "last_modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                    })
                except Exception:
                    continue
        return {"path": abs_path, "files": items, "status": 200}
    except PermissionError:
        return {"error": "Permission denied", "status": 403}
    except Exception as e:
        return {"error": str(e), "status": 500}


class FileBrowserAPI(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress HTTP logging noise

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/browse':
            qs = parse_qs(parsed.query)
            if 'path' in qs:
                target_path = qs['path'][0]
                result      = list_directory(target_path)
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
# DATA COLLECTION
# ============================================================================

def collect_static_data():
    """Collect static system information"""
    try:
        battery  = get_battery_info()
        av_info  = get_antivirus_info()

        payload = {
            "serial_number":     get_serial_number(),
            "hostname":          socket.gethostname(),
            "device_name":       get_device_model(),
            "processor":         get_cpu_info(),
            "os_type":           platform.architecture()[0],
            "os_name":           get_os_version(),
            "ram_size":          round(psutil.virtual_memory().total / (1024 ** 3), 2),
            "rom_size":          round(psutil.disk_usage('/').total / (1024 ** 3), 2),
            "disk_type":         get_disk_type(),
            "graphics_card_raw": get_graphics_card(),
            "battery_capacity":  battery['capacity'],
            "battery_percentage": battery['percentage'],
            "battery_health":    battery['health'],
            "storage_volumes":   get_storage_volumes(),
            "installed_apps":    get_installed_apps(),
            "agent_version":     AGENT_VERSION,
            "local_ip":          get_local_ip(),
            "mac_address":       get_mac_address(),
            "file_browser_port": FILE_BROWSER_PORT,
            "platform":          "macOS",
            # Antivirus information
            "antivirus_installed": av_info["antivirus_installed"],
            "antivirus_product":   av_info["antivirus_product"],
            "antivirus_version":   av_info["antivirus_version"],
            "antivirus_running":   av_info["antivirus_running"],
        }
        location_data = get_location_data()
        payload.update(location_data)
        return payload

    except Exception as e:
        logger.error(f"Error collecting static data: {e}", exc_info=True)
        payload = {
            "serial_number": get_serial_number(), "hostname": socket.gethostname(),
            "device_name": "Unknown Mac", "processor": "Unknown", "os_type": "64bit",
            "os_name": "macOS", "ram_size": 0, "rom_size": 0, "disk_type": "SSD",
            "graphics_card_raw": "Unknown", "battery_capacity": 0, "battery_percentage": 0,
            "battery_health": "Unknown", "storage_volumes": "[]", "installed_apps": "[]",
            "agent_version": AGENT_VERSION, "local_ip": get_local_ip(),
            "mac_address": get_mac_address(), "file_browser_port": FILE_BROWSER_PORT,
            "platform": "macOS",
            "antivirus_installed": False, "antivirus_product": "Error",
            "antivirus_version": "unknown", "antivirus_running": False
        }
        location_data = get_location_data()
        payload.update(location_data)
        return payload


def collect_live_data():
    """Collect live system metrics"""
    serial_number      = get_serial_number()
    hostname           = socket.gethostname()
    current_heartbeat  = datetime.now(timezone.utc).isoformat()
    try:
        cpu_usage             = round(psutil.cpu_percent(interval=0.5), 2)
        ram                   = psutil.virtual_memory()
        ram_usage             = round(ram.percent, 2)
        disk                  = psutil.disk_usage('/')
        disk_usage            = round(disk.percent, 2)
        upload_mbps, download_mbps = network_monitor.get_network_usage()
        battery               = psutil.sensors_battery()
        battery_percentage    = round(battery.percent, 2) if battery else 0
        network               = get_network_info()

        return {
            "serial_number":         serial_number,
            "hostname":              hostname,
            "cpu_usage_percent":     cpu_usage,
            "ram_usage_percent":     ram_usage,
            "disk_usage_percent":    disk_usage,
            "network_upload_mbps":   upload_mbps,
            "network_download_mbps": download_mbps,
            "battery_percentage":    battery_percentage,
            "heartbeat":             current_heartbeat,
            "agent_version":         AGENT_VERSION,
            "local_ip":              get_local_ip(),
            "file_browser_port":     FILE_BROWSER_PORT,
            "network_connected":     network['connected'],
            "wifi_ssid":             network['ssid'],
        }
    except Exception as e:
        logger.warning(f"Error collecting live data: {e}")
        return {
            "serial_number": serial_number, "hostname": hostname,
            "cpu_usage_percent": 0, "ram_usage_percent": 0, "disk_usage_percent": 0,
            "network_upload_mbps": 0, "network_download_mbps": 0,
            "battery_percentage": 0, "heartbeat": current_heartbeat,
            "agent_version": AGENT_VERSION, "local_ip": get_local_ip(),
            "file_browser_port": FILE_BROWSER_PORT,
            "network_connected": False, "wifi_ssid": "Unknown",
        }


# ============================================================================
# UI CLASS (Optional)
# ============================================================================

class AssetAgentUI:
    """GUI for the asset monitoring agent (macOS)"""

    def __init__(self, root):
        self.root = root
        self.root.title(f"Asset Agent v{AGENT_VERSION} - macOS")
        self.root.geometry("600x720")
        self.root.resizable(False, False)
        self.stop_event        = threading.Event()
        self.is_syncing_static = False
        self.is_syncing_live   = False
        self.labels            = {}
        self._create_ui()
        self.static_thread = threading.Thread(target=self.static_sync_loop, daemon=True)
        self.live_thread   = threading.Thread(target=self.live_sync_loop, daemon=True)
        self.static_thread.start()
        self.live_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(1000, self.perform_static_sync)
        self.root.after(2000, self.perform_live_sync)

    def _create_ui(self):
        # Title bar
        title_frame = tk.Frame(self.root, bg="#2c3e50", height=60)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)
        tk.Label(title_frame, text=f"Asset Monitoring Agent v{AGENT_VERSION}",
                 font=("Arial", 16, "bold"), bg="#2c3e50", fg="white").pack(pady=15)

        content = tk.Frame(self.root, padx=20, pady=10)
        content.pack(fill=tk.BOTH, expand=True)

        # Device info
        device_frame = tk.LabelFrame(content, text="Device Information",
                                     font=("Arial", 10, "bold"), padx=10, pady=10)
        device_frame.pack(fill=tk.X, pady=(0, 10))
        device_info = [
            ("Hostname:",      socket.gethostname()),
            ("Serial Number:", get_serial_number()),
            ("Model:",         get_device_model()),
            ("CPU:",           get_cpu_info()),
            ("GPU:",           get_graphics_card()),
            ("RAM:",           f"{round(psutil.virtual_memory().total / (1024**3), 2)} GB"),
            ("Storage:",       f"{round(psutil.disk_usage('/').total / (1024**3), 2)} GB"),
            ("OS:",            get_os_version()),
        ]
        for i, (label_text, value) in enumerate(device_info):
            tk.Label(device_frame, text=label_text, font=("Arial", 9, "bold"), anchor="w")\
              .grid(row=i, column=0, sticky="w", pady=2)
            key = label_text.replace(":", "").lower().replace(" ", "_")
            lbl = tk.Label(device_frame, text=value, font=("Arial", 9), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(10, 0), pady=2)
            self.labels[key] = lbl

        # Live metrics
        metrics_frame = tk.LabelFrame(content, text="Live Metrics",
                                      font=("Arial", 10, "bold"), padx=10, pady=10)
        metrics_frame.pack(fill=tk.X, pady=(0, 10))
        live_metrics = [
            ("CPU Usage:",    "cpu_usage"),
            ("RAM Usage:",    "ram_usage"),
            ("Disk Usage:",   "disk_usage"),
            ("Battery:",      "battery"),
            ("Network ↑:",    "net_up"),
            ("Network ↓:",    "net_down"),
            ("Network:",      "network"),
        ]
        for i, (label_text, key) in enumerate(live_metrics):
            tk.Label(metrics_frame, text=label_text, font=("Arial", 9, "bold"), anchor="w")\
              .grid(row=i, column=0, sticky="w", pady=2)
            lbl = tk.Label(metrics_frame, text="--", font=("Arial", 9), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(10, 0), pady=2)
            self.labels[key] = lbl

        # Status
        status_frame = tk.LabelFrame(content, text="Status",
                                     font=("Arial", 10, "bold"), padx=10, pady=10)
        status_frame.pack(fill=tk.X, pady=(0, 10))
        self.status_var         = tk.StringVar(value="Ready")
        self.last_static_sync_var = tk.StringVar(value="Static: Never")
        self.last_live_sync_var   = tk.StringVar(value="Live: Never")
        tk.Label(status_frame, textvariable=self.status_var, font=("Arial", 9)).pack(anchor="w")
        tk.Label(status_frame, textvariable=self.last_static_sync_var, font=("Arial", 8)).pack(anchor="w")
        tk.Label(status_frame, textvariable=self.last_live_sync_var, font=("Arial", 8)).pack(anchor="w")

        # Buttons
        btn_frame = tk.Frame(content)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        tk.Button(btn_frame, text="Force Static Sync", command=self.trigger_static_sync,
                  bg="#3498db", fg="white", font=("Arial", 9, "bold"), cursor="hand2")\
          .pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(btn_frame, text="Force Live Sync", command=self.trigger_live_sync,
                  bg="#2ecc71", fg="white", font=("Arial", 9, "bold"), cursor="hand2")\
          .pack(side=tk.LEFT)

    def on_closing(self):
        self.stop_event.set()
        self.root.destroy()

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
                if 'battery_percentage' in payload:
                    battery_text = (f"{payload['battery_percentage']}% "
                                    f"({payload.get('battery_capacity', 0)} mAh)")
                    self.root.after(0, lambda: self.labels["battery"].config(text=battery_text))
                success, response = send_with_retry(ODOO_API_URL, payload, max_retries=3, timeout=30)
                if success:
                    self.root.after(0, lambda: self.status_var.set("Static sync successful"))
                    self.root.after(0, lambda: self.last_static_sync_var.set(
                        f"Static: {datetime.now().strftime('%H:%M:%S')}"))
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
                self.root.after(0, lambda: self.labels["cpu_usage"].config(
                    text=f"{payload['cpu_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["ram_usage"].config(
                    text=f"{payload['ram_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["disk_usage"].config(
                    text=f"{payload['disk_usage_percent']}%"))
                self.root.after(0, lambda: self.labels["net_up"].config(
                    text=f"{payload['network_upload_mbps']} Mbps"))
                self.root.after(0, lambda: self.labels["net_down"].config(
                    text=f"{payload['network_download_mbps']} Mbps"))
                network_status = (f"{'Connected' if payload['network_connected'] else 'Disconnected'}"
                                  f" - {payload['wifi_ssid']}")
                self.root.after(0, lambda: self.labels["network"].config(text=network_status))
                success, response = send_with_retry(f"{ODOO_API_URL}/live", payload, max_retries=2, timeout=10)
                if success:
                    self.root.after(0, lambda: self.last_live_sync_var.set(
                        f"Live: {datetime.now().strftime('%H:%M:%S')}"))
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
# MAIN FUNCTION
# ============================================================================

def main():
    global SHOW_UI

    parser = argparse.ArgumentParser(description="Asset Agent (macOS)")
    parser.add_argument("--ui", action="store_true", help="Enable the graphical user interface")
    args, unknown = parser.parse_known_args()
    if args.ui:
        SHOW_UI = True

    logger.info("=" * 60)
    logger.info(f"Asset Agent v{AGENT_VERSION} Starting (macOS)")
    logger.info("=" * 60)

    if SHOW_UI:
        try:
            root = tk.Tk()
            app  = AssetAgentUI(root)
            threading.Thread(target=update_checker_thread,       daemon=True).start()
            threading.Thread(target=macos_update_sync_loop,      daemon=True).start()
            threading.Thread(target=file_access_sync_loop,       daemon=True).start()
            threading.Thread(target=start_file_browser,          daemon=True).start()
            threading.Thread(target=antivirus_sync_loop,         daemon=True).start()
            threading.Thread(target=app_uninstall_sync_loop,     daemon=True).start()
            threading.Thread(target=software_deployment_sync_loop, daemon=True).start()
            threading.Thread(target=app_deployment_sync_loop,    daemon=True).start()
            logger.info("macOS Software Update sync started")
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

        # Start macOS Software Update sync
        threading.Thread(target=macos_update_sync_loop, daemon=True).start()
        logger.info(f"macOS Software Update sync started (interval: {MACOS_UPDATE_SYNC_INTERVAL}s)")

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

        # Start App Deployment sync
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

        # Initial live sync (heartbeat)
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

        logger.info("Starting continuous monitoring loop...")
        logger.info(f"Static sync interval:  {STATIC_SYNC_INTERVAL}s")
        logger.info(f"Live sync interval:    {LIVE_SYNC_INTERVAL}s")
        logger.info(f"macOS Update interval: {MACOS_UPDATE_SYNC_INTERVAL}s")

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
                                success, response = send_with_retry(ODOO_API_URL, payload,
                                                                     max_retries=3, timeout=30)
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
                                success, response = send_with_retry(f"{ODOO_API_URL}/live",
                                                                     payload, max_retries=2, timeout=10)
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