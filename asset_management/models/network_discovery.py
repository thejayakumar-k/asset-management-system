from odoo import models, fields, api
import logging
import subprocess
import ipaddress

_logger = logging.getLogger(__name__)

SNMP_AVAILABLE = False  # Using subprocess snmpget instead

# Standard SNMP OIDs for fingerprinting
OID_SYS_DESCR    = '1.3.6.1.2.1.1.1.0'
OID_SYS_NAME     = '1.3.6.1.2.1.1.5.0'
OID_SYS_LOCATION = '1.3.6.1.2.1.1.6.0'
OID_SYS_UPTIME   = '1.3.6.1.2.1.1.3.0'
OID_SYS_CONTACT  = '1.3.6.1.2.1.1.4.0'

# Device type fingerprint keywords
DEVICE_FINGERPRINTS = {
    'router': [
        'router', 'cisco ios', 'junos', 'routeros', 'mikrotik',
        'edge', 'gateway', 'broadband', 'dsl', 'wan'
    ],
    'switch': [
        'switch', 'catalyst', 'nexus', 'procurve', 'powerconnect',
        'stackable', 'layer 2', 'layer 3', 'vlan', 'spanning tree'
    ],
    'firewall': [
        'firewall', 'asa', 'fortigate', 'palo alto', 'checkpoint',
        'sonicwall', 'watchguard', 'iptables', 'netscreen', 'juniper srx'
    ],
    'access_point': [
        'access point', 'wireless', 'aironet', 'unifi', 'aruba',
        'wifi', 'wlan', 'ap ', '802.11', 'meraki'
    ],
}

# Manufacturer fingerprint keywords
MANUFACTURER_FINGERPRINTS = {
    'cisco':    ['cisco', 'catalyst', 'aironet', 'nexus', 'asa'],
    'juniper':  ['juniper', 'junos', 'srx', 'ex series'],
    'hp':       ['hp ', 'hewlett', 'procurve', 'aruba'],
    'mikrotik': ['mikrotik', 'routeros'],
    'ubiquiti': ['ubiquiti', 'unifi', 'edgeos'],
    'tp_link':  ['tp-link', 'tplink'],
    'dell':     ['dell', 'powerconnect', 'force10'],
    'fortinet': ['fortinet', 'fortigate'],
}


