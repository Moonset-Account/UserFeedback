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

"""Cross-format (JSON vs XML) semantic parity tests for vulnerability handling.

These tests never treat one serialization format as the truth for the other:
each rendered document is parsed on its own and projected into an independent
neutral business structure (see :mod:`tests.parity.projection`) before the two
projections are compared. Every assertion is recorded through
:class:`~tests.parity.check.Checker`, so the local verification wrapper
(``tools/local_verify.py``) can report real assertion counts and fail on zero
assertions.
"""
