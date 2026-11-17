import json
import xml.etree.ElementTree as ET

import pretend  # type: ignore
import pytest
from packaging.version import Version

import pip_audit._service as service
from pip_audit._format import CycloneDxFormat


def _component_refs(components: list[dict]) -> set[str]:
    return {component["bom-ref"] for component in components}


def _vulnerability_affect_refs(vulnerability: dict) -> set[str]:
    return {affect["ref"] for affect in vulnerability.get("affects", [])}


def _xml_localname(element: ET.Element) -> str:
    return element.tag.split("}", 1)[-1]


def _xml_component_refs(root: ET.Element) -> set[str]:
    refs = set()
    for component in root.iter():
        if _xml_localname(component) == "component":
            refs.add(component.attrib["bom-ref"])
    return refs


def _xml_component_names(root: ET.Element) -> set[str]:
    names = set()
    for component in root.iter():
        if _xml_localname(component) != "component":
            continue
        for child in component:
            if _xml_localname(child) == "name":
                names.add(child.text)
    return names


def _xml_vulnerability_affect_refs(root: ET.Element) -> list[set[str]]:
    affect_refs = []
    for vulnerability in root.iter():
        if _xml_localname(vulnerability) != "vulnerability":
            continue
        refs = set()
        for element in vulnerability.iter():
            if _xml_localname(element) == "affects":
                for ref in element.iter():
                    if _xml_localname(ref) == "ref":
                        refs.add(ref.text)
        affect_refs.append(refs)
    return affect_refs


@pytest.mark.parametrize(
    "inner", [CycloneDxFormat.InnerFormat.Xml, CycloneDxFormat.InnerFormat.Json]
)
def test_cyclonedx_manifest(inner):
    fmt = CycloneDxFormat(inner_format=inner)
    assert fmt.is_manifest


def test_cyclonedx_inner_json(vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)

    # We don't test CycloneDX's formatting/layout decisions, only that
    # the formatter emits correct JSON when initialized in JSON mode.
    assert json.loads(formatter.format(vuln_data, list())) is not None


def test_cyclonedx_inner_xml(vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Xml)

    # We don't test CycloneDX's formatting/layout decisions, only that
    # the formatter emits correct XML when initialized in XML mode.
    assert ET.fromstring(formatter.format(vuln_data, list())) is not None


def test_cyclonedx_skipped_dep(vuln_data_skipped_dep):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)

    # Just test that a skipped dependency doesn't cause the formatter to blow up
    assert json.loads(formatter.format(vuln_data_skipped_dep, list())) is not None


def test_cyclonedx_fix(monkeypatch, vuln_data, fix_data):
    import pip_audit._format.cyclonedx as cyclonedx

    logger = pretend.stub(warning=pretend.call_recorder(lambda s: None))
    monkeypatch.setattr(cyclonedx, "logger", logger)

    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)
    assert json.loads(formatter.format(vuln_data, fix_data)) is not None

    # The CycloneDX format doesn't support fixes so we expect to log a warning
    assert len(logger.warning.calls) == 1


def test_cyclonedx_json_vulnerabilities_reference_affected_components(vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)
    bom = json.loads(formatter.format(vuln_data, list()))

    component_refs = _component_refs(bom["components"])

    # Each vulnerability must be traceable to the actual affected component
    # through an `affects` reference, and every reference must resolve to a
    # component that exists in the BOM.
    assert bom["vulnerabilities"]
    for vulnerability in bom["vulnerabilities"]:
        affect_refs = _vulnerability_affect_refs(vulnerability)
        assert affect_refs
        assert affect_refs <= component_refs

    # The fixture contains one vulnerable dependency with two findings and
    # another with one finding, for three linked findings in total.
    assert len(bom["vulnerabilities"]) == 3
    linked_refs = set()
    for vulnerability in bom["vulnerabilities"]:
        linked_refs |= _vulnerability_affect_refs(vulnerability)
    assert len(linked_refs) == 2


