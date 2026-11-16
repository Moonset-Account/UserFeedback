from __future__ import annotations

import struct
from typing import TYPE_CHECKING
from unittest.mock import patch

from flow.record.fieldtypes import datetime as dt

from dissect.target.helpers.regutil import VirtualKey, VirtualValue
from dissect.target.plugins.os.windows.regf.usb import UsbPlugin
from dissect.util.ts import wintimestamp

if TYPE_CHECKING:
    from dissect.target.helpers.regutil import VirtualHive
    from dissect.target.target import Target
USB_PROPERTY_GUID = "{83da6326-97a6-4088-9453-a1923f573b29}"


def add_timestamp(
    hive: VirtualHive,
    guid_key: VirtualKey,
    property_name: str,
    filetime: int | None,
    in_version_subkey: bool = False,
) -> None:
    """Add a single USB timestamp property under the USB properties GUID key.

    Args:
        hive: The virtual hive the keys belong to.
        guid_key: The ``{83da6326-...}`` USB properties key to add the property to.
        property_name: The short property key name (e.g. ``"0064"`` or ``"00000064"``).
        filetime: The raw Windows FILETIME value to store, or ``None`` to skip the property.
        in_version_subkey: When ``True`` the value is stored in a ``00000000`` subkey's
            ``Data`` value. Otherwise it is stored directly in the key's ``(Default)`` value.
    """
    if filetime is None:
        return

    property_key = VirtualKey(hive, property_name)
    data = struct.pack("<Q", filetime)
    if in_version_subkey:
        version_key = VirtualKey(hive, "00000000")
        version_key.add_value("Data", VirtualValue(hive, "Data", data))
        property_key.add_subkey("00000000", version_key)
    else:
        property_key.add_value("(Default)", VirtualValue(hive, "(Default)", data))
    guid_key.add_subkey(property_name, property_key)


def build_usb_device(
    hive: VirtualHive,
    usbstor_path: str,
    serial: str,
    friendly_name: str,
    timestamps: dict[str, int | None],
    *,
    in_version_subkey: bool = False,
    include_properties: bool = True,
) -> None:
    """Create a virtual USBSTOR device key, optionally with a timestamp properties tree."""
    serial_key = VirtualKey(hive, serial)
    serial_key.add_value(
        "ContainerID", VirtualValue(hive, "ContainerID", f"{{container-id-{serial}}}")
    )
    serial_key.add_value("FriendlyName", VirtualValue(hive, "FriendlyName", friendly_name))
    serial_key.add_subkey("Device Parameters", VirtualKey(hive, "Device Parameters"))

    if include_properties:
        properties_key = VirtualKey(hive, "Properties")
        guid_key = VirtualKey(hive, USB_PROPERTY_GUID)
        for property_name, filetime in timestamps.items():
            add_timestamp(hive, guid_key, property_name, filetime, in_version_subkey)
        properties_key.add_subkey(USB_PROPERTY_GUID, guid_key)
        serial_key.add_subkey("Properties", properties_key)

    usbstor_key = hive.make_keys(usbstor_path)
    usbstor_key.add_subkey(serial, serial_key)


