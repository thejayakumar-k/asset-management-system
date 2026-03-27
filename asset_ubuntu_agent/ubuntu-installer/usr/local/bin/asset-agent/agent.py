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
import logging
import sys
import re
from datetime import datetime, timezone

# ============================================================================
# VERSION & UPDATE CONFIGURATION
# ============================================================================
AGENT_VERSION = "1.1.0"
UPDATE_CHECK_URL = "https://sneakily-interalar-yon.ngrok-free.dev/api/agent/version"
UPDATE_DOWNLOAD_URL = "https://sneakily-interalar-yon.ngrok-free.dev/downloads/AssetAgent_ubuntu_latest.tar.gz"
UPDATE_CHECK_INTERVAL = 60  # Check every 1 minute (60 seconds)

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
log_dir = '/var/log/asset-agent'
try:
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'asset_agent.log')
except PermissionError:
    # Fallback to user directory if no root access
    log_dir = os.path.expanduser('~/.asset-agent')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'asset_agent.log')

# Fix Unicode encoding for Windows console BEFORE configuring logging
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# ODOO API CONFIGURATION
# ============================================================================
ODOO_API_URL = "https://sneakily-interalar-yon.ngrok-free.dev/api/laptop_monitor"
STATIC_SYNC_INTERVAL = 60
LIVE_SYNC_INTERVAL = 30

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

# ============================================================================
# AUTO-UPDATE FUNCTIONS
# ============================================================================

