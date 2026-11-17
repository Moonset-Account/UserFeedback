# This file is part of CycloneDX Python Lib
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) OWASP Foundation. All Rights Reserved.


import json
from unittest import TestCase
from unittest.mock import Mock

from cyclonedx.model import ExternalReferenceType, HashAlgorithm

from cyclonedx_py._internal.utils.pep610 import (
    PackageSourceArchive,
    PackageSourceLocal,
    PackageSourceVcs,
    packagesource2extref,
    packagesource4dist,
)


def _dist_with_direct_url(data: dict) -> Mock:
    dist = Mock()
    dist.read_text = Mock(return_value=json.dumps(data))
    return dist


class TestPackageSourceArchive(TestCase):
    """
    Regression tests for PEP 610 archive source parsing,
    based on ``direct_url.json`` files found in locally installed packages.
    """

    def test_hashes_map_is_preferred_over_legacy_hash(self) -> None:
        # local install metadata #1: modern multi-hash map,
        # accompanied by a stale/invalid legacy `hash` field
        dist = _dist_with_direct_url({
            'url': 'https://example.com/packages/somepkg-1.0.tar.gz',
            'subdirectory': 'src',
            'archive_info': {
                'hashes': {
                    'sha256': 'a' * 64,
                    'sha512': 'b' * 128,
                },
                'hash': 'this-is-not-a-valid-legacy-hash',
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceArchive)
        assert isinstance(src, PackageSourceArchive)
        self.assertEqual(src.url, 'https://example.com/packages/somepkg-1.0.tar.gz')
        self.assertEqual(src.subdirectory, 'src')
        self.assertEqual(src.hashes, {'sha256': 'a' * 64, 'sha512': 'b' * 128})
        extref = packagesource2extref(src)
        self.assertIsNotNone(extref)
        assert extref is not None
        self.assertEqual(extref.type, ExternalReferenceType.DISTRIBUTION)
        self.assertEqual(str(extref.url), 'https://example.com/packages/somepkg-1.0.tar.gz')
        self.assertEqual(
            {(h.alg, h.content) for h in extref.hashes},
            {(HashAlgorithm.SHA_256, 'a' * 64), (HashAlgorithm.SHA_512, 'b' * 128)})

    def test_valid_legacy_hash_is_converted(self) -> None:
        dist = _dist_with_direct_url({
            'url': 'https://example.com/packages/somepkg-1.0.tar.gz',
            'archive_info': {
                'hash': 'sha256=' + 'c' * 64,
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceArchive)
        assert isinstance(src, PackageSourceArchive)
        self.assertEqual(src.hashes, {'sha256': 'c' * 64})
        extref = packagesource2extref(src)
        self.assertIsNotNone(extref)
        assert extref is not None
        self.assertEqual(
            [(h.alg, h.content) for h in extref.hashes],
            [(HashAlgorithm.SHA_256, 'c' * 64)])

    def test_invalid_legacy_hash_is_dropped_but_source_is_kept(self) -> None:
        # local install metadata #2: leftover invalid legacy `hash` field
        # (no "<algorithm>=<value>" structure) -- must not abort processing
        dist = _dist_with_direct_url({
            'url': 'https://example.com/packages/otherpkg-2.0.zip',
            'subdirectory': 'pkg',
            'archive_info': {
                'hash': 'sha256',
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceArchive)
        assert isinstance(src, PackageSourceArchive)
        self.assertEqual(src.url, 'https://example.com/packages/otherpkg-2.0.zip')
        self.assertEqual(src.subdirectory, 'pkg')
        self.assertEqual(src.hashes, {})
        extref = packagesource2extref(src)
        self.assertIsNotNone(extref)
        assert extref is not None
        self.assertEqual(extref.type, ExternalReferenceType.DISTRIBUTION)
        self.assertEqual(str(extref.url), 'https://example.com/packages/otherpkg-2.0.zip')
        self.assertEqual(list(extref.hashes), [])
        self.assertIn("subdirectory 'pkg'", extref.comment)

    def test_empty_legacy_hash_value_is_dropped(self) -> None:
        dist = _dist_with_direct_url({
            'url': 'https://example.com/packages/otherpkg-2.0.zip',
            'archive_info': {
                'hash': 'sha256=',
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceArchive)
        assert isinstance(src, PackageSourceArchive)
        self.assertEqual(src.hashes, {})

    def test_no_hash_info_at_all(self) -> None:
        dist = _dist_with_direct_url({
            'url': 'https://example.com/packages/otherpkg-2.0.zip',
            'archive_info': {},
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceArchive)
        assert isinstance(src, PackageSourceArchive)
        self.assertEqual(src.hashes, {})


class TestPackageSourceUnchangedBehaviors(TestCase):
    """
    Guard tests: VCS and local-directory sources must behave as before.
    """

    def test_vcs_source(self) -> None:
        dist = _dist_with_direct_url({
            'url': 'https://github.com/pypa/packaging.git',
            'subdirectory': 'lib',
            'vcs_info': {
                'vcs': 'git',
                'requested_revision': 'main',
                'commit_id': 'deadbee',
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceVcs)
        assert isinstance(src, PackageSourceVcs)
        self.assertEqual(src.vcs, 'git')
        self.assertEqual(src.requested_revision, 'main')
        self.assertEqual(src.commit_id, 'deadbee')
        extref = packagesource2extref(src)
        self.assertIsNotNone(extref)
        assert extref is not None
        self.assertEqual(extref.type, ExternalReferenceType.VCS)
        self.assertEqual(str(extref.url), 'https://github.com/pypa/packaging.git#deadbee')
        self.assertIn("subdirectory 'lib'", extref.comment)

    def test_local_dir_source(self) -> None:
        dist = _dist_with_direct_url({
            'url': 'file:///home/user/projects/somepkg',
            'dir_info': {
                'editable': True,
            },
        })
        src = packagesource4dist(dist)
        self.assertIsInstance(src, PackageSourceLocal)
        assert isinstance(src, PackageSourceLocal)
        self.assertTrue(src.editable)
        extref = packagesource2extref(src)
        self.assertIsNotNone(extref)
        assert extref is not None
        self.assertEqual(extref.type, ExternalReferenceType.DISTRIBUTION)
        self.assertEqual(str(extref.url), 'file:///home/user/projects/somepkg')

    def test_no_direct_url(self) -> None:
        dist = Mock()
        dist.read_text = Mock(return_value=None)
        self.assertIsNone(packagesource4dist(dist))