def test_windows_usb(target_win_users: Target, hive_hklm: VirtualHive, hive_hku: VirtualHive) -> None:
    """Test discovery of windows usb connection history."""
    usbstor_name = "SYSTEM\\ControlSet001\\Enum\\USBSTOR\\Disk&Ven_SanDisk&Prod_Ultra&Rev_1.00"
    usbstor_key = VirtualKey(hive_hklm, usbstor_name)

    serial_name = "0401399b179e9d46555d8aa9d8618cd0a4f82512595aaea56a74bcd1c387638"
    serial_key = VirtualKey(hive_hklm, serial_name)
    serial_key.add_value(
        "ContainerID", VirtualValue(hive_hklm, "ContainerID", "{3bceaa2e-d325-5328-ba38-d55baacd43d9}")
    )
    serial_key.add_value("FriendlyName", VirtualValue(hive_hklm, "FriendlyName", "SanDisk Ultra USB Device"))
    serial_key.add_subkey("Device Parameters", VirtualKey(hive_hklm, "Device Parameters"))

    properties_key = VirtualKey(hive_hklm, "Properties")
    guid_key = VirtualKey(hive_hklm, "{83da6326-97a6-4088-9453-a1923f573b29}")

    key_0064 = VirtualKey(hive_hklm, "0064")
    key_0064.add_value("(Default)", VirtualValue(hive_hklm, "(Default)", b"\xea\x05+$T\xdf\xda\x01"))

    key_0065 = VirtualKey(hive_hklm, "0065")
    key_0065.add_value("(Default)", VirtualValue(hive_hklm, "(Default)", b"\xea\x05+$T\xdf\xda\x01"))

    key_0066 = VirtualKey(hive_hklm, "0066")
    key_0066.add_value("(Default)", VirtualValue(hive_hklm, "(Default)", b"\xea\x05+$T\xdf\xda\x01"))

    key_0067 = VirtualKey(hive_hklm, "0067")
    key_0067.add_value("(Default)", VirtualValue(hive_hklm, "(Default)", b"\x8b\xe0\x10\x96T\xdf\xda\x01"))

    guid_key.add_subkey("0064", key_0064)
    guid_key.add_subkey("0065", key_0065)
    guid_key.add_subkey("0066", key_0066)
    guid_key.add_subkey("0067", key_0067)
    properties_key.add_subkey("{83da6326-97a6-4088-9453-a1923f573b29}", guid_key)
    serial_key.add_subkey("Properties", properties_key)
    usbstor_key.add_subkey(serial_name, serial_key)
    hive_hklm.map_key(usbstor_name, usbstor_key)

    mounted_devices = VirtualKey(hive_hklm, "SYSTEM\\MountedDevices")
    mounted_devices.add_value(
        "\\DosDevices\\E:", VirtualValue(hive_hklm, "\\DosDevices\\E:", serial_name.encode("utf-16-le"))
    )
    mounted_devices.add_value(
        "\\??\\Volume{4be8862a-4b47-11ef-9a61-70d823df2914}",
        VirtualValue(hive_hklm, "\\??\\Volume{4be8862a-4b47-11ef-9a61-70d823df2914}", serial_name.encode("utf-16-le")),
    )
    hive_hklm.map_key("SYSTEM\\MountedDevices", mounted_devices)

    mounted_volumes_name = "SOFTWARE\\Microsoft\\Windows Portable Devices\\Devices\\SWD#WPDBUSENUM#_??_USBSTOR#DISK&VEN_SANDISK&PROD_ULTRA&REV_1.00#0401399B179E9D46555D8AA9D8618CD0A4F82512595AAEA56A74BCD1C387638#{53F56307-B6BF-11D0-94F2-00A0C91EFB8B}"  # noqa: E501
    mounted_volumes = VirtualKey(hive_hklm, mounted_volumes_name)
    mounted_volumes.add_value("FriendlyName", VirtualValue(hive_hklm, "FriendlyName", "Example USB"))
    hive_hklm.map_key(mounted_volumes_name, mounted_volumes)

    mountpoints2_name = (
        "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Mountpoints2\\{4be8862a-4b47-11ef-9a61-70d823df2914}"
    )
    mountpoints2 = VirtualKey(hive_hku, mountpoints2_name)
    hive_hku.map_key(mountpoints2_name, mountpoints2)

    target_win_users.add_plugin(UsbPlugin)

    with patch("dissect.target.plugins.os.windows.registry.RegistryPlugin.get_user_details") as mock_reg_user_details:
        mock_reg_user_details.return_value = target_win_users.user_details.find(username="John")
        results = list(target_win_users.usb())

    assert len(results) == 1
    assert results[0].type == "Disk"
    assert results[0].serial == "0401399b179e9d46555d8aa9d8618cd0a4f82512595aaea56a74bcd1c387638"
    assert results[0].container_id == "{3bceaa2e-d325-5328-ba38-d55baacd43d9}"
    assert results[0].vendor == "SanDisk"
    assert results[0].product == "Ultra"
    assert results[0].revision == "1.00"
    assert results[0].friendly_name == "SanDisk Ultra USB Device"
    assert results[0].first_insert == dt("2024-07-26 12:05:43.789719+00:00")
    assert results[0].first_install == dt("2024-07-26 12:05:43.789719+00:00")
    assert results[0].last_insert == dt("2024-07-26 12:05:43.789719+00:00")
    assert results[0].last_removal == dt("2024-07-26 12:08:54.878632+00:00")
    assert results[0].volumes == ["Example USB"]
    assert results[0].mounts == ["E:", "\\??\\Volume{4be8862a-4b47-11ef-9a61-70d823df2914}"]
    assert results[0].users == ["John"]
    assert results[0].source == "HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USBSTOR"


