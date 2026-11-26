"""De-identification gateway facade.

Binds together the field dictionary, the project-scoped persistent UID
mapper and the recursive tree processor.  One :class:`DeidGateway`
instance (with one mapping file) is shared by concurrent exporters and
by the three export batches (images / annotations / review checklist).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .dictionary import FieldDictionary
from .processor import ProcessReport, process_tree
from .uids import (
    DEFAULT_BASE_ROOT,
    ShortCodeGenerator,
    UidMapper,
)


@dataclass
class ExportResult:
    batch: Optional[str]
    project: str
    tree: Any
    report: ProcessReport
    mapper: UidMapper

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.tree, ensure_ascii=False, indent=indent)

    def report_dict(self) -> Dict[str, Any]:
        return self.report.as_dict()


class DeidGateway:
    """High level API used by the three exporters."""

    def __init__(
        self,
        dictionary: Union[FieldDictionary, dict, list, str, Path, None] = None,
        mapping_path: Union[str, Path, UidMapper, None] = None,
        project: Optional[str] = None,
        base_root: Optional[str] = DEFAULT_BASE_ROOT,
        *,
        mapper: Optional[UidMapper] = None,
        code_generator=None,
        preserved_uids=None,
        name_heuristics: bool = True,
        prefix: Optional[str] = None,
    ) -> None:
        # Allow DeidGateway(dictionary, existing_mapper).
        if isinstance(mapping_path, UidMapper) and mapper is None:
            mapper = mapping_path
            mapping_path = None
        if prefix is not None:
            base_root = prefix
        self.dictionary = (
            dictionary
            if isinstance(dictionary, FieldDictionary)
            else FieldDictionary.from_dict(dictionary)
        )
        constants = set(self.dictionary.registered_constants)
        if preserved_uids:
            constants.update(preserved_uids)
        if mapper is not None:
            self.mapper = mapper
            self.mapper.preserved_uids.update(constants)
        else:
            self.mapper = UidMapper(
                mapping_path,
                base_root=base_root,
                code_generator=code_generator,
                preserved_uids=constants,
            )
        self.default_project = project
        self.name_heuristics = name_heuristics

    # ------------------------------------------------------------ processing
    def process(
        self,
        tree: Any,
        project: Optional[str] = None,
        *,
        report: Optional[ProcessReport] = None,
    ) -> Any:
        """Return a de-identified deep copy of *tree*.

        *tree* may be a parsed JSON object/list or a JSON document
        string/bytes.
        """

        if isinstance(tree, (str, bytes)):
            if isinstance(tree, bytes):
                tree = tree.decode("utf-8")
            if tree.lstrip()[:1] in {"{", "["}:
                tree = json.loads(tree)
        active_project = project or self.default_project
        return process_tree(
            tree,
            dictionary=self.dictionary,
            mapper=self.mapper,
            project=active_project,
            report=report,
            name_heuristics=self.name_heuristics,
        )

    def process_json(
        self,
        payload: Union[str, bytes],
        project: Optional[str] = None,
        *,
        report: Optional[ProcessReport] = None,
    ) -> str:
        """Parse a JSON string/bytes, process it and return JSON text."""

        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        tree = json.loads(payload)
        result = self.process(tree, project, report=report)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def process_file(
        self,
        input_path: Union[str, Path],
        output_path: Union[str, Path, None] = None,
        project: Optional[str] = None,
    ) -> ExportResult:
        """Read a JSON export file, de-identify and optionally write it."""

        input_path = Path(input_path)
        tree = json.loads(input_path.read_text(encoding="utf-8"))
        report = ProcessReport()
        result = self.process(tree, project, report=report)
        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return ExportResult(
            batch=input_path.stem,
            project=UidMapper.normalize_project(project or self.default_project),
            tree=result,
            report=report,
            mapper=self.mapper,
        )

    def export(
        self,
        tree: Any,
        project: Optional[str] = None,
        *,
        batch: Optional[str] = None,
    ) -> ExportResult:
        """Process one export batch and bundle tree + audit report."""

        report = ProcessReport()
        processed = self.process(tree, project, report=report)
        return ExportResult(
            batch=batch,
            project=UidMapper.normalize_project(project or self.default_project),
            tree=processed,
            report=report,
            mapper=self.mapper,
        )

    # --------------------------------------------------------------- mapping
    def map_uid(self, uid: str, project: Optional[str] = None) -> str:
        return self.mapper.map_uid(uid, project or self.default_project)

    def save_mapping(self, path: Union[str, Path, None] = None) -> Path:
        return self.mapper.save(path)

    @classmethod
    def load_mapping(
        cls,
        mapping_path: Union[str, Path],
        dictionary: Union[FieldDictionary, dict, str, Path, None] = None,
        **kwargs: Any,
    ) -> "DeidGateway":
        """Create a gateway whose mapper reloads an existing mapping file."""

        mapper = UidMapper.load(mapping_path)
        return cls(dictionary=dictionary, mapper=mapper, **kwargs)


# Vocabulary aliases.
DicomDeidentifier = DeidGateway
DeidentificationGateway = DeidGateway
Deidentifier = DeidGateway
Gateway = DeidGateway


def deidentify(
    tree: Any,
    dictionary: Union[FieldDictionary, dict, list, str, Path, None] = None,
    *,
    project: Optional[str] = None,
    mapper: Optional[UidMapper] = None,
    mapping_path: Union[str, Path, None] = None,
    base_root: Optional[str] = DEFAULT_BASE_ROOT,
    report: Optional[ProcessReport] = None,
    **kwargs: Any,
) -> Any:
    """One shot convenience wrapper returning the de-identified tree."""

    gateway = DeidGateway(
        dictionary,
        mapping_path=mapping_path,
        project=project,
        base_root=base_root,
        mapper=mapper,
        **kwargs,
    )
    return gateway.process(tree, project, report=report)
