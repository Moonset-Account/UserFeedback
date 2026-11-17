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


from logging import getLogger
from unittest import TestCase

from cyclonedx.model.component import ComponentType

from cyclonedx_py._internal import PropertyName
from cyclonedx_py._internal.poetry import PoetryBB
from cyclonedx_py._internal.utils.packaging import normalize_packagename
from cyclonedx_py._internal.utils.poetry import poetry2dependencies


def _component_names(bom) -> set:
    return {c.name for c in bom.components}


def _component_versions(bom, name: str) -> set:
    return {c.version for c in bom.components if c.name == name}


def _dep_refs(bom, ref) -> set:
    dep = next(d for d in bom.dependencies if str(d.ref) == str(ref))
    return {str(d.ref) for d in dep.dependencies}


def _extra_props(component) -> set:
    return {
        p.value for p in component.properties
        if p.name == PropertyName.PythonPackageExtra.value
    }


class PoetryBBMultiConstraintTest(TestCase):

    def setUp(self) -> None:
        self.bb = PoetryBB(logger=getLogger('test'))

    @staticmethod
    def _make_project(dependencies: dict, extras: dict = None) -> dict:
        return {
            'tool': {
                'poetry': {
                    'name': 'mc-test',
                    'version': '1.0.0',
                    'description': 'multi constraint test',
                    'dependencies': dependencies,
                    'extras': extras or {},
                }
            }
        }

    @staticmethod
    def _lock_package(name: str, version: str, dependencies: dict = None,
                      extras: dict = None) -> dict:
        package = {
            'name': name,
            'version': version,
            'description': f'{name} {version}',
            'optional': False,
            'files': [],
        }
        if dependencies:
            package['dependencies'] = dependencies
        if extras:
            package['extras'] = extras
        return package

    def _build(self, dependencies: dict, locker_packages: list,
               use_extras: frozenset = frozenset(), extras_map: dict = None):
        project = self._make_project(dependencies, extras_map)
        locker = {
            'metadata': {'lock-version': '2.0'},
            'package': locker_packages,
        }
        return self.bb._make_bom(
            project, locker,
            use_groups=frozenset({'main'}),
            use_extras=use_extras,
            mc_type=ComponentType.APPLICATION,
        )

    # region declaration shape normalization

    def test_normalize_string_declaration(self) -> None:
        self.assertEqual(
            PoetryBB._normalize_dep_specs('^1.2.3'),
            [{'version': '^1.2.3'}]
        )

    def test_normalize_table_declaration(self) -> None:
        self.assertEqual(
            PoetryBB._normalize_dep_specs({'version': '^1.2.3', 'optional': True}),
            [{'version': '^1.2.3', 'optional': True}]
        )

    def test_normalize_list_declaration(self) -> None:
        self.assertEqual(
            PoetryBB._normalize_dep_specs([
                {'version': '1.0', 'python': '>=3.8'},
                {'version': '0.9', 'python': '<3.8', 'extras': ['x']},
                '0.8',
            ]),
            [
                {'version': '1.0', 'python': '>=3.8'},
                {'version': '0.9', 'python': '<3.8', 'extras': ['x']},
                {'version': '0.8'},
            ]
        )

    # endregion declaration shape normalization

    def test_string_declaration_builds_component(self) -> None:
        bom = self._build(
            {'python': '^3', 'tomli': '^2.0'},
            [self._lock_package('tomli', '2.0.1')],
        )
        self.assertEqual(_component_names(bom), {'tomli'})
        self.assertEqual(_dep_refs(bom, 'mc-test'), {'tomli@2.0.1'})

    def test_table_declaration_builds_component(self) -> None:
        bom = self._build(
            {'python': '^3', 'tomli': {'version': '^2.0'}},
            [self._lock_package('tomli', '2.0.1')],
        )
        self.assertEqual(_component_versions(bom, 'tomli'), {'2.0.1'})

    def test_multi_constraint_all_required_includes_all_lock_entries_once(self) -> None:
        # all declarations are required -> every locked variant is linked, but each
        # lock entry (component) is only created once
        bom = self._build(
            {'python': '^3', 'tomli': [
                {'version': '2.0.1', 'python': '>=3.8'},
                {'version': '1.2.3', 'python': '<3.8'},
            ]},
            [
                self._lock_package('tomli', '2.0.1'),
                self._lock_package('tomli', '1.2.3'),
            ],
        )
        self.assertEqual(_component_versions(bom, 'tomli'), {'2.0.1', '1.2.3'})
        self.assertEqual(
            _dep_refs(bom, 'mc-test'), {'tomli@2.0.1', 'tomli@1.2.3#1'})
        # no duplicate component entries
        self.assertEqual(
            sorted(c.bom_ref.value for c in bom.components),
            ['tomli@1.2.3#1', 'tomli@2.0.1']
        )

    def test_one_required_declaration_keeps_optional_package_without_extra(self) -> None:
        # one declaration is required, another is optional -> the package MUST be included
        # even though the extra that would pull in the optional declaration is not selected
        bom = self._build(
            {'python': '^3', 'attrs': [
                {'version': '22.2.0', 'python': '>=3.8'},
                {'version': '21.4.0', 'python': '<3.8', 'optional': True},
            ]},
            [
                self._lock_package('attrs', '22.2.0'),
                self._lock_package('attrs', '21.4.0'),
            ],
        )
        self.assertEqual(_component_versions(bom, 'attrs'), {'22.2.0', '21.4.0'})
        self.assertIn('attrs@22.2.0', _dep_refs(bom, 'mc-test'))

    def test_all_optional_declaration_skipped_without_extra(self) -> None:
        bom = self._build(
            {'python': '^3', 'attrs': [
                {'version': '22.2.0', 'optional': True},
                {'version': '21.4.0', 'optional': True},
            ]},
            [
                self._lock_package('attrs', '22.2.0'),
                self._lock_package('attrs', '21.4.0'),
            ],
        )
        self.assertNotIn('attrs', _component_names(bom))
        self.assertEqual(_dep_refs(bom, 'mc-test'), set())

    def test_all_optional_declaration_included_with_extra(self) -> None:
        bom = self._build(
            {'python': '^3', 'attrs': [
                {'version': '22.2.0', 'optional': True},
                {'version': '21.4.0', 'optional': True, 'extras': ['mypy']},
            ]},
            [
                self._lock_package(
                    'attrs', '22.2.0',
                    extras={'mypy': ['mypy-extensions']}),
                self._lock_package(
                    'attrs', '21.4.0',
                    extras={'mypy': ['mypy-extensions']}),
                self._lock_package('mypy-extensions', '0.4.3'),
            ],
            use_extras=frozenset({'mypy'}),
            extras_map={'mypy': ['attrs']},
        )
        self.assertEqual(_component_versions(bom, 'attrs'), {'22.2.0', '21.4.0'})
        # extras pulled from the lock file are resolved transitively
        self.assertIn('mypy-extensions', _component_names(bom))

    def test_extras_of_overlapping_declarations_are_merged(self) -> None:
        # extras declared on different constraints must be merged on the shared
        # lock entry, so the extra's transitive deps are not missed
        bom = self._build(
            {'python': '^3', 'attrs': [
                {'version': '22.2.0', 'extras': ['mypy']},
                {'version': '21.4.0', 'extras': ['cov']},
            ]},
            [
                self._lock_package(
                    'attrs', '22.2.0',
                    extras={
                        'mypy': ['mypy-extensions'],
                        'cov': ['coverage'],
                    }),
                self._lock_package('mypy-extensions', '0.4.3'),
                self._lock_package('coverage', '7.0.0'),
            ],
        )
        attrs = next(c for c in bom.components if c.name == 'attrs')
        self.assertEqual(_extra_props(attrs), {'mypy', 'cov'})
        self.assertEqual(
            _dep_refs(bom, attrs.bom_ref),
            {'mypy-extensions@0.4.3', 'coverage@7.0.0'}
        )

    def test_optional_package_with_extra_does_not_get_required_without_extra(self) -> None:
        # single optional table declaration: baseline behavior must be preserved
        bom = self._build(
            {'python': '^3', 'attrs': {'version': '22.2.0', 'optional': True}},
            [self._lock_package('attrs', '22.2.0')],
        )
        self.assertNotIn('attrs', _component_names(bom))

    def test_optional_package_included_when_extra_selected(self) -> None:
        bom = self._build(
            {'python': '^3', 'attrs': {
                'version': '22.2.0', 'optional': True, 'extras': ['mypy']}},
            [
                self._lock_package(
                    'attrs', '22.2.0',
                    extras={'mypy': ['mypy-extensions']}),
                self._lock_package('mypy-extensions', '0.4.3'),
            ],
            use_extras=frozenset({'mypy'}),
            extras_map={'mypy': ['attrs']},
        )
        self.assertEqual(_component_versions(bom, 'attrs'), {'22.2.0'})
        self.assertIn('mypy-extensions', _component_names(bom))

    def test_normalize_packagename_applied_to_dependency_name(self) -> None:
        # ensure normalization still happens for list-shaped declarations
        bom = self._build(
            {'python': '^3', 'Some_Package': [{'version': '1.0'}]},
            [self._lock_package('some-package', '1.0')],
        )
        self.assertIn(
            normalize_packagename('Some_Package'),
            {normalize_packagename(n) for n in _component_names(bom)}
        )


class Poetry2DependenciesTest(TestCase):

    @staticmethod
    def _poetry(dependencies: dict) -> dict:
        return {'dependencies': dependencies}

    def test_string_spec_yields_requirement_without_extras(self) -> None:
        req = next(poetry2dependencies(self._poetry({'tomli': '^2.0'})))
        self.assertEqual(req.name, 'tomli')
        self.assertEqual(req.extras, set())

    def test_table_spec_yields_requirement_with_extras(self) -> None:
        req = next(poetry2dependencies(self._poetry({
            'attrs': {'version': '22.2.0', 'extras': ['mypy', 'cov']}
        })))
        self.assertEqual(req.name, 'attrs')
        self.assertEqual(req.extras, {'mypy', 'cov'})

    def test_multi_constraint_spec_merges_extras(self) -> None:
        # list-shaped declarations must not crash the parser and must merge the
        # extras declared on the individual constraints
        req = next(poetry2dependencies(self._poetry({
            'attrs': [
                {'version': '22.2.0', 'extras': ['mypy']},
                {'version': '21.4.0', 'extras': ['cov']},
                '20.0.0',
            ]
        })))
        self.assertEqual(req.name, 'attrs')
        self.assertEqual(req.extras, {'mypy', 'cov'})

    def test_multi_constraint_spec_without_extras(self) -> None:
        req = next(poetry2dependencies(self._poetry({
            'tomli': [{'version': '2.0.1'}, '1.2.3']
        })))
        self.assertEqual(req.name, 'tomli')
        self.assertEqual(req.extras, set())
