from dissect.regf import regf


def test_regf(system_hive):
    hive = regf.RegistryHive(system_hive)

    root = hive.root()
    assert len(list(root.subkeys())) == 17
    assert hive.open("Software") is root.subkey("Software") is root.subkey("software")

    lsa = hive.open("ControlSet001\\Control\\LSA")
    assert lsa.subkey("JD").class_name == "cdebfed5"
    assert lsa.subkey("Skew1").class_name == "7db4e11c"
    assert lsa.subkey("GBG").class_name == "b185f3f2"
    assert lsa.subkey("Data").class_name == "a282942c"

    assert hive.open("ControlSet001\\Services\\Tcpip\\Parameters\\DNSRegisteredAdapters").class_name == "DynDRootClass"


def test_named_key_path_is_relative_to_root(system_hive):
    hive = regf.RegistryHive(system_hive)

    # The root key (the hive's HiveEntry) anchors every path. Its own path is
    # empty and its internal name must never be prepended to child paths.
    root = hive.root()
    assert root.path == ""
    assert root.name not in hive.open("ControlSet001").path

    # First level subkey: path relative to the root, no internal root name.
    first = hive.open("ControlSet001")
    assert first.path == "ControlSet001"

    # Second level subkey: the full root-relative path is preserved.
    second = hive.open("ControlSet001\\Control")
    assert second.path == "ControlSet001\\Control"


def test_named_key_path_roundtrips_through_open(system_hive):
    hive = regf.RegistryHive(system_hive)

    for key_path in (
        "ControlSet001",
        "ControlSet001\\Control",
        "ControlSet001\\Control\\LSA",
        "ControlSet001\\Services\\Tcpip\\Parameters",
    ):
        key = hive.open(key_path)
        # The reported path must be resolvable back to the exact same key.
        assert key.path == key_path
        assert hive.open(key.path) is key


def test_named_key_path_under_virtual_mount_point(system_hive):
    hive = regf.RegistryHive(system_hive)
    virtual_mount_point = "HKEY_LOCAL_MACHINE\\SYSTEM"

    def mount(key):
        # Simulate mounting a hive under another (virtual) root key, as a
        # forensics report does when composing multiple hives into one tree.
        return "\\".join([virtual_mount_point, key.path]).rstrip("\\")

    root = hive.root()
    first = hive.open("ControlSet001")
    second = hive.open("ControlSet001\\Control")

    assert mount(root) == virtual_mount_point
    assert mount(first) == f"{virtual_mount_point}\\ControlSet001"
    assert mount(second) == f"{virtual_mount_point}\\ControlSet001\\Control"

    # The root's internal name must not leak into any mounted display path.
    assert root.name not in mount(second)

    # The same root-relative path must relocate the key after the mount prefix
    # is stripped again.
    mounted_path = mount(second)
    relative_path = mounted_path[len(virtual_mount_point) + 1 :]
    assert relative_path == second.path
    assert hive.open(relative_path) is second


def test_named_key_parent_child_relationship(system_hive):
    hive = regf.RegistryHive(system_hive)

    root = hive.root()
    first = hive.open("ControlSet001")
    second = hive.open("ControlSet001\\Control")

    # Parent/child relationships of non-root nodes are retained and can be
    # walked back up to the root key without ever including its internal name.
    assert root.subkey("ControlSet001") is first
    assert first.subkey("Control") is second
    assert hive.cell(first.nk.parent_key_offset) is root
    assert hive.cell(second.nk.parent_key_offset) is first

    # Walking the parent chain of a non-root node terminates at the root key.
    node = second
    while not node.nk.flags.HiveEntry:
        node = hive.cell(node.nk.parent_key_offset)
    assert node is root
