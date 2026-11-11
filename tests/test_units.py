"""单位子集解析与换算的独立单元测试。"""

import pytest

from collector import units
from collector.units import UnsupportedUnit, normalize, scale_ratio


def test_normalize_aliases():
    assert normalize("g").canonical == "g"
    assert normalize(" 毫克 ").canonical == "mg"
    assert normalize("％").canonical == "%"
    assert normalize("wt%").canonical == "%"
    assert normalize("mg/g").canonical == "mg/g"


def test_mass_scale_factors():
    assert scale_ratio(normalize("mg"), normalize("g")) == pytest.approx(1e-3)
    assert scale_ratio(normalize("kg"), normalize("g")) == pytest.approx(1e3)
    assert scale_ratio(normalize("g"), normalize("g")) == pytest.approx(1.0)


def test_fraction_scale_factors():
    # mg/g -> g/g 为 1e-3；% -> g/g 为 1e-2
    assert scale_ratio(normalize("mg/g"), normalize("g/g")) == pytest.approx(1e-3)
    assert scale_ratio(normalize("%"), normalize("g/g")) == pytest.approx(1e-2)


def test_cross_dimension_rejected():
    with pytest.raises(UnsupportedUnit):
        scale_ratio(normalize("g"), normalize("%"))


def test_unsupported_unit_rejected():
    for raw in ("lb", "mol/L", "ppm", ""):
        with pytest.raises(UnsupportedUnit):
            normalize(raw)
    with pytest.raises(UnsupportedUnit):
        normalize(None)


def test_supported_subset_declared():
    assert set(units.SUPPORTED_UNITS) == {"g", "mg", "kg", "%", "g/g", "mg/g"}
