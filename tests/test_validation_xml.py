# This file is part of CycloneDX Python Library
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

from collections.abc import Generator
from glob import iglob
from itertools import chain
from os.path import basename, dirname, join
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest import TestCase

from ddt import ddt, idata, unpack

from cyclonedx.exception import MissingOptionalDependencyException
from cyclonedx.schema import OutputFormat, SchemaVersion
from cyclonedx.schema import _res as schema_res
from cyclonedx.validation.xml import XmlValidator
from tests import OWN_DATA_DIRECTORY, SCHEMA_TESTDATA_DIRECTORY, DpTuple

UNSUPPORTED_SCHEMA_VERSIONS = set()


def _dp_sv_tf(valid: bool) -> Generator:
    prefix = 'valid-' if valid else 'invalid-'
    return (
        DpTuple((sv, tf))
        for sv in SchemaVersion if sv not in UNSUPPORTED_SCHEMA_VERSIONS
        for tf in iglob(join(SCHEMA_TESTDATA_DIRECTORY, sv.to_version(), f'{prefix}*.xml'))
    )


def _dp_sv_own(valid: bool) -> Generator:
    return (
        DpTuple((sv, tf))
        for sv in SchemaVersion if sv not in UNSUPPORTED_SCHEMA_VERSIONS
        for tf in iglob(join(OWN_DATA_DIRECTORY, 'xml', sv.to_version(), '*.xml')) if ('invalid-' in tf) != valid
    )


@ddt
class TestXmlValidator(TestCase):

    @idata(sv for sv in SchemaVersion if sv not in UNSUPPORTED_SCHEMA_VERSIONS)
    def test_validator_as_expected(self, schema_version: SchemaVersion) -> None:
        validator = XmlValidator(schema_version)
        self.assertIs(validator.schema_version, schema_version)
        self.assertIs(validator.output_format, OutputFormat.XML)

    @idata(UNSUPPORTED_SCHEMA_VERSIONS)
    def test_throws_with_unsupported_schema_version(self, schema_version: SchemaVersion) -> None:
        with self.assertRaisesRegex(ValueError, f'unsupported schema_version: {schema_version}'):
            XmlValidator(schema_version)

    @idata(chain(
        _dp_sv_tf(True),
        _dp_sv_own(True)
    ))
    @unpack
    def test_validate_no_none(self, schema_version: SchemaVersion, test_data_file: str) -> None:
        validator = XmlValidator(schema_version)
        with open(join(test_data_file)) as tdfh:
            test_data = tdfh.read()
        try:
            validation_error = validator.validate_str(test_data)
        except MissingOptionalDependencyException:
            self.skipTest('MissingOptionalDependencyException')
        self.assertIsNone(validation_error)

    @idata(chain(
        _dp_sv_tf(False),
        _dp_sv_own(False)
    ))
    @unpack
    def test_validate_expected_error_one(self, schema_version: SchemaVersion, test_data_file: str) -> None:
        validator = XmlValidator(schema_version)
        with open(join(test_data_file)) as tdfh:
            test_data = tdfh.read()
        try:
            validation_error = validator.validate_str(test_data)
        except MissingOptionalDependencyException:
            self.skipTest('MissingOptionalDependencyException')
        self.assertIsNotNone(validation_error)
        self.assertIsNotNone(validation_error.data)

    @idata(chain(
        _dp_sv_tf(False),
        _dp_sv_own(False)
    ))
    @unpack
    def test_validate_expected_error_iterator(self, schema_version: SchemaVersion, test_data_file: str) -> None:
        validator = XmlValidator(schema_version)
        with open(join(test_data_file)) as tdfh:
            test_data = tdfh.read()
        try:
            validation_errors = validator.validate_str(test_data, all_errors=True)
        except MissingOptionalDependencyException:
            self.skipTest('MissingOptionalDependencyException')
        self.assertIsNotNone(validation_errors)
        validation_errors = tuple(validation_errors)
        self.assertGreater(len(validation_errors), 0)
        for validation_error in validation_errors:
            self.assertIsNotNone(validation_error.data)


@ddt
class TestXmlValidatorRelocatedSchema(TestCase):
    """The bundled schemas may be located in a directory whose name contains
    URI-significant characters such as `#` and spaces.

    lxml/libxml2 interprets ``XMLSchema(file=...)`` as a URI rather than a
    plain filesystem path. The local main schema and its relative sibling
    references (e.g. the ``spdx.SNAPSHOT.xsd`` imported via
    ``schemaLocation``) therefore have to be loaded through a proper
    ``file://`` URI, so validation keeps working regardless of the path.
    """

    @idata(sv for sv in SchemaVersion if sv not in UNSUPPORTED_SCHEMA_VERSIONS)
    def test_schema_loads_from_plain_and_special_paths(self, schema_version: SchemaVersion) -> None:
        try:
            from lxml.etree import XMLSchema  # noqa: F401
        except ImportError:
            self.skipTest('lxml is not installed')
        version = schema_version.to_version()
        valid_files = sorted(iglob(join(SCHEMA_TESTDATA_DIRECTORY, version, 'valid-*.xml')))
        invalid_files = sorted(iglob(join(SCHEMA_TESTDATA_DIRECTORY, version, 'invalid-*.xml')))
        if not valid_files:
            self.skipTest(f'no valid xml test data for schema version {version}')
        with open(valid_files[0], encoding='utf-8') as fh:
            valid_bom = fh.read()
        invalid_bom = None
        if invalid_files:
            with open(invalid_files[0], encoding='utf-8') as fh:
                invalid_bom = fh.read()
        original_schema_file = schema_res.BOM_XML[schema_version]

        # the second name contains a hash sign AND spaces, which break raw
        # URI parsing in libxml2
        for location in ('plain-directory', 'release #1 (final copy)'):
            with self.subTest(location=location, schema_version=version), \
                    TemporaryDirectory(prefix='cyclonedx-relocated-') as tmp:
                res_dir = join(tmp, location, '_res')
                # copy the whole resource directory, so the main schema and
                # its relative sibling references are relocated together
                copytree(dirname(original_schema_file), res_dir)
                relocated_schema_file = join(res_dir, basename(original_schema_file))

                # point the validator's schema lookup at the relocated copy
                schema_res.BOM_XML[schema_version] = relocated_schema_file
                try:
                    validator = XmlValidator(schema_version)
                    self.assertEqual(validator._schema_file, relocated_schema_file)

                    # legal BOM: must validate without error, proving the main
                    # schema and its relative sibling references loaded
                    self.assertIsNone(validator.validate_str(valid_bom))

                    # structurally illegal BOM: must report a normal validation
                    # error instead of raising a schema-loading exception
                    if invalid_bom is not None:
                        error = validator.validate_str(invalid_bom)
                        self.assertIsNotNone(error)
                        self.assertIsNotNone(error.data)
                finally:
                    schema_res.BOM_XML[schema_version] = original_schema_file