def test_cyclonedx_xml_vulnerabilities_reference_affected_components(vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Xml)
    root = ET.fromstring(formatter.format(vuln_data, list()))

    component_refs = _xml_component_refs(root)
    affect_refs_per_vulnerability = _xml_vulnerability_affect_refs(root)

    # The XML output path carries the same component associations as JSON.
    assert len(affect_refs_per_vulnerability) == 3
    for affect_refs in affect_refs_per_vulnerability:
        assert affect_refs
        assert affect_refs <= component_refs


def test_cyclonedx_same_name_different_versions_are_distinctly_linked():
    # Two component records can share a name while being installed at
    # different versions; each finding has to point at the version it affects.
    foo_old = service.ResolvedDependency(name="foo", version=Version("1.0"))
    foo_new = service.ResolvedDependency(name="foo", version=Version("2.0"))
    data = {
        foo_old: [
            service.VulnerabilityResult(
                id="VULN-OLD",
                description="Affects only the old foo release",
                fix_versions=[Version("1.1")],
                aliases=set(),
            )
        ],
        foo_new: [
            service.VulnerabilityResult(
                id="VULN-NEW",
                description="Affects only the new foo release",
                fix_versions=[Version("2.1")],
                aliases=set(),
            )
        ],
    }

    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)
    bom = json.loads(formatter.format(data, list()))

    assert len(bom["components"]) == 2
    components_by_version = {
        component["version"]: component["bom-ref"] for component in bom["components"]
    }
    assert set(components_by_version) == {"1.0", "2.0"}

    vulnerabilities_by_id = {
        vulnerability["id"]: vulnerability for vulnerability in bom["vulnerabilities"]
    }
    assert _vulnerability_affect_refs(vulnerabilities_by_id["VULN-OLD"]) == {
        components_by_version["1.0"]
    }
    assert _vulnerability_affect_refs(vulnerabilities_by_id["VULN-NEW"]) == {
        components_by_version["2.0"]
    }


def test_cyclonedx_advisory_affecting_multiple_components_links_each():
    # A single advisory can apply to several components; each actually
    # affected component has to remain traceable from the findings.
    foo = service.ResolvedDependency(name="foo", version=Version("1.0"))
    bar = service.ResolvedDependency(name="bar", version=Version("0.1"))
    shared_advisory = dict(
        id="VULN-SHARED",
        description="Affects both foo and bar",
        fix_versions=[],
        aliases=set(),
    )
    data = {
        foo: [service.VulnerabilityResult(**shared_advisory)],
        bar: [service.VulnerabilityResult(**shared_advisory)],
    }

    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)
    bom = json.loads(formatter.format(data, list()))

    component_refs = _component_refs(bom["components"])
    assert len(component_refs) == 2

    # One finding per affected component, each pointing at its own component,
    # so both affected components are traceable from the shared advisory.
    linked_refs = set()
    for vulnerability in bom["vulnerabilities"]:
        assert vulnerability["id"] == "VULN-SHARED"
        affect_refs = _vulnerability_affect_refs(vulnerability)
        assert len(affect_refs) == 1
        linked_refs |= affect_refs
    assert linked_refs == component_refs


def test_cyclonedx_unaffected_components_remain_visible(no_vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Json)
    bom = json.loads(formatter.format(no_vuln_data, list()))

    # Components without findings are still emitted as BOM components;
    # only the vulnerability list is empty.
    assert len(bom["components"]) == 2
    assert {component["name"] for component in bom["components"]} == {"foo", "bar"}
    assert bom["vulnerabilities"] == []


def test_cyclonedx_unaffected_components_remain_visible_xml(no_vuln_data):
    formatter = CycloneDxFormat(inner_format=CycloneDxFormat.InnerFormat.Xml)
    root = ET.fromstring(formatter.format(no_vuln_data, list()))

    # The XML output path keeps the same components as the JSON path.
    assert len(_xml_component_refs(root)) == 2
    assert _xml_component_names(root) == {"foo", "bar"}
    assert _xml_vulnerability_affect_refs(root) == []
