"""
Custom integration for Roth Touchline with coordinated updates.
This version batches the XML request into smaller chunks to work around potential size limitations.
"""

import logging
import time
from datetime import timedelta
import xml.etree.ElementTree as ET
import httplib2
import cchardet
from http.client import RemoteDisconnected
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from pytouchline_extended import PyTouchline, Parameter
from .const import DOMAIN, CONF_HOST

_LOGGER = logging.getLogger(__name__)


class TouchlineDataUpdateCoordinator(DataUpdateCoordinator):
    """Fetch data from the Touchline hub for all configured devices using batched requests."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        device_ids: list[int],
        update_interval: timedelta,
    ) -> None:
        self.host = host
        self.device_ids = device_ids
        self._temp_scale = 100
        # Create one PyTouchline instance as a helper for XML generation.
        self._pytouchline = PyTouchline(url=host)
        # PATCH: Change the "upass" parameter type to G so it outputs a G‑prefix.
        for param in self._pytouchline._xml_element_list:
            if param.get_name() == "upass":
                param._type = Parameter.G
        super().__init__(
            hass,
            _LOGGER,
            name="touchline_data_coordinator",
            update_interval=update_interval,
        )

    def _generate_request_xml_for_batch(self, batch_ids: list[int]) -> str:
        """Generate one XML request for a batch of device IDs."""
        request_items = []
        for device_id in batch_ids:
            items = self._pytouchline._get_touchline_device_item(device_id)
            request_items.extend(items)
        xml = (
            "<body>"
            "<version>1.0</version>"
            "<client>IMaster6_02_00</client>"
            "<client_ver>6.02.0006</client_ver>"
            "<file_name>room</file_name>"
            "<item_list_size>0</item_list_size>"
            "<item_list>"
        )
        for item in request_items:
            xml += item
        xml += "</item_list></body>"
        _LOGGER.debug(
            "Generated batched request XML for devices %s: %s", batch_ids, xml
        )
        return xml

    def _parse_all_response(self, content: bytes) -> Dict[int, Dict[str, Any]]:
        """
        Parse the XML response and return a dictionary mapping device_id
        to a dictionary of parameter values.
        """
        encoding = cchardet.detect(content)["encoding"]
        root = ET.XML(content, parser=ET.XMLParser(encoding=encoding))
        data: Dict[int, Dict[str, Any]] = {}
        item_list = root.find("item_list")
        if item_list is None:
            _LOGGER.debug("No <item_list> found in the Touchline response.")
            return data

        # Assume each <i> element corresponds to one device in the batch.
        for i, elem in enumerate(item_list.findall("i")):
            # When batching, the device_id is taken from the batch list order.
            # We'll assign device id based on the order in the batch.
            device_id = i  # Remapping will occur later.
            device_data: Dict[str, Any] = {}
            children = list(elem)
            # Expect alternating <n> and <v> tags.
            for j in range(0, len(children), 2):
                if j + 1 < len(children):
                    key = children[
                        j
                    ].text  # e.g. "G0.name" (includes G prefix with device's id)
                    val = children[j + 1].text
                    device_data[key] = val
            data[device_id] = device_data
        return data

    def _sync_update_data(self) -> Dict[int, Dict[str, Any]]:
        """
        Synchronously perform batched HTTP requests to the Touchline hub, merge the responses,
        and stagger the requests by adding a delay between each batch.
        """
        h = httplib2.Http(timeout=30)
        headers = {"Content-Type": "text/xml"}
        batch_size = 3  # Adjust batch size as needed.
        merged_data: Dict[int, Dict[str, Any]] = {}

        # Process device IDs in batches.
        for batch_start in range(0, len(self.device_ids), batch_size):
            batch_ids = self.device_ids[batch_start : batch_start + batch_size]
            request_xml = self._generate_request_xml_for_batch(batch_ids)
            url = f"{self.host}/cgi-bin/ILRReadValues.cgi"
            resp, content = h.request(
                url, method="POST", body=request_xml, headers=headers
            )
            if resp.reason != "OK":
                msg = f"Error updating Touchline data for batch {batch_ids}: {resp.reason}"
                raise Exception(msg)
            batch_data = self._parse_all_response(content)
            # Remap the batch's data: batch_data keys are 0,1,... so map them to actual device IDs.
            for index, device_id in enumerate(batch_ids):
                merged_data[device_id] = batch_data.get(index, {})
            # Stagger the batches: if more batches remain, sleep a bit.
            if batch_start + batch_size < len(self.device_ids):
                time.sleep(1)  # 1 second delay; adjust as needed.
        return merged_data

    async def _async_update_data(self) -> Dict[int, Dict[str, Any]]:
        """
        Fetch data from the Touchline hub by batching requests.
        If an error occurs, return the cached data so that entities remain available.
        """
        try:
            return await self.hass.async_add_executor_job(self._sync_update_data)
        except Exception as e:
            _LOGGER.debug("Error updating Touchline data: %s. Using cached data.", e)
            return self.data if self.data is not None else {}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Touchline hub using coordinated updates with batched requests."""
    host = entry.data[CONF_HOST]
    pt = PyTouchline(url=host)
    try:
        number_of_devices = await hass.async_add_executor_job(
            lambda: int(pt.get_number_of_devices())
        )
    except Exception as e:
        _LOGGER.error("Error getting device count from %s: %s", host, e)
        number_of_devices = 0
    device_ids = list(range(number_of_devices))
    update_interval = timedelta(seconds=30)

    coordinator = TouchlineDataUpdateCoordinator(
        hass, host, device_ids, update_interval
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})["coordinator"] = coordinator

    # Forward the setup to the climate platform.
    await hass.config_entries.async_forward_entry_setups(entry, ["climate"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Touchline config entry."""
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "climate")
    if unload_ok:
        hass.data[DOMAIN].pop("coordinator")
    return unload_ok
