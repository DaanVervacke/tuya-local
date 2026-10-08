"""Tests for the schedule helper sync."""

import base64
from datetime import time as time_of_day
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.tuya_local.const import CONF_SCHEDULE_ENTITY
from custom_components.tuya_local.helpers.schedule_sync import (
    RECORD_BYTES,
    UNSET,
    ScheduleSync,
    decode_schedule,
    encode_schedule,
)

EMPTY_RANGES = {
    day: []
    for day in [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
}


def make_sync(activity="STANDBY", helper="schedule.test"):
    device = Mock()
    device.get_property = Mock(return_value=activity)
    device.async_set_properties = AsyncMock()
    entry = Mock()
    entry.options = {CONF_SCHEDULE_ENTITY: helper} if helper else {}
    hass = Mock()
    hass.states.get = Mock(return_value=object())
    sync = ScheduleSync(hass, entry, device)
    return sync, device


@pytest.mark.asyncio
async def test_encode_empty_schedule():
    raw, problems, has_windows = encode_schedule(EMPTY_RANGES)
    assert problems == []
    assert has_windows is False
    assert len(raw) == 70
    assert all(raw[day * 5] == day for day in range(7))
    assert raw[1] == UNSET
    assert raw[36] == UNSET


@pytest.mark.asyncio
async def test_encode_single_window():
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    raw, problems, has_windows = encode_schedule(ranges)
    assert problems == []
    assert has_windows is True
    record = raw[0:RECORD_BYTES]
    assert record == bytes([0, 8, 30, 10, 0])
    assert raw[35 + 1] == UNSET


@pytest.mark.asyncio
async def test_encode_two_windows():
    ranges = {
        **EMPTY_RANGES,
        "monday": [
            {"from": "18:00", "to": "20:00"},
            {"from": "08:00", "to": "09:30"},
        ],
    }
    raw, problems, has_windows = encode_schedule(ranges)
    assert problems == []
    assert has_windows is True
    assert raw[0:RECORD_BYTES] == bytes([0, 8, 0, 9, 30])
    assert raw[35 : 35 + RECORD_BYTES] == bytes([0, 18, 0, 20, 0])


@pytest.mark.asyncio
async def test_encode_end_of_day():
    ranges = {**EMPTY_RANGES, "friday": [{"from": "22:00", "to": "24:00"}]}
    raw, _, _ = encode_schedule(ranges)
    record = raw[4 * RECORD_BYTES : 5 * RECORD_BYTES]
    assert record == bytes([4, 22, 0, 23, 59])


@pytest.mark.asyncio
async def test_encode_time_objects():
    ranges = {
        **EMPTY_RANGES,
        "sunday": [{"from": time_of_day(9, 15), "to": time_of_day(11, 45)}],
    }
    raw, _, _ = encode_schedule(ranges)
    record = raw[6 * RECORD_BYTES : 7 * RECORD_BYTES]
    assert record == bytes([6, 9, 15, 11, 45])


@pytest.mark.asyncio
async def test_encode_too_many_windows_reports_problem():
    ranges = {
        **EMPTY_RANGES,
        "tuesday": [
            {"from": "08:00", "to": "09:00"},
            {"from": "12:00", "to": "13:00"},
            {"from": "18:00", "to": "19:00"},
        ],
    }
    raw, problems, has_windows = encode_schedule(ranges)
    assert problems == ["tuesday has 3 windows, the mower supports 2"]
    assert has_windows is True
    assert raw[5 : 5 + RECORD_BYTES] == bytes([1, 8, 0, 9, 0])
    assert raw[40 : 40 + RECORD_BYTES] == bytes([1, 12, 0, 13, 0])


@pytest.mark.asyncio
async def test_decode_roundtrip():
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    raw, _, _ = encode_schedule(ranges)
    blob = base64.b64encode(raw).decode()
    days = decode_schedule(blob)
    assert days["monday"] == ["08:30-10:00"]
    assert days["tuesday"] == []


@pytest.mark.asyncio
async def test_decode_invalid_blob():
    assert decode_schedule("not base64!!") is None
    assert decode_schedule("AAAA") is None


@pytest.mark.asyncio
async def test_reconcile_writes_while_docked():
    sync, device = make_sync(activity="STANDBY")
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.async_set_properties.assert_awaited_once()
    dps = device.async_set_properties.await_args.args[0]
    assert list(dps.keys()) == [110]
    days = decode_schedule(dps[110])
    assert days["monday"] == ["08:30-10:00"]


@pytest.mark.asyncio
async def test_reconcile_defers_while_mowing():
    sync, device = make_sync(activity="MOWING")
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.async_set_properties.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_writes_after_docking():
    sync, device = make_sync(activity="MOWING")
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.get_property = Mock(return_value="CHARGING")
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.async_set_properties.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_empty_helper():
    sync, device = make_sync()
    with patch.object(
        sync, "_async_fetch_ranges", AsyncMock(return_value=EMPTY_RANGES)
    ):
        await sync.async_reconcile()
    device.async_set_properties.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_clears_after_windows_removed():
    sync, device = make_sync()
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.async_set_properties.reset_mock()
    with patch.object(
        sync, "_async_fetch_ranges", AsyncMock(return_value=EMPTY_RANGES)
    ):
        await sync.async_reconcile()
    device.async_set_properties.assert_awaited_once()
    dps = device.async_set_properties.await_args.args[0]
    days = decode_schedule(dps[110])
    assert all(windows == [] for windows in days.values())


@pytest.mark.asyncio
async def test_reconcile_skips_unchanged_schedule():
    sync, device = make_sync()
    ranges = {**EMPTY_RANGES, "monday": [{"from": "08:30", "to": "10:00"}]}
    fetch = AsyncMock(return_value=ranges)
    with patch.object(sync, "_async_fetch_ranges", fetch):
        await sync.async_reconcile()
        await sync.async_reconcile()
    device.async_set_properties.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_too_many_windows():
    sync, device = make_sync()
    ranges = {
        **EMPTY_RANGES,
        "tuesday": [
            {"from": "08:00", "to": "09:00"},
            {"from": "12:00", "to": "13:00"},
            {"from": "18:00", "to": "19:00"},
        ],
    }
    with patch.object(sync, "_async_fetch_ranges", AsyncMock(return_value=ranges)):
        await sync.async_reconcile()
    device.async_set_properties.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_without_helper_registers_nothing(hass):
    sync, _ = make_sync(helper=None)
    sync._hass = hass
    await sync.async_setup()
    assert sync._unsubs == []


@pytest.mark.asyncio
async def test_setup_with_helper_registers_listeners(hass):
    hass.states.async_set("schedule.test", "off")
    sync, _ = make_sync()
    sync._hass = hass
    await sync.async_setup()
    assert len(sync._unsubs) == 2
    sync.async_shutdown()
    assert sync._unsubs == []