class NetworkDiscoveryService(models.Model):
    _name = 'network.discovery.service'
    _description = 'Network Auto Discovery Service'

    # ─── Config fields (single record) ───────────────────────────────────────
    name = fields.Char(default='Network Discovery Config')
    subnet = fields.Char(
        string='Subnet to Scan',
        default='192.168.1.0/24',
        help='CIDR notation e.g. 192.168.105.0/24'
    )
    snmp_community = fields.Char(default='public')
    snmp_port = fields.Integer(default=161)
    scan_threads = fields.Integer(default=50)
    auto_create = fields.Boolean(
        default=True,
        string='Auto-create new devices',
        help='Automatically create records for newly discovered devices'
    )
    last_scan = fields.Datetime(readonly=True)
    discovered_count = fields.Integer(readonly=True)

    # ─── Ping ────────────────────────────────────────────────────────────────
    def _ping(self, ip):
        """Ping using icmplib (works without ping binary in Docker)"""
        try:
            from icmplib import ping as icmp_ping
            result = icmp_ping(str(ip), count=1, timeout=2, privileged=False)
            return result.is_alive
        except Exception as e:
            _logger.debug(f"icmplib ping failed for {ip}: {e}, trying subprocess...")

        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '1', str(ip)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3
            )
            return result.returncode == 0
        except Exception:
            return False

    # ─── SNMP single GET via subprocess ──────────────────────────────────────
    def _snmp_get(self, ip, oid, community='public', port=161):
        try:
            cmd = ['snmpget', '-Oqv', '-v', '2c', '-c', community,
                   '-t', '2', '-r', '0', f'{ip}:{port}', oid]
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5
            )
            if result.returncode == 0:
                value = result.stdout.decode().strip().strip('"')
                return value if value else None
        except Exception:
            pass
        return None

    # ─── Fingerprint device type from sysDescr ───────────────────────────────
    def _fingerprint_device_type(self, sys_descr):
        if not sys_descr:
            return 'router'
        desc_lower = sys_descr.lower()
        for device_type, keywords in DEVICE_FINGERPRINTS.items():
            for kw in keywords:
                if kw in desc_lower:
                    return device_type
        return 'router'

    def _fingerprint_manufacturer(self, sys_descr):
        if not sys_descr:
            return 'generic'
        desc_lower = sys_descr.lower()
        for manufacturer, keywords in MANUFACTURER_FINGERPRINTS.items():
            for kw in keywords:
                if kw in desc_lower:
                    return manufacturer
        return 'generic'

    # ─── Scan one IP ─────────────────────────────────────────────────────────
    def _scan_ip(self, ip, community, port):
        ip_str = str(ip)
        if not self._ping(ip_str):
            return None

        result = {
            'ip': ip_str,
            'sys_descr': None,
            'sys_name': None,
            'sys_location': None,
            'uptime': None,
            'snmp_reachable': False,
        }

        sys_descr = self._snmp_get(ip_str, OID_SYS_DESCR, community, port)
        if sys_descr:
            result['snmp_reachable'] = True
            result['sys_descr'] = sys_descr
            result['sys_name'] = self._snmp_get(ip_str, OID_SYS_NAME, community, port)
            result['sys_location'] = self._snmp_get(ip_str, OID_SYS_LOCATION, community, port)
            result['uptime'] = self._snmp_get(ip_str, OID_SYS_UPTIME, community, port)

        return result

    # ─── Auto setup on new database ──────────────────────────────────────────
    @api.model
    def _auto_setup_on_startup(self):
        """Auto-create discovery config and run first scan if none exists."""
        import socket
        existing = self.search([], limit=1)
        if not existing:
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
                parts = ip.split('.')
                subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
            except Exception:
                subnet = '192.168.1.0/24'

            existing = self.create({
                'name': 'Auto Network Discovery',
                'subnet': subnet,
                'snmp_community': 'public',
                'snmp_port': 161,
                'scan_threads': 50,
                'auto_create': True,
            })
            _logger.info(f"[Discovery] Auto-created config for subnet: {subnet}")
            existing.run_discovery()

    # ─── Main discovery ──────────────────────────────────────────────────────
    def run_discovery(self):
        """
        Scan subnet, ping all IPs, SNMP fingerprint live ones,
        auto-create/update asset.network.device records.
        Called by cron or manually.
        """
        config = self if self.id else self.search([], limit=1)
        if not config:
            config = self.create({
                'name': 'Default Discovery Config',
                'subnet': '192.168.1.0/24',
            })

        subnet = config.subnet
        community = config.snmp_community or 'public'
        port = config.snmp_port or 161
        threads = config.scan_threads or 50

        _logger.info(f"[Discovery] Starting scan of {subnet}")

        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            _logger.error(f"[Discovery] Invalid subnet {subnet}: {e}")
            return

        hosts = list(network.hosts())
        discovered = []

        for ip in hosts:
            result = self._scan_ip(ip, community, port)
            if result:
                discovered.append(result)
                _logger.info(f"[Discovery] Found: {result['ip']} — {result.get('sys_name') or 'Unknown'}")

        _logger.info(f"[Discovery] Scan complete. Found {len(discovered)} live devices.")

        if config.auto_create:
            self._sync_devices(discovered, community, port)

        config.write({
            'last_scan': fields.Datetime.now(),
            'discovered_count': len(discovered),
        })

    # ─── Sync to asset.network.device ────────────────────────────────────────
    def _sync_devices(self, discovered, community, port):
        Device = self.env['asset.network.device']
        Sequence = self.env['ir.sequence']

        for item in discovered:
            ip = item['ip']
            sys_descr = item.get('sys_descr') or ''
            sys_name = item.get('sys_name') or ''
            sys_location = item.get('sys_location') or ''
            uptime = item.get('uptime') or ''

            device_type = self._fingerprint_device_type(sys_descr)
            manufacturer = self._fingerprint_manufacturer(sys_descr)

            existing = Device.search([('ip_address', '=', ip)], limit=1)

            if existing:
                vals = {
                    'connection_status': 'online',
                    'last_check': fields.Datetime.now(),
                    'last_online': fields.Datetime.now(),
                }
                if sys_descr:
                    vals['notes'] = sys_descr
                if uptime:
                    vals['uptime'] = uptime
                existing.write(vals)
                _logger.info(f"[Discovery] Updated existing device: {ip}")
            else:
                name = sys_name or f"Device-{ip.replace('.', '-')}"
                code = Sequence.next_by_code('asset.asset.network') or f"NET-{ip.replace('.', '')}"

                Device.create({
                    'name': name,
                    'device_code': code,
                    'device_type': device_type,
                    'manufacturer': manufacturer,
                    'ip_address': ip,
                    'location': sys_location or 'Auto-discovered',
                    'snmp_community': community,
                    'snmp_port': port,
                    'snmp_version': 'v2c',
                    'connection_status': 'online',
                    'last_check': fields.Datetime.now(),
                    'last_online': fields.Datetime.now(),
                    'uptime': uptime,
                    'notes': sys_descr,
                    'is_active': True,
                })
                _logger.info(f"[Discovery] Created new device: {name} ({ip}) type={device_type}")