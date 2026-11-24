"""Registration rules: source de-duplication and copy consistency."""

from __future__ import annotations

import unittest

from _fixtures import DEVICE_A, DEVICE_B, SEG, image, library, read, unreadable

from forensic_segments import (
    ImageLibrary,
    UnknownSourceError,
    ValidationError,
)


class RegistrationTests(unittest.TestCase):
    def test_readd_identical_image_is_noop(self):
        img = image("a.img", [read(0, "d0")])
        lib = ImageLibrary()
        lib.add_image(img)
        lib.add_image(img)
        self.assertEqual(lib.image_ids, ("a.img",))

    def test_readd_different_content_rejected(self):
        lib = library(image("a.img", [read(0, "d0")]))
        with self.assertRaises(ValidationError):
            lib.add_image(image("a.img", [read(0, "d1")]))

    def test_mixed_segment_sizes_rejected(self):
        lib = library(image("a.img", [read(0, "d0")], segment_size=512))
        with self.assertRaises(ValidationError) as ctx:
            lib.add_image(image("b.img", [read(0, "d0")], segment_size=4096))
        self.assertIn("segment size", str(ctx.exception))

    def test_derived_copy_shares_capture_and_content(self):
        parent = image("orig.img", [read(0, "d0"), unreadable(1, 0x00)], capture="CAP1")
        lib = ImageLibrary()
        lib.add_image(parent)
        lib.add_image(
            image(
                "copy.img",
                [read(0, "d0"), unreadable(1, 0x00)],
                capture="CAP1",
                derived_from="orig.img",
            )
        )
        self.assertEqual(lib.capture_ids(), ("CAP1",))
        self.assertEqual(len(lib.images_for_capture("CAP1")), 2)

    def test_derived_copy_with_unknown_parent_rejected(self):
        lib = ImageLibrary()
        with self.assertRaises(UnknownSourceError):
            lib.add_image(
                image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="ghost.img")
            )

    def test_derived_copy_change_is_rejected(self):
        lib = library(image("orig.img", [read(0, "d0")], capture="CAP1"))
        with self.assertRaises(ValidationError) as ctx:
            lib.add_image(
                image(
                    "copy.img",
                    [read(0, "tampered")],
                    capture="CAP1",
                    derived_from="orig.img",
                )
            )
        self.assertIn("segments differ", str(ctx.exception))

    def test_derived_copy_cannot_change_capture_id(self):
        lib = library(image("orig.img", [read(0, "d0")], capture="CAP1"))
        with self.assertRaises(ValidationError):
            lib.add_image(
                image("copy.img", [read(0, "d0")], capture="CAP2", derived_from="orig.img")
            )

    def test_derived_copy_cannot_change_device(self):
        lib = library(image("orig.img", [read(0, "d0")], capture="CAP1", device=DEVICE_A))
        with self.assertRaises(ValidationError):
            lib.add_image(
                image(
                    "copy.img",
                    [read(0, "d0")],
                    capture="CAP1",
                    device=DEVICE_B,
                    derived_from="orig.img",
                )
            )

    def test_unknown_lookups(self):
        lib = library(image("a.img", [read(0, "d0")]))
        with self.assertRaises(UnknownSourceError):
            lib.image("missing.img")
        with self.assertRaises(UnknownSourceError):
            lib.images_for_capture("NOPE")

    def test_capture_ids_are_deduplicated_in_order(self):
        lib = library(
            image("a.img", [read(0, "d0")], capture="CAP1"),
            image("b.img", [read(0, "d0")], capture="CAP2"),
            image("c.img", [read(0, "d0")], capture="CAP1"),
        )
        self.assertEqual(lib.capture_ids(), ("CAP1", "CAP2"))

    def test_device_identities_are_kept_per_image(self):
        lib = library(
            image("a.img", [read(0, "d0")], capture="CAP1", device=DEVICE_A),
            image("b.img", [read(0, "d0")], capture="CAP2", device=DEVICE_B),
            image("c.img", [read(0, "d0")], capture="CAP3", device=DEVICE_A),
        )
        self.assertEqual(lib.device_identities(), (DEVICE_A, DEVICE_B))
        self.assertEqual(
            [img.image_id for img in lib.images_for_device(DEVICE_B)], ["b.img"]
        )
        with self.assertRaises(UnknownSourceError):
            lib.images_for_device("unknown-device")

    def test_report_names_the_device_that_supplied_bytes(self):
        lib = library(
            image("a.img", [read(0, "d0")], capture="CAP1", device=DEVICE_A),
            image("b.img", [read(0, "d0")], capture="CAP2", device=DEVICE_B),
        )
        seg = lib.extract(0, SEG, preferred=["b.img"]).segments[0]
        self.assertEqual(seg.device_identity, DEVICE_B)


if __name__ == "__main__":
    unittest.main()
