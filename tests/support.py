"""Shared synthetic fixtures for the test suite.

All records here are invented for local, offline tests.
"""

from __future__ import annotations


def legacy_record(**overrides) -> dict:
    record = {
        "version": "1",
        "task_type": "text_label",
        "task_id": "legacy-001",
        "content": "Is this review positive?",
        "answer_labels": ["positive", "negative"],
        "extensions": {"legacy_source": "old-pipeline"},
    }
    record.update(overrides)
    return record


def modern_record(**overrides) -> dict:
    record = {
        "version": "2",
        "task_type": "text_classification",
        "record_id": "modern-001",
        "text": "Is this review positive?",
        "labels": ["positive", "negative"],
        "extensions": {"source_system": "v2-pipeline"},
    }
    record.update(overrides)
    return record


def document_record(**overrides) -> dict:
    record = {
        "version": "2",
        "task_type": "document_analysis",
        "record_id": "doc-001",
        "document": {
            "title": "Quarterly report",
            "paragraphs": ["Revenue rose.", "Costs fell."],
        },
        "extensions": {"annotations": {"reviewer": "synthetic", "round": 1}},
    }
    record.update(overrides)
    return record
