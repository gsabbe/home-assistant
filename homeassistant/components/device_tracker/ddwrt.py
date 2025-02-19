"""
homeassistant.components.device_tracker.ddwrt
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Device tracker platform that supports scanning a DD-WRT router for device
presence.

Configuration:

To use the DD-WRT tracker you will need to add something like the following
to your configuration.yaml file.

device_tracker:
  platform: ddwrt
  host: YOUR_ROUTER_IP
  username: YOUR_ADMIN_USERNAME
  password: YOUR_ADMIN_PASSWORD

Variables:

host
*Required
The IP address of your router, e.g. 192.168.1.1.

username
*Required
The username of an user with administrative privileges, usually 'admin'.

password
*Required
The password for your given admin account.
"""
import logging
from datetime import timedelta
import re
import threading
import requests

from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers import validate_config
from homeassistant.util import Throttle
from homeassistant.components.device_tracker import DOMAIN

# Return cached results if last scan was less then this time ago
MIN_TIME_BETWEEN_SCANS = timedelta(seconds=5)

_LOGGER = logging.getLogger(__name__)

_DDWRT_DATA_REGEX = re.compile(r'\{(\w+)::([^\}]*)\}')
_MAC_REGEX = re.compile(r'(([0-9A-Fa-f]{1,2}\:){5}[0-9A-Fa-f]{1,2})')


# pylint: disable=unused-argument
def get_scanner(hass, config):
    """ Validates config and returns a DD-WRT scanner. """
    if not validate_config(config,
                           {DOMAIN: [CONF_HOST, CONF_USERNAME, CONF_PASSWORD]},
                           _LOGGER):
        return None

    scanner = DdWrtDeviceScanner(config[DOMAIN])

    return scanner if scanner.success_init else None


# pylint: disable=too-many-instance-attributes
class DdWrtDeviceScanner(object):
    """
    This class queries a wireless router running DD-WRT firmware
    for connected devices. Adapted from Tomato scanner.
    """

    def __init__(self, config):
        self.host = config[CONF_HOST]
        self.username = config[CONF_USERNAME]
        self.password = config[CONF_PASSWORD]

        self.lock = threading.Lock()

        self.last_results = {}

        self.mac2name = {}

        # Test the router is accessible
        url = 'http://{}/Status_Wireless.live.asp'.format(self.host)
        data = self.get_ddwrt_data(url)
        self.success_init = data is not None

    def scan_devices(self):
        """
        Scans for new devices and return a list containing found device ids.
        """

        self._update_info()

        return self.last_results

    def get_device_name(self, device):
        """ Returns the name of the given device or None if we don't know. """

        with self.lock:
            # if not initialised and not already scanned and not found
            if device not in self.mac2name:
                url = 'http://{}/Status_Lan.live.asp'.format(self.host)
                data = self.get_ddwrt_data(url)

                if not data:
                    return None

                dhcp_leases = data.get('dhcp_leases', None)

                if not dhcp_leases:
                    return None

                # remove leading and trailing single quotes
                cleaned_str = dhcp_leases.strip().strip('"')
                elements = cleaned_str.split('","')
                num_clients = int(len(elements)/5)
                self.mac2name = {}
                for idx in range(0, num_clients):
                    # this is stupid but the data is a single array
                    # every 5 elements represents one hosts, the MAC
                    # is the third element and the name is the first
                    mac_index = (idx * 5) + 2
                    if mac_index < len(elements):
                        mac = elements[mac_index]
                        self.mac2name[mac] = elements[idx * 5]

            return self.mac2name.get(device)

    @Throttle(MIN_TIME_BETWEEN_SCANS)
    def _update_info(self):
        """
        Ensures the information from the DD-WRT router is up to date.
        Returns boolean if scanning successful.
        """
        if not self.success_init:
            return False

        with self.lock:
            _LOGGER.info("Checking ARP")

            url = 'http://{}/Status_Wireless.live.asp'.format(self.host)
            data = self.get_ddwrt_data(url)

            if not data:
                return False

            self.last_results = []

            active_clients = data.get('active_wireless', None)
            if not active_clients:
                return False

            # This is really lame, instead of using JSON the DD-WRT UI
            # uses its own data format for some reason and then
            # regex's out values so I guess I have to do the same,
            # LAME!!!

            # remove leading and trailing single quotes
            clean_str = active_clients.strip().strip("'")
            elements = clean_str.split("','")

            self.last_results.extend(item for item in elements
                                     if _MAC_REGEX.match(item))

            return True

    def get_ddwrt_data(self, url):
        """ Retrieve data from DD-WRT and return parsed result. """
        try:
            response = requests.get(
                url,
                auth=(self.username, self.password),
                timeout=4)
        except requests.exceptions.Timeout:
            _LOGGER.exception("Connection to the router timed out")
            return
        if response.status_code == 200:
            return _parse_ddwrt_response(response.text)
        elif response.status_code == 401:
            # Authentication error
            _LOGGER.exception(
                "Failed to authenticate, "
                "please check your username and password")
            return
        else:
            _LOGGER.error("Invalid response from ddwrt: %s", response)


def _parse_ddwrt_response(data_str):
    """ Parse the DD-WRT data format. """
    return {
        key: val for key, val in _DDWRT_DATA_REGEX
        .findall(data_str)}
