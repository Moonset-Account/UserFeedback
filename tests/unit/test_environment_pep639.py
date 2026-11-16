# This file is part of CycloneDX Python
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


from email.message import Message
from logging import getLogger
from typing import Dict, Iterable, Optional, Union
from unittest import TestCase, skipIf
from unittest.mock import patch

from cyclonedx.model.bom import Bom
from cyclonedx.model.component import Component, ComponentType
from cyclonedx.model.license import DisjunctiveLicense, LicenseExpression
from cyclonedx.output import make_outputter
from cyclonedx.schema import OutputFormat, SchemaVersion

from cyclonedx_py._internal.environment import EnvironmentBB
from cyclonedx_py._internal.utils.pep639 import dist2licenses
from tests import SUPPORTED_OF_SV

try:
    from cyclonedx.validation import make_schemabased_validator
except ImportError:  # pragma: no cover
    make_schemabased_validator = None  # type:ignore[assignment]


def _metadata(fields: Dict[str, Union[str, Iterable[str]]]) -> Message:
    metadata = Message()
    for key, values in fields.items():
        if isinstance(values, str):
            values = (values,)
        for value in values:
            metadata[key] = value
    return metadata


class _FakeDist:
    """Minimal stand-in for `importlib.metadata.Distribution`."""

    def __init__(self, metadata: Message,
                 license_files: Optional[Dict[str, str]] = None) -> None:
        self.metadata = metadata
        self._license_files = license_files or {}
        self.requires = None

    def read_text(self, path: str) -> Optional[str]:
        # license files may live in dedicated subdirectories
        for prefix in ('licenses/', 'license_files/', ''):
            if path.startswith(prefix):
                content = self._license_files.get(path[len(prefix):])
                if content is not None:
                    return content
        return None


def _build_bom(dists: Iterable[_FakeDist],
               gather_license_texts: bool = True) -> Bom:
    builder = EnvironmentBB(
        logger=getLogger('cyclonedx-py.tests'),
        pep639=True,
        gather_license_texts=gather_license_texts)
    with patch('cyclonedx_py._internal.environment.distributions',
               lambda **__: iter(dists)):
        return builder(
            python=None,
            pyproject_file=None,
            mc_type=ComponentType.APPLICATION)


def _the_only_component(bom: Bom) -> Component:
    components = tuple(bom.components)
    if len(components) != 1:  # pragma: no cover
        raise ValueError('expected exactly one component')
    return components[0]


def _dist_with_expression_and_license_file() -> _FakeDist:
    return _FakeDist(
        _metadata({
            'Name': 'foo',
            'Version': '1.0',
            'License-Expression': 'MIT OR Apache-2.0',
            'License-File': 'LICENSE',
        }),
        license_files={'LICENSE': 'some license text'})


class TestEnvironmentPep639Licenses(TestCase):

    def test_expression_and_license_file(self) -> None:
        bom = _build_bom([_dist_with_expression_and_license_file()])
        component = _the_only_component(bom)
        licenses = tuple(component.licenses)
        # the main license field holds the declared expression, and nothing else
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], LicenseExpression)
        self.assertEqual('MIT OR Apache-2.0', licenses[0].value)
        # the license file is kept as evidence, source and content included
        evidence = component.evidence
        self.assertIsNotNone(evidence)
        evidence_licenses = tuple(evidence.licenses)
        self.assertEqual(1, len(evidence_licenses))
        evidence_license = evidence_licenses[0]
        self.assertIsInstance(evidence_license, DisjunctiveLicense)
        self.assertEqual('declared license file: LICENSE', evidence_license.name)
        self.assertEqual('some license text', evidence_license.text.content)

    def test_license_file_only(self) -> None:
        bom = _build_bom([_FakeDist(
            _metadata({
                'Name': 'foo',
                'Version': '1.0',
                'License-File': 'LICENSE',
            }),
            license_files={'LICENSE': 'some license text'})])
        component = _the_only_component(bom)
        # without an expression, license files stay in the main license field
        licenses = tuple(component.licenses)
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], DisjunctiveLicense)
        self.assertEqual('declared license file: LICENSE', licenses[0].name)
        self.assertIsNone(component.evidence)

    def test_expression_only(self) -> None:
        bom = _build_bom([_FakeDist(
            _metadata({
                'Name': 'foo',
                'Version': '1.0',
                'License-Expression': 'MIT OR Apache-2.0',
            }))])
        component = _the_only_component(bom)
        licenses = tuple(component.licenses)
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], LicenseExpression)
        self.assertIsNone(component.evidence)

    def test_legacy_license_only(self) -> None:
        bom = _build_bom([_FakeDist(
            _metadata({
                'Name': 'foo',
                'Version': '1.0',
                'License': 'MIT',
            }))])
        component = _the_only_component(bom)
        licenses = tuple(component.licenses)
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], DisjunctiveLicense)
        self.assertEqual('MIT', licenses[0].id)
        self.assertIsNone(component.evidence)

    def test_no_license_files_gathered_when_disabled(self) -> None:
        bom = _build_bom([_dist_with_expression_and_license_file()],
                         gather_license_texts=False)
        component = _the_only_component(bom)
        licenses = tuple(component.licenses)
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], LicenseExpression)
        self.assertIsNone(component.evidence)

    @skipIf(make_schemabased_validator is None, 'requires extra "validation"')
    def test_expression_and_license_file_is_schema_conform(self) -> None:
        bom = _build_bom([_dist_with_expression_and_license_file()])
        of: OutputFormat
        sv: SchemaVersion
        for of, sv in SUPPORTED_OF_SV:
            with self.subTest(of=of.name, sv=sv.to_version()):
                output = make_outputter(bom, of, sv).output_as_string(indent=2)
                validation_error = make_schemabased_validator(of, sv).validate_str(output)
                self.assertIsNone(validation_error,
                                  getattr(validation_error, 'data', None))


class TestEnvironmentPep639Utils(TestCase):

    def test_dist2licenses_still_yields_all_sources(self) -> None:
        dist = _dist_with_expression_and_license_file()
        licenses = tuple(dist2licenses(dist, True, getLogger('cyclonedx-py.tests')))
        self.assertEqual(2, len(licenses))
        self.assertIsInstance(licenses[0], LicenseExpression)
        self.assertIsInstance(licenses[1], DisjunctiveLicense)

    def test_dist2licenses_without_texts(self) -> None:
        dist = _dist_with_expression_and_license_file()
        licenses = tuple(dist2licenses(dist, False, getLogger('cyclonedx-py.tests')))
        self.assertEqual(1, len(licenses))
        self.assertIsInstance(licenses[0], LicenseExpression)


if __name__ == '__main__':  # pragma: no cover
    import unittest
    unittest.main()