def test_windows_usb_missing_removal_and_storage_variants(
    target_win_users: Target, hive_hklm: VirtualHive
) -> None:
    """Devices with incomplete timestamps must still be exported with the missing values left empty.

    Two devices are placed in the same virtual registry:

        - A device whose timestamps live in the ``00000000`` version subkeys and which never
          recorded a last removal time. Before the fix the broken subkey detection made the
          plugin lose every timestamp (and could abort the whole export).
        - A fully documented device whose timestamps are stored directly in the ``(Default)``
          value of each property key, including a last removal time.

    Both devices must be exported independently, each keeping its own accurate timestamps while
    the missing last removal of the first device stays ``None``.
    """
    # Raw FILETIME values (100ns ticks since 1601-01-01). Every value is unique so that a mix-up
    # between devices or properties is detected immediately.
    subkey_times = {
        "0064": 133665315430000000,  # first_install
        "0065": 133665315440000000,  # first_insert
        "0066": 133665315450000000,  # last_insert
        # 0067 (last_removal) deliberately absent: the device was never pulled out.
    }
    direct_times = {
        "00000064": 133665400000000000,  # first_install (long property name variant)
        "00000065": 133665400010000000,  # first_insert
        "00000066": 133665400020000000,  # last_insert
        "00000067": 133665400030000000,  # last_removal
    }

    build_usb_device(
        hive_hklm,
        "SYSTEM\\ControlSet001\\Enum\\USBSTOR\\Disk&Ven_Kingston&Prod_DataTraveler&Rev_1.00",
        "serial-kingston-no-removal",
        "Kingston DataTraveler USB Device",
        subkey_times,
        in_version_subkey=True,
    )
    build_usb_device(
        hive_hklm,
        "SYSTEM\\ControlSet001\\Enum\\USBSTOR\\Disk&Ven_SanDisk&Prod_Cruzer&Rev_2.00",
        "serial-sandisk-complete",
        "SanDisk Cruzer USB Device",
        direct_times,
        in_version_subkey=False,
    )

    target_win_users.add_plugin(UsbPlugin)
    results = {record.serial: record for record in target_win_users.usb()}

    assert set(results) == {"serial-kingston-no-removal", "serial-sandisk-complete"}

    no_removal = results["serial-kingston-no-removal"]
    assert no_removal.vendor == "Kingston"
    assert no_removal.product == "DataTraveler"
    assert no_removal.revision == "1.00"
    assert no_removal.friendly_name == "Kingston DataTraveler USB Device"
    # Timestamps stored in the 00000000 version subkeys are still parsed accurately.
    assert no_removal.first_install == wintimestamp(subkey_times["0064"])
    assert no_removal.first_insert == wintimestamp(subkey_times["0065"])
    assert no_removal.last_insert == wintimestamp(subkey_times["0066"])
    # The absent removal time is exported as an empty value, not dropped with the record.
    assert no_removal.last_removal is None

    complete = results["serial-sandisk-complete"]
    assert complete.vendor == "SanDisk"
    assert complete.product == "Cruzer"
    assert complete.revision == "2.00"
    assert complete.friendly_name == "SanDisk Cruzer USB Device"
    # Timestamps stored directly in the (Default) value are parsed independently and accurately.
    assert complete.first_install == wintimestamp(direct_times["00000064"])
    assert complete.first_insert == wintimestamp(direct_times["00000065"])
    assert complete.last_insert == wintimestamp(direct_times["00000066"])
    assert complete.last_removal == wintimestamp(direct_times["00000067"])

    # The two records must not share or leak each other's timestamps.
    assert no_removal.first_insert != complete.first_insert
    assert no_removal.last_insert != complete.last_insert


def test_windows_usb_device_without_properties(
    target_win_users: Target, hive_hklm: VirtualHive
) -> None:
    """A device without a Properties tree is still exported with every timestamp empty."""
    build_usb_device(
        hive_hklm,
        "SYSTEM\\ControlSet001\\Enum\\USBSTOR\\Disk&Ven_Generic&Prod_Flash&Rev_0.01",
        "serial-generic-no-properties",
        "Generic Flash USB Device",
        timestamps={},
        include_properties=False,
    )

    target_win_users.add_plugin(UsbPlugin)
    results = list(target_win_users.usb())

    assert len(results) == 1
    record = results[0]
    assert record.serial == "serial-generic-no-properties"
    assert record.friendly_name == "Generic Flash USB Device"
    assert record.first_install is None
    assert record.first_insert is None
    assert record.last_insert is None
    assert record.last_removal is None
