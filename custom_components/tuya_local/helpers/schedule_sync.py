from __future__ import annotations

import base64
import logging

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ..const import CONF_SCHEDULE_ENTITY

_LOGGER = logging.getLogger(__name__)

SCHEDULE_DOMAIN = "schedule"
SCHEDULE_DP = 110
ACTIVITY_DP = 101
WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
DOCKED_ACTIVITIES = ["STANDBY", "CHARGING", "CHARGING_WITH_TASK_SUSPEND"]
UNSET = 136
MAX_WINDOWS = 2
RECORD_BYTES = 5
TOTAL_BYTES = 70


def encode_schedule(ranges_by_day):
    """Turn per day time ranges into the mower dp110 byte layout."""
    raw = bytearray([UNSET] * TOTAL_BYTES)
    problems = []
    has_windows = False
    for day_index, day in enumerate(WEEKDAYS):
        raw[day_index * RECORD_BYTES] = day_index
        raw[(day_index + len(WEEKDAYS)) * RECORD_BYTES] = day_index
        windows = []
        for entry in ranges_by_day.get(day, []):
            start_h, start_m = _split_time(entry["from"])
            end_h, end_m = _split_time(entry["to"])
            windows.append((start_h, start_m, end_h, end_m))
        windows.sort()
        if len(windows) > MAX_WINDOWS:
            problems.append(
                f"{day} has {len(windows)} windows, the mower supports {MAX_WINDOWS}"
            )
            windows = windows[:MAX_WINDOWS]
        for period, numbers in enumerate(windows):
            has_windows = True
            offset = (day_index + period * len(WEEKDAYS)) * RECORD_BYTES
            for extra, number in enumerate(numbers):
                raw[offset + 1 + extra] = number
    return bytes(raw), problems, has_windows


def decode_schedule(blob):
    """Turn a dp110 base64 blob into readable windows per weekday."""
    try:
        decoded = base64.b64decode(blob)
    except ValueError, TypeError:
        return None
    if len(decoded) != TOTAL_BYTES:
        return None
    days = {}
    for day_index, day in enumerate(WEEKDAYS):
        windows = []
        for period in range(MAX_WINDOWS):
            offset = (day_index + period * len(WEEKDAYS)) * RECORD_BYTES
            record = decoded[offset : offset + RECORD_BYTES]
            if record[1] != UNSET and record[3] != UNSET:
                windows.append(
                    f"{record[1]:02d}:{record[2]:02d}-{record[3]:02d}:{record[4]:02d}"
                )
        days[day] = windows
    return days


def _split_time(value):
    if not isinstance(value, str):
        value = value.isoformat()
    parts = value.split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour == 24:
        return 23, 59
    return hour, minute


class ScheduleSync:
    """Sync a Home Assistant schedule helper to the mower schedule dps."""

    def __init__(self, hass: HomeAssistant, entry, device):
        self._hass = hass
        self._device = device
        self._helper = entry.options.get(CONF_SCHEDULE_ENTITY)
        self._last_written = None
        self._last_problems = None
        self._has_ever_had_windows = False
        self._unsubs = []

    async def async_setup(self):
        """Start listening for helper changes if one was configured."""
        if not self._helper:
            return
        self._unsubs.append(
            async_track_state_change_event(
                self._hass, [self._helper], self._helper_changed
            )
        )
        await self._async_sync(raise_when_blocked=False)

    @callback
    def async_shutdown(self):
        """Stop listening."""
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def _helper_changed(self, event: Event):
        self._hass.async_create_task(self._async_sync())

    async def _async_sync(self, raise_when_blocked: bool = True):
        """Send the helper schedule to the mower if it changed.

        The mower only accepts schedule writes while it is docked or
        charging. A write attempted in any other activity raises, and
        nothing retries it: the next helper edit tries again.
        """
        ranges = await self._async_fetch_ranges()
        if ranges is None:
            return
        raw, problems, has_windows = encode_schedule(ranges)
        if problems and problems != self._last_problems:
            _LOGGER.warning(
                "Mower schedule not sent from %s: %s",
                self._helper,
                "; ".join(problems),
            )
        self._last_problems = problems
        if problems:
            return
        if has_windows:
            self._has_ever_had_windows = True
        elif not self._has_ever_had_windows:
            _LOGGER.debug(
                "Not clearing the mower schedule from an empty %s",
                self._helper,
            )
            return
        blob = base64.b64encode(raw).decode()
        if blob == self._last_written:
            return
        activity = self._device.get_property(ACTIVITY_DP)
        if activity not in DOCKED_ACTIVITIES:
            message = (
                f"Cannot sync schedule from {self._helper}: the mower is "
                f"{activity}, it only accepts schedule writes while docked "
                "or charging"
            )
            if raise_when_blocked:
                raise HomeAssistantError(message)
            _LOGGER.warning(message)
            return
        await self._device.async_set_properties({SCHEDULE_DP: blob})
        self._last_written = blob
        _LOGGER.info(
            "Mower schedule updated from %s: %s",
            self._helper,
            decode_schedule(blob),
        )

    async def _async_fetch_ranges(self):
        """Return the helper time ranges keyed by weekday name."""
        state = self._hass.states.get(self._helper)
        if state is None:
            _LOGGER.warning("Schedule helper %s not found", self._helper)
            return None
        try:
            response = await self._hass.services.async_call(
                SCHEDULE_DOMAIN,
                "get_schedule",
                {},
                blocking=True,
                target={CONF_ENTITY_ID: self._helper},
                return_response=True,
            )
        except Exception as e:
            _LOGGER.warning(
                "Could not read schedule from %s: %s",
                self._helper,
                e,
            )
            return None
        if isinstance(response, dict) and self._helper in response:
            return response[self._helper]
        return response