def check_for_updates():
    """Check if a new version is available"""
    try:
        logger.info(f"Checking for updates (current version: {AGENT_VERSION})")
        
        response = requests.get(
            UPDATE_CHECK_URL,
            timeout=10,
            params={"current_version": AGENT_VERSION, "platform": "ubuntu"}
        )
        
        if response.status_code == 200:
            data = response.json()
            latest_version = data.get("latest_version", AGENT_VERSION)
            download_url = data.get("download_url", UPDATE_DOWNLOAD_URL)
            
            # Simple version comparison (assumes format: X.Y.Z)
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
        update_file = os.path.join(temp_dir, "AssetAgent_update.tar.gz")
        
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
                    
                    # Log progress every 1MB
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
        current_exe = os.path.abspath(__file__)
        updater_script = os.path.join(tempfile.gettempdir(), "agent_updater.sh")
        
        # Create shell script that will replace the agent after this process exits
        bash_content = f'''#!/bin/bash
echo "[AssetAgent Updater] Waiting for agent to exit..."
sleep 3

echo "[AssetAgent Updater] Backing up old version..."
if [ -f "{current_exe}.bak" ]; then
    rm "{current_exe}.bak"
fi
mv "{current_exe}" "{current_exe}.bak"

echo "[AssetAgent Updater] Extracting new version..."
tar -xzf "{update_file}" -C "$(dirname {current_exe})"

if [ -f "{current_exe}" ]; then
    echo "[AssetAgent Updater] Setting permissions..."
    chmod +x "{current_exe}"
    
    echo "[AssetAgent Updater] Starting updated agent..."
    nohup python3 "{current_exe}" > /dev/null 2>&1 &
    echo "[AssetAgent Updater] Update complete!"
else
    echo "[AssetAgent Updater] ERROR: Update failed, restoring backup..."
    mv "{current_exe}.bak" "{current_exe}"
    nohup python3 "{current_exe}" > /dev/null 2>&1 &
fi

sleep 2
rm -f "{update_file}"
rm -f "$0"
'''
        
        with open(updater_script, 'w') as f:
            f.write(bash_content)
        
        os.chmod(updater_script, 0o755)
        
        logger.info("RESTART Launching updater and exiting current agent...")
        
        # Launch updater in background
        subprocess.Popen(
            [updater_script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Give updater time to start, then exit
        time.sleep(1)
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Error applying update: {e}")
        return False


def update_checker_thread():
    """Background thread that periodically checks for updates"""
    logger.info(f"Update checker started (interval: {UPDATE_CHECK_INTERVAL}s)")
    
    # Wait a bit before first check (let agent start properly)
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
                    break  # Exit loop (agent will exit and restart)
                else:
                    logger.error("ERROR Update download failed, will retry next interval")
                    
            # Wait before next check
            time.sleep(UPDATE_CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in update checker: {e}")
            time.sleep(UPDATE_CHECK_INTERVAL)


# ============================================================================
# NETWORK MONITORING
# ============================================================================

def send_with_retry(url, payload, max_retries=3, timeout=30):
    """Send HTTP POST request with exponential backoff retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code == 200:
                logger.info(f"Successfully sent data to {url}")
                return True, response
            else:
                logger.warning(f"Request to {url} failed with status {response.status_code}")
                if response.status_code >= 500:
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + (time.time() % 1)
                        logger.info(f"Retrying in {wait_time:.2f}s...")
                        time.sleep(wait_time)
                        continue
                return False, response
        except requests.exceptions.Timeout:
            logger.warning(f"Request to {url} timed out (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + (time.time() % 1)
                logger.info(f"Retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error to {url}: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + (time.time() % 1)
                logger.info(f"Retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
        except Exception as e:
            logger.error(f"Unexpected error sending to {url}: {e}")
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + (time.time() % 1)
                time.sleep(wait_time)
    
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

# ============================================================================
# LOCATION DETECTION (GeoClue for Ubuntu)
# ============================================================================

def get_location_data():
    """Get location using Ubuntu GeoClue service with caching"""
    global _cached_location, _last_location_fetch
    
    current_time = time.time()
    # Use cache if it's less than 30 minutes old (1800 seconds)
    with _cache_lock:
        if _cached_location and (current_time - _last_location_fetch < 1800):
            return _cached_location
    
    try:
        # Try to use GeoClue2 via D-Bus
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            
            # Initialize D-Bus
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            
            # Connect to GeoClue2
            geoclue = bus.get_object('org.freedesktop.GeoClue2', '/org/freedesktop/GeoClue2/Manager')
            manager = dbus.Interface(geoclue, 'org.freedesktop.GeoClue2.Manager')
            
            # Get client
            client_path = manager.GetClient()
            client = bus.get_object('org.freedesktop.GeoClue2', client_path)
            client_interface = dbus.Interface(client, 'org.freedesktop.GeoClue2.Client')
            
            # Set properties
            props = dbus.Interface(client, 'org.freedesktop.DBus.Properties')
            props.Set('org.freedesktop.GeoClue2.Client', 'DesktopId', 'asset-agent')
            props.Set('org.freedesktop.GeoClue2.Client', 'RequestedAccuracyLevel', dbus.UInt32(8))  # EXACT level
            
            # Start location updates
            client_interface.Start()
            
            # Get location
            location_path = props.Get('org.freedesktop.GeoClue2.Client', 'Location')
            
            if location_path and location_path != '/':
                location = bus.get_object('org.freedesktop.GeoClue2', location_path)
                location_props = dbus.Interface(location, 'org.freedesktop.DBus.Properties')
                
                latitude = float(location_props.Get('org.freedesktop.GeoClue2.Location', 'Latitude'))
                longitude = float(location_props.Get('org.freedesktop.GeoClue2.Location', 'Longitude'))
                accuracy = float(location_props.Get('org.freedesktop.GeoClue2.Location', 'Accuracy'))
                
                # Stop client
                client_interface.Stop()
                
                logger.info(f"Successfully obtained location from GeoClue (lat: {latitude}, lon: {longitude}, accuracy: {accuracy}m)")
                
                result = {
                    "public_ip": "",
                    "location_country": "",
                    "location_region": "",
                    "location_city": "",
                    "location_latitude": float(latitude),
                    "location_longitude": float(longitude),
                    "location_source": "geoclue"
                }
                
                # Update cache
                with _cache_lock:
                    _cached_location = result
                    _last_location_fetch = current_time
                
                return result
            else:
                logger.warning("GeoClue returned no location")
                
        except ImportError:
            logger.warning("GeoClue not available (dbus-python not installed)")
        except dbus.exceptions.DBusException as e:
            logger.warning(f"GeoClue D-Bus error: {e}")
        except Exception as e:
            logger.warning(f"Error accessing GeoClue: {e}")
        
        # Fallback to IP-based location if GeoClue fails
        logger.info("Falling back to IP-based location")
        
        providers = [
            {"url": "https://ipapi.co/json/", "name": "ipapi.co"},
            {"url": "https://ipinfo.io/json", "name": "ipinfo.io"},
            {"url": "https://ifconfig.co/json", "name": "ifconfig.co"}
        ]
        
        for provider in providers:
            try:
                response = requests.get(provider["url"], timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Successfully fetched IP-based location data from {provider['name']}")
                    
                    res = {
                        "public_ip": str(data.get("ip", "")),
                        "location_country": "",
                        "location_region": "",
                        "location_city": str(data.get("city", "")),
                        "location_latitude": 0.0,
                        "location_longitude": 0.0,
                        "location_source": "ip"
                    }

                    if provider["name"] == "ipapi.co":
                        res["location_country"] = str(data.get("country_name", ""))
                        res["location_region"] = str(data.get("region", ""))
                        res["location_latitude"] = float(data.get("latitude", 0.0))
                        res["location_longitude"] = float(data.get("longitude", 0.0))
                    elif provider["name"] == "ipinfo.io":
                        res["location_country"] = str(data.get("country", ""))
                        res["location_region"] = str(data.get("region", ""))
                        loc = data.get("loc", "").split(',')
                        if len(loc) == 2:
                            res["location_latitude"] = float(loc[0])
                            res["location_longitude"] = float(loc[1])
                    elif provider["name"] == "ifconfig.co":
                        res["location_country"] = str(data.get("country", ""))
                        res["location_region"] = str(data.get("region_name", ""))
                        res["location_latitude"] = float(data.get("latitude", 0.0))
                        res["location_longitude"] = float(data.get("longitude", 0.0))
                    
                    # Update cache
                    with _cache_lock:
                        _cached_location = res
                        _last_location_fetch = current_time
                    
                    return res
                else:
                    logger.warning(f"IP provider {provider['name']} failed (HTTP {response.status_code}), trying fallback")
            except Exception as e:
                logger.warning(f"Error fetching from {provider['name']}: {e}, trying fallback")
        
        # If all fail but we have an old cache, return it rather than empty
        with _cache_lock:
            if _cached_location:
                return _cached_location
        
        result = {
            "public_ip": "",
            "location_country": "",
            "location_region": "",
            "location_city": "",
            "location_latitude": 0.0,
            "location_longitude": 0.0,
            "location_source": "unavailable"
        }
        
        with _cache_lock:
            _cached_location = result
            _last_location_fetch = current_time
        
        return result
        
    except Exception as e:
        logger.error(f"Unexpected error in location detection: {e}")
        result = {
            "public_ip": "",
            "location_country": "",
            "location_region": "",
            "location_city": "",
            "location_latitude": 0.0,
            "location_longitude": 0.0,
            "location_source": "unavailable"
        }
        with _cache_lock:
            _cached_location = result
            _last_location_fetch = current_time
        return result


# ============================================================================
# SYSTEM INFORMATION FUNCTIONS (CACHED)
# ============================================================================

def run_command(cmd, shell=False):
    """Run a shell command and return output"""
    try:
        if shell:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logger.debug(f"Command failed: {cmd}, Error: {e}")
        return None


def get_serial_number():
    """Get system serial number using multiple methods (cached)"""
    global _cached_serial_number
    with _cache_lock:
        if _cached_serial_number is not None:
            return _cached_serial_number
    
    try:
        serial = None
        
        # Method 1: Try dmidecode (requires root)
        output = run_command(['dmidecode', '-s', 'system-serial-number'])
        if output and output.upper() not in ['TO BE FILLED BY O.E.M.', '0', 'NONE', 'UNKNOWN', 'DEFAULT STRING', 'NOT SPECIFIED', '']:
            serial = output
        
        # Method 2-5: Try various /sys/class/dmi/id/ files (usually readable without root)
        if not serial:
            for dmi_file in ['product_serial', 'board_serial', 'product_uuid', 'chassis_serial']:
                try:
                    path = f'/sys/class/dmi/id/{dmi_file}'
                    if os.path.exists(path):
                        with open(path, 'r') as f:
                            val = f.read().strip()
                            if val and val.upper() not in ['TO BE FILLED BY O.E.M.', '0', 'NONE', 'UNKNOWN', 'DEFAULT STRING', 'NOT SPECIFIED', '']:
                                serial = val
                                break
                except:
                    continue
        
        # Method 6: Try /proc/cpuinfo
        if not serial:
            try:
                if os.path.exists('/proc/cpuinfo'):
                    with open('/proc/cpuinfo', 'r') as f:
                        for line in f:
                            if line.lower().startswith('serial'):
                                val = line.split(':')[-1].strip()
                                if val and val not in ['0000000000000000']:
                                    serial = val
                                    break
            except:
                pass
        
        # Method 7: Fallback to MAC address as a unique identifier
        if not serial:
            try:
                for interface, addrs in psutil.net_if_addrs().items():
                    if interface == 'lo': continue
                    for addr in addrs:
                        if hasattr(psutil, 'AF_LINK') and addr.family == psutil.AF_LINK:
                            serial = f"MAC-{addr.address.replace(':', '').upper()}"
                            break
                    if serial: break
            except:
                pass

        if serial:
            with _cache_lock:
                _cached_serial_number = serial
            return serial
        
        with _cache_lock:
            _cached_serial_number = "UNKNOWN"
        return "UNKNOWN"
        
    except Exception as e:
        logger.warning(f"Error getting serial number: {e}")
        return "UNKNOWN"


def get_device_model():
    """Get device manufacturer and model (cached)"""
    global _cached_device_model
    with _cache_lock:
        if _cached_device_model is not None:
            return _cached_device_model
    
    try:
        manufacturer = None
        model = None
        
        # Try dmidecode first
        manufacturer = run_command(['dmidecode', '-s', 'system-manufacturer'])
        model = run_command(['dmidecode', '-s', 'system-product-name'])
        
        # Fallback to /sys/class/dmi/id/
        if not manufacturer:
            try:
                with open('/sys/class/dmi/id/sys_vendor', 'r') as f:
                    manufacturer = f.read().strip()
            except:
                pass
        
        if not model:
            try:
                with open('/sys/class/dmi/id/product_name', 'r') as f:
                    model = f.read().strip()
            except:
                pass
        
        # Clean up common placeholder values
        if manufacturer and manufacturer.upper() in ['TO BE FILLED BY O.E.M.', 'SYSTEM MANUFACTURER', 'DEFAULT STRING']:
            manufacturer = None
        if model and model.upper() in ['TO BE FILLED BY O.E.M.', 'SYSTEM PRODUCT NAME', 'DEFAULT STRING']:
            model = None
        
        # Build device model string
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
        # Try lspci
        output = run_command(['lspci'], shell=False)
        if output:
            gpu_lines = [line for line in output.split('\n') if 'VGA' in line or 'Display' in line or '3D' in line]
            if gpu_lines:
                # Extract GPU name (after ": ")
                gpus = []
                for line in gpu_lines:
                    if ':' in line:
                        gpu_name = line.split(':', 2)[-1].strip()
                        gpus.append(gpu_name)
                
                if gpus:
                    gpu_info = ", ".join(gpus)
                    with _cache_lock:
                        _cached_graphics_card = gpu_info
                    return gpu_info
        
        # Fallback: Check /sys/class/drm
        try:
            drm_path = '/sys/class/drm'
            if os.path.exists(drm_path):
                cards = [d for d in os.listdir(drm_path) if d.startswith('card') and '-' not in d]
                if cards:
                    gpu_info = f"DRM Graphics Card ({len(cards)} device(s))"
                    with _cache_lock:
                        _cached_graphics_card = gpu_info
                    return gpu_info
        except:
            pass
        
        with _cache_lock:
            _cached_graphics_card = "No GPU Detected"
        return "No GPU Detected"
        
    except Exception as e:
        logger.warning(f"Error getting graphics card: {e}")
        with _cache_lock:
            _cached_graphics_card = "Detection Failed"
        return "Detection Failed"


def get_disk_type():
    """Detect if primary disk is SSD or HDD"""
    try:
        # Method 1: Use lsblk -d -o NAME,ROTA,TYPE
        output = run_command(['lsblk', '-d', '-o', 'NAME,ROTA,TYPE'], shell=False)
        if output:
            for line in output.split('\n')[1:]: # Skip header
                parts = line.split()
                if len(parts) >= 3 and parts[2] == 'disk':
                    name, rota = parts[0], parts[1]
                    if rota == '0':
                        return "SSD"
                    elif rota == '1':
                        return "HDD"

        # Method 2: Try to find the device for root and check sysfs
        root_partition = run_command(['df', '/', '--output=source'], shell=False)
        if root_partition:
            lines = root_partition.split('\n')
            if len(lines) >= 2:
                device_path = lines[1].strip()
                device_name = os.path.basename(device_path)
                
                # Check /sys/class/block/NAME/slaves if it's a partition
                sys_block_path = f'/sys/class/block/{device_name}'
                if os.path.exists(sys_block_path):
                    # Handle nvme0n1p3 -> nvme0n1, sda1 -> sda
                    m = re.match(r'(nvme\d+n\d+)(p\d+)?', device_name)
                    if m:
                        device_name = m.group(1)
                    else:
                        device_name = re.sub(r'\d+$', '', device_name)
                    
                    rotational_path = f'/sys/block/{device_name}/queue/rotational'
                    if os.path.exists(rotational_path):
                        with open(rotational_path, 'r') as f:
                            if f.read().strip() == '0':
                                return "SSD"
                            else:
                                return "HDD"

        # Method 3: Check all devices in /sys/block/
        if os.path.exists('/sys/block/'):
            for dev in os.listdir('/sys/block/'):
                if dev.startswith(('sd', 'nvme', 'vd')):
                    rot_path = f'/sys/block/{dev}/queue/rotational'
                    if os.path.exists(rot_path):
                        with open(rot_path, 'r') as f:
                            if f.read().strip() == '0':
                                return "SSD"
        
        return "Unknown"
    except Exception as e:
        logger.debug(f"Error detecting disk type: {e}")
        return "Unknown"


def get_storage_volumes():
    """Detect all mounted filesystems"""
    try:
        volumes = []
        
        # Linux (Ubuntu) storage detection using mount points
        partitions = psutil.disk_partitions(all=False)
        seen_mountpoints = set()
        
        for partition in partitions:
            mount = partition.mountpoint
            if not partition.fstype or mount in seen_mountpoints:
                continue
            
            # Ignore virtual filesystems
            if partition.fstype in ['tmpfs', 'proc', 'sysfs', 'overlay', 'devtmpfs', 'squashfs']:
                continue
            
            # Treat "/" as system, others as Data
            is_valid_mount = mount == "/" or mount == "/home" or mount == "/data" or mount.startswith("/mnt/")
            
            if is_valid_mount:
                try:
                    usage = psutil.disk_usage(mount)
                    if usage.total == 0:
                        continue
                        
                    volumes.append({
                        'drive_letter': mount,
                        'total_size': round(usage.total / (1024 ** 3), 2),
                        'free_space': round(usage.free / (1024 ** 3), 2),
                        'used_space': round(usage.used / (1024 ** 3), 2),
                        'drive_label': "System" if mount == "/" else "Data"
                    })
                    seen_mountpoints.add(mount)
                except Exception as e:
                    logger.debug(f"Error reading partition {mount}: {e}")
                    continue
        
        return json.dumps(volumes)
            
    except Exception as e:
        logger.warning(f"Error getting storage volumes: {e}")
        return "[]"


def get_installed_apps():
    """Collect installed applications from dpkg, rpm, snap, and flatpak"""
    all_apps = {}
    
    # Detect package manager
    has_dpkg = run_command(['which', 'dpkg']) is not None
    has_rpm = run_command(['which', 'rpm']) is not None
    has_snap = run_command(['which', 'snap']) is not None
    has_flatpak = run_command(['which', 'flatpak']) is not None
    
    # Method 1: dpkg (Debian/Ubuntu)
    if has_dpkg:
        try:
            # Step 1: Execute apt-mark showmanual
            manual_output = run_command(['apt-mark', 'showmanual'], shell=False)
            manual_packages = set(line.strip() for line in manual_output.split('\n') if line.strip()) if manual_output else set()

            output = run_command(['dpkg', '-l'], shell=False)
            if output:
                lines = output.split('\n')
                for line in lines:
                    if line.startswith('ii'):
                        parts = line.split()
                        if len(parts) >= 3:
                            name = parts[1]
                            pkg_name = name.split(':')[0]
                            version = parts[2]
                            
                            # Rule 2: Include ONLY if in manual_packages
                            if pkg_name not in manual_packages and name not in manual_packages:
                                continue
                            
                            # Rule 3: Filter system/dependency packages, OS services, libraries
                            exclude_prefixes = (
                                'libc', 'lib', 'linux-', 'systemd', 'python3-', 'gcc-', 
                                'grub', 'snapd', 'udev', 'mesa', 'xserver', 'fonts-', 'firmware-',
                                'binutils', 'coreutils', 'cpp-', 'dash', 'diffutils', 'dpkg',
                                'findutils', 'grep', 'gzip', 'hostname', 'init', 'iproute2',
                                'iptables', 'iputils-', 'kmod', 'login', 'lsb-', 'mount',
                                'netbase', 'ncurses-', 'pam', 'perl', 'sed', 'tar', 'util-linux'
                            )
                            
                            if pkg_name.startswith(exclude_prefixes):
                                continue
                                
                            # Get installed date from /var/lib/dpkg/info/<package>.list
                            installed_date = ""
                            list_file = f"/var/lib/dpkg/info/{name}.list"
                            if os.path.exists(list_file):
                                try:
                                    st = os.stat(list_file)
                                    installed_date = datetime.fromtimestamp(st.st_ctime).strftime('%Y-%m-%d')
                                except Exception:
                                    pass

                            # Get size using dpkg-query (returns KB, convert to MB)
                            size_mb = 0.0
                            size_kb_str = run_command(['dpkg-query', '-W', '-f=${Installed-Size}', name])
                            if size_kb_str:
                                try:
                                    size_kb = float(size_kb_str.strip().strip("'"))
                                    size_mb = round(size_kb / 1024, 2)
                                except (ValueError, TypeError):
                                    pass

                            all_apps[name] = {
                                'name': name[:255],
                                'publisher': 'System Package',
                                'version': version[:100],
                                'installed_date': installed_date,
                                'size': size_mb
                            }
            logger.info(f"dpkg: Found {len(all_apps)} packages")
        except Exception as e:
            logger.warning(f"dpkg read failed: {e}")
    
    # Method 2: rpm (RedHat/CentOS/Fedora)
    if has_rpm:
        try:
            output = run_command(['rpm', '-qa', '--queryformat', '%{NAME}|%{VERSION}|%{VENDOR}\n'], shell=False)
            if output:
                lines = output.split('\n')
                for line in lines:
                    if '|' in line:
                        parts = line.split('|')
                        if len(parts) >= 2:
                            name = parts[0].strip()
                            version = parts[1].strip()
                            publisher = parts[2].strip() if len(parts) > 2 else 'System Package'
                            
                            if name not in all_apps:
                                all_apps[name] = {
                                    'name': name[:255],
                                    'publisher': publisher[:255],
                                    'version': version[:100],
                                    'installed_date': '',
                                    'size': 0
                                }
            logger.info(f"rpm: Found {len(all_apps)} packages")
        except Exception as e:
            logger.warning(f"rpm read failed: {e}")
    
    # Method 3: Snap packages
    if has_snap:
        try:
            output = run_command(['snap', 'list'], shell=False)
            if output:
                lines = output.split('\n')[1:]  # Skip header
                for line in lines:
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 2:
                            name = parts[0]
                            version = parts[1]
                            publisher = parts[3] if len(parts) > 3 else 'Snap Store'
                            
                            snap_name = f"{name} (Snap)"
                            if snap_name not in all_apps:
                                all_apps[snap_name] = {
                                    'name': snap_name[:255],
                                    'publisher': publisher[:255],
                                    'version': version[:100],
                                    'installed_date': '',
                                    'size': 0
                                }
            logger.info(f"snap: Found {len([k for k in all_apps.keys() if 'Snap' in k])} snap packages")
        except Exception as e:
            logger.warning(f"snap read failed: {e}")
    
    # Method 4: Flatpak packages
    if has_flatpak:
        try:
            output = run_command(['flatpak', 'list', '--app', '--columns=name,version,origin'], shell=False)
            if output:
                lines = output.split('\n')[1:]  # Skip header
                for line in lines:
                    if line.strip():
                        parts = line.split('\t')
                        if len(parts) >= 1:
                            name = parts[0].strip()
                            version = parts[1].strip() if len(parts) > 1 else ''
                            origin = parts[2].strip() if len(parts) > 2 else 'Flathub'
                            
                            flatpak_name = f"{name} (Flatpak)"
                            if flatpak_name not in all_apps:
                                all_apps[flatpak_name] = {
                                    'name': flatpak_name[:255],
                                    'publisher': origin[:255],
                                    'version': version[:100],
                                    'installed_date': '',
                                    'size': 0
                                }
            logger.info(f"flatpak: Found {len([k for k in all_apps.keys() if 'Flatpak' in k])} flatpak packages")
        except Exception as e:
            logger.warning(f"flatpak read failed: {e}")
    
    if len(all_apps) == 0:
        logger.warning("No applications found from any package manager")
        return "[]"
    
    logger.info(f"OK Total unique applications: {len(all_apps)}")
    return json.dumps(list(all_apps.values()))


def get_os_version():
    """Detect OS name and version"""
    try:
        # Try lsb_release first
        output = run_command(['lsb_release', '-d'], shell=False)
        if output and ':' in output:
            return output.split(':', 1)[1].strip()
        
        # Fallback to /etc/os-release
        if os.path.exists('/etc/os-release'):
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        return line.split('=', 1)[1].strip().strip('"')
        
        # Last resort: platform module
        return f"{platform.system()} {platform.release()}"
        
    except Exception as e:
        logger.debug(f"Error getting OS version: {e}")
        return f"{platform.system()} {platform.release()}"


def get_processor_info():
    """Get processor information"""
    try:
        # Try lscpu
        output = run_command(['lscpu'], shell=False)
        if output:
            for line in output.split('\n'):
                if line.startswith('Model name:'):
                    return line.split(':', 1)[1].strip()
        
        # Fallback to /proc/cpuinfo
        if os.path.exists('/proc/cpuinfo'):
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('model name'):
                        return line.split(':', 1)[1].strip()
        
        return platform.processor() or "Unknown Processor"
        
    except Exception as e:
        logger.debug(f"Error getting processor info: {e}")
        return "Unknown Processor"


def get_battery_capacity():
    """Get battery capacity from /sys/class/power_supply/"""
    try:
        power_supply_path = '/sys/class/power_supply'
        if not os.path.exists(power_supply_path):
            return 0, 0
        
        # Find battery directory
        batteries = [d for d in os.listdir(power_supply_path) if d.startswith('BAT')]
        if not batteries:
            return 0, 0
        
        battery_path = os.path.join(power_supply_path, batteries[0])
        
        # Try to read energy_full (in µWh)
        energy_full_path = os.path.join(battery_path, 'energy_full')
        charge_full_path = os.path.join(battery_path, 'charge_full')
        
        capacity_mwh = 0
        
        if os.path.exists(energy_full_path):
            with open(energy_full_path, 'r') as f:
                capacity_uwh = int(f.read().strip())
                capacity_mwh = capacity_uwh / 1000  # Convert µWh to mWh
        elif os.path.exists(charge_full_path):
            with open(charge_full_path, 'r') as f:
                capacity_uah = int(f.read().strip())
                capacity_mah = capacity_uah / 1000  # Convert µAh to mAh
                # Assume ~11.1V for conversion
                capacity_mwh = capacity_mah * 11.1
        
        if capacity_mwh == 0:
            return 0, 0
        
        # Convert mWh to mAh (assuming ~11.1V)
        capacity_mah = round(capacity_mwh / 11.1, 2)
        
        return int(capacity_mwh), capacity_mah
        
    except Exception as e:
        logger.debug(f"Error getting battery capacity: {e}")
        return 0, 0


def get_uptime():
    """Get system uptime in human-readable format"""
    try:
        if os.path.exists('/proc/uptime'):
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
        else:
            uptime_seconds = time.time() - psutil.boot_time()
            
        days = int(uptime_seconds // (24 * 3600))
        hours = int((uptime_seconds % (24 * 3600)) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0 or not parts:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
            
        return ", ".join(parts)
    except Exception as e:
        logger.debug(f"Error getting uptime: {e}")
        return "Unknown"


def get_total_storage_size():
    """Get total storage size in GB using multiple detection methods"""
    try:
        total_gb = 0.0
        partitions = psutil.disk_partitions(all=False)
        seen_mountpoints = set()
        
        for partition in partitions:
            mount = partition.mountpoint
            if not partition.fstype or mount in seen_mountpoints:
                continue
            
            # Ignore virtual filesystems
            if partition.fstype in ['tmpfs', 'proc', 'sysfs', 'overlay', 'devtmpfs', 'squashfs']:
                continue
            
            # Only sum real storage volumes as per requirements
            is_valid_mount = mount == "/" or mount == "/home" or mount == "/data" or mount.startswith("/mnt/")
            
            if is_valid_mount:
                try:
                    usage = psutil.disk_usage(mount)
                    total_gb += usage.total / (1024 ** 3)
                    seen_mountpoints.add(mount)
                except:
                    continue
        
        return round(total_gb, 2)
    except Exception as e:
        logger.warning(f"Error calculating total storage size: {e}")
        return 0.0


# ============================================================================
# DATA COLLECTION FUNCTIONS
# ============================================================================

def collect_static_data():
    """Collect static system information"""
    try:
        battery = psutil.sensors_battery()
        battery_percentage = round(battery.percent, 2) if battery else 0
        _, battery_capacity_mah = get_battery_capacity()
        
        payload = {
            "serial_number": get_serial_number(),
            "hostname": socket.gethostname(),
            "device_name": get_device_model(),
            "processor": get_processor_info(),
            "os_type": platform.architecture()[0],
            "os_name": get_os_version(),
            "ram_size": round(psutil.virtual_memory().total / (1024 ** 3), 2),
            "rom_size": get_total_storage_size(),
            "disk_type": get_disk_type(),
            "graphics_card_raw": get_graphics_card(),
            "battery_capacity": battery_capacity_mah,
            "battery_percentage": battery_percentage,
            "storage_volumes": get_storage_volumes(),
            "installed_apps": get_installed_apps(),
            "uptime": get_uptime(),
            "agent_version": AGENT_VERSION
        }
        
        # Add location data
        location_data = get_location_data()
        payload.update(location_data)
        
        return payload
        
    except Exception as e:
        logger.error(f"Error collecting static data: {e}", exc_info=True)
        payload = {
            "serial_number": get_serial_number(),
            "hostname": socket.gethostname(),
            "device_name": "Unknown",
            "processor": "Unknown",
            "os_type": "64bit",
            "os_name": "Linux",
            "ram_size": 0,
            "rom_size": 0,
            "disk_type": "Unknown",
            "graphics_card_raw": "Unknown",
            "battery_capacity": 0,
            "battery_percentage": 0,
            "storage_volumes": "[]",
            "installed_apps": "[]",
            "uptime": "Unknown",
            "agent_version": AGENT_VERSION
        }
        
        # Add location data even on error
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
        disk = psutil.disk_usage('/')
        disk_usage = round(disk.percent, 2)
        upload_mbps, download_mbps = network_monitor.get_network_usage()
        
        battery = psutil.sensors_battery()
        battery_percentage = round(battery.percent, 2) if battery else 0
        
        return {
            "serial_number": serial_number,
            "hostname": hostname,
            "cpu_usage_percent": cpu_usage,
            "ram_usage_percent": ram_usage,
            "disk_usage_percent": disk_usage,
            "network_upload_mbps": upload_mbps,
            "network_download_mbps": download_mbps,
            "battery_percentage": battery_percentage,
            "uptime": get_uptime(),
            "heartbeat": current_heartbeat,
            "agent_version": AGENT_VERSION
        }
    except Exception as e:
        logger.warning(f"Error collecting live data: {e}")
        return {
            "serial_number": serial_number,
            "hostname": hostname,
            "cpu_usage_percent": 0,
            "ram_usage_percent": 0,
            "disk_usage_percent": 0,
            "network_upload_mbps": 0,
            "network_download_mbps": 0,
            "battery_percentage": 0,
            "uptime": get_uptime(),
            "heartbeat": current_heartbeat,
            "agent_version": AGENT_VERSION
        }


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main():
    """Main entry point"""
    logger.info("=" * 60)
    logger.info(f"Ubuntu Asset Agent v{AGENT_VERSION} Starting")
    logger.info("=" * 60)
    
    # Start auto-update checker
    update_thread = threading.Thread(target=update_checker_thread, daemon=True)
    update_thread.start()
    logger.info(f"Auto-update checker started (checks every {UPDATE_CHECK_INTERVAL}s)")
    
    # Initial system detection
    logger.info("Performing initial system detection...")
    serial = get_serial_number()
    hostname = socket.gethostname()
    model = get_device_model()
    logger.info(f"Device: {model}")
    logger.info(f"Hostname: {hostname}")
    logger.info(f"Serial: {serial}")
    logger.info(f"Version: {AGENT_VERSION}")
    logger.info(f"OS: {get_os_version()}")
    logger.info(f"Uptime: {get_uptime()}")
    
    # Fetch and log location
    logger.info("Fetching location data...")
    location = get_location_data()
    logger.info(f"Location: {location.get('location_city', 'Unknown')}, {location.get('location_region', 'Unknown')}, {location.get('location_country', 'Unknown')}")
    logger.info(f"Public IP: {location.get('public_ip', 'Unknown')}")
    logger.info(f"Location Source: {location.get('location_source', 'Unknown')}")
    
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
    
    # Start continuous monitoring loop
    logger.info(f"Starting continuous monitoring loop...")
    logger.info(f"Static sync interval: {STATIC_SYNC_INTERVAL}s")
    logger.info(f"Live sync (heartbeat) interval: {LIVE_SYNC_INTERVAL}s")
    
    next_static_sync = time.monotonic() + STATIC_SYNC_INTERVAL
    next_live_sync = time.monotonic() + LIVE_SYNC_INTERVAL
    
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
                                logger.warning("Static sync failed, will retry in next interval")
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
                            success, response = send_with_retry(f"{ODOO_API_URL}/live", payload, max_retries=3, timeout=10)
                            if not success:
                                logger.warning("Live sync failed, will retry in next interval")
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