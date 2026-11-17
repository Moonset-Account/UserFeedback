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

"""Minimal recording assertion harness.

Unlike bare ``assert`` statements (which can be stripped with ``-O`` and are
invisible to a programmatic runner), every check performed with a
:class:`Checker` is counted. A case that makes zero assertions is therefore
detectable and must be treated as failed by the verification wrapper.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CheckFailure:
    """One failed assertion with the model field it relates to (when known)."""

    message: str
    field: Optional[str] = None
    expected: Any = None
    observed: Any = None


@dataclass
class Checker:
    """Counts assertions instead of raising on the first failure.

    Failures are collected so a single case run can report every semantic
    divergence at once; callers decide afterwards whether the case passed.
    """

    name: str
    failures: list[CheckFailure] = field(default_factory=list)
    _passed: int = 0
    _skipped: int = 0

    @property
    def passed(self) -> int:
        return self._passed

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def total(self) -> int:
        return self._passed + self.failed + self._skipped

    def check(
        self,
        condition: bool,
        message: str,
        *,
        field: Optional[str] = None,
        expected: Any = None,
        observed: Any = None,
    ) -> bool:
        """Record one assertion; return whether it held."""
        if condition:
            self._passed += 1
        else:
            self.failures.append(
                CheckFailure(message=message, field=field,
                             expected=expected, observed=observed)
            )
        return condition

    def check_equal(
        self,
        expected: Any,
        observed: Any,
        message: str,
        *,
        field: Optional[str] = None,
    ) -> bool:
        return self.check(
            expected == observed, message,
            field=field, expected=expected, observed=observed,
        )

    def skip(self, reason: str) -> None:
        """Record an explicit, justified skip (never used to dodge assertions)."""
        self._skipped += 1
        self.failures.append(
            CheckFailure(message=f'skipped: {reason}', field=None)
        )
