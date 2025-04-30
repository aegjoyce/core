"""
Climate platform for Roth Touchline using coordinated updates.
Each thermostat entity pulls its data from the shared DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Define preset modes as a mapping from preset name to (op_mode, week_prog)
PRESET_MODES = {
    "Normal": (0, 0),
    "Night": (1, 0),
    "Holiday": (2, 0),
    "Pro 1": (0, 1),
    "Pro 2": (0, 2),
    "Pro 3": (0, 3),
}
# Invert the mapping so we can directly look up a preset name by (op_mode, week_prog)
TOUCHLINE_HA_PRESETS = {v: k for k, v in PRESET_MODES.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: Any, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Touchline climate entities via the coordinator."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    # Create one entity per device.
    entities = [
        TouchlineEntity(coordinator, device_id) for device_id in coordinator.device_ids
    ]
    async_add_entities(entities, True)


class TouchlineEntity(ClimateEntity):
    """Representation of a Touchline thermostat."""

    _attr_hvac_mode = HVACMode.HEAT
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: Any, device_id: int) -> None:
        """Initialize the thermostat entity."""
        self.coordinator = coordinator
        self._device_id = device_id
        self._name = f"Touchline {device_id}"
        self._current_temperature = None
        self._target_temperature = None
        self._preset_mode = None
        # Variables for optimistic updates.
        self._optimistic_target_temperature: float | None = None
        self._optimistic_preset_mode: str | None = None
        self._optimistic_until = 0  # Timestamp until which optimistic value is used

    @property
    def available(self) -> bool:
        """Return True if the coordinator last update was successful."""
        return self.coordinator.last_update_success

    @property
    def name(self) -> str:
        """Return the device name from the coordinator data, if available."""
        data = self.coordinator.data.get(self._device_id, {})
        new_name = data.get(f"G{self._device_id}.name")
        if new_name:
            self._name = new_name
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the thermostat."""
        return f"{self.coordinator.host}-{self._device_id}"

    @property
    def current_temperature(self):
        """Return the current temperature (dividing by 100)."""
        data = self.coordinator.data.get(self._device_id, {})
        val = data.get(f"G{self._device_id}.RaumTemp")
        try:
            return int(val) / 100 if val is not None else self._current_temperature
        except Exception:
            return self._current_temperature

    @property
    def target_temperature(self):
        """Return the target temperature.

        For an optimistic update, if the current time is within the optimistic grace period,
        return the optimistic value; otherwise, return the hub's reported value.
        """
        if (
            time.time() < self._optimistic_until
            and self._optimistic_target_temperature is not None
        ):
            return self._optimistic_target_temperature

        data = self.coordinator.data.get(self._device_id, {})
        val = data.get(f"G{self._device_id}.SollTemp")
        try:
            return int(val) / 100 if val is not None else self._target_temperature
        except Exception:
            return self._target_temperature

    @property
    def preset_mode(self):
        """Return the preset mode.

        For optimistic updates, if within the grace period, return the locally stored value.
        Otherwise, compute it from the hub's reported parameters.
        """
        if (
            time.time() < self._optimistic_until
            and self._optimistic_preset_mode is not None
        ):
            return self._optimistic_preset_mode

        data = self.coordinator.data.get(self._device_id, {})
        op_mode = data.get(f"G{self._device_id}.OPMode")
        week_prog = data.get(f"G{self._device_id}.WeekProg")
        if op_mode is not None and week_prog is not None:
            try:
                op_mode_int = int(op_mode)
                week_prog_int = int(week_prog)
                pr = TOUCHLINE_HA_PRESETS.get((op_mode_int, week_prog_int))
                if pr:
                    self._preset_mode = pr
            except Exception as e:
                _LOGGER.error(
                    "Error computing preset mode for device %s: %s", self._device_id, e
                )
        return self._preset_mode

    @property
    def preset_modes(self):
        """Return a list of available preset mode names."""
        return list(PRESET_MODES.keys())

    def set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature on the device.

        This method creates a new PyTouchline instance, calls update() per docs, and
        sends the temperature command. If successful, it sets an optimistic override for 10 seconds.
        """
        from pytouchline_extended import PyTouchline

        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        _LOGGER.debug(
            "Setting temperature %s on device %s", temperature, self._device_id
        )
        pt = PyTouchline(id=self._device_id, url=self.coordinator.host)
        pt.update()  # Populate internal parameters

        result = pt.set_target_temperature(temperature)
        if result:
            _LOGGER.debug(
                "Successfully set temperature on device %s to %s",
                self._device_id,
                temperature,
            )
            self._target_temperature = temperature
            # Set optimistic override for 10 seconds.
            self._optimistic_target_temperature = temperature
            self._optimistic_until = time.time() + 10
            self.hass.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.coordinator.async_request_refresh())
            )
        else:
            _LOGGER.error("Failed to set temperature on device %s", self._device_id)

    def set_preset_mode(self, preset_mode: str) -> None:
        """Set a new preset mode on the device.

        This method looks up the desired preset from PRESET_MODES, creates a new PyTouchline instance,
        calls update() to load internal parameters, and then sends the commands for week program and op mode.
        If successful, an optimistic override is set for 10 seconds.
        """
        from pytouchline_extended import PyTouchline

        if preset_mode not in PRESET_MODES:
            _LOGGER.error("Unknown preset mode: %s", preset_mode)
            return

        op_mode, week_prog = PRESET_MODES[preset_mode]
        _LOGGER.debug(
            "Setting preset mode %s on device %s (OPMode=%s, WeekProg=%s)",
            preset_mode,
            self._device_id,
            op_mode,
            week_prog,
        )

        pt = PyTouchline(id=self._device_id, url=self.coordinator.host)
        pt.update()  # Populate internal parameters

        result_week = pt.set_week_program(week_prog)
        result_op = pt.set_operation_mode(op_mode)

        _LOGGER.debug(
            "set_week_program result: %s, set_operation_mode result: %s",
            result_week,
            result_op,
        )

        if result_week and result_op:
            _LOGGER.debug(
                "Successfully set preset mode %s on device %s",
                preset_mode,
                self._device_id,
            )
            self._preset_mode = preset_mode
            # Set optimistic override for 10 seconds.
            self._optimistic_preset_mode = preset_mode
            self._optimistic_until = time.time() + 10
            self.hass.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.coordinator.async_request_refresh())
            )
        else:
            _LOGGER.error(
                "Failed to set preset mode for device %s: week_program result %s, op_mode result %s",
                self._device_id,
                result_week,
                result_op,
            )

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (only HEAT supported)."""
        pass

    async def async_update(self) -> None:
        """Force a refresh via the coordinator."""
        await self.coordinator.async_request_refresh()
