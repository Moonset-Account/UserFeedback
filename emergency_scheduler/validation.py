"""Structural validation of scheduling instances."""

from __future__ import annotations

from .model import Component, Problem, Service, Stage


class ValidationError(ValueError):
    """Raised when an instance is structurally malformed."""


def validate_problem(problem: Problem) -> None:
    if not isinstance(problem, Problem):
        raise ValidationError("problem must be a Problem instance")
    if not isinstance(problem.horizon, int) or isinstance(problem.horizon, bool):
        raise ValidationError("horizon must be an integer")
    if problem.horizon <= 0:
        raise ValidationError("horizon must be strictly positive")
    if (
        not isinstance(problem.personnel_capacity, int)
        or isinstance(problem.personnel_capacity, bool)
        or problem.personnel_capacity < 0
    ):
        raise ValidationError("personnel_capacity must be a non-negative integer")

    if not problem.components:
        raise ValidationError("at least one component is required")
    component_ids: set[str] = set()
    stage_ids: set[str] = set()
    service_ids: set[str] = {s.service_id for s in problem.services}

    for component in problem.components:
        if not isinstance(component, Component):
            raise ValidationError("components must be Component instances")
        cid = component.component_id
        if not isinstance(cid, str) or not cid:
            raise ValidationError("component ids must be non-empty strings")
        if cid in component_ids:
            raise ValidationError(f"duplicate component id: {cid}")
        component_ids.add(cid)
        # A component without stages is a healthy redundant unit: it stays
        # online for the whole horizon unless a repair stage explicitly stops
        # it. At least one component must carry repair work (checked below).

    if not any(component.stages for component in problem.components):
        raise ValidationError("at least one component must carry repair stages")

    for component in problem.components:
        cid = component.component_id
        for stage in component.stages:
            if not isinstance(stage.stage_id, str) or not stage.stage_id:
                raise ValidationError("stage ids must be non-empty strings")
            if stage.stage_id in stage_ids:
                raise ValidationError(f"duplicate stage id: {stage.stage_id}")
            stage_ids.add(stage.stage_id)
            if stage.component_id != cid:
                raise ValidationError(
                    f"stage {stage.stage_id} is registered under component {cid} "
                    f"but declares {stage.component_id}"
                )
            if (
                not isinstance(stage.duration, int)
                or isinstance(stage.duration, bool)
                or stage.duration <= 0
            ):
                raise ValidationError(
                    f"stage {stage.stage_id} duration must be a positive integer"
                )
            if (
                not isinstance(stage.personnel, int)
                or isinstance(stage.personnel, bool)
                or stage.personnel < 0
            ):
                raise ValidationError(
                    f"stage {stage.stage_id} personnel must be a non-negative integer"
                )
            _check_mapping(stage.spare_needs, f"stage {stage.stage_id} spare_needs")
            _check_mapping(
                stage.spare_releases, f"stage {stage.stage_id} spare_releases"
            )
            for sid in stage.stop_components:
                if sid not in component_ids:
                    raise ValidationError(
                        f"stage {stage.stage_id} stops unknown component {sid}"
                    )
            if cid in stage.stop_components:
                raise ValidationError(
                    f"stage {stage.stage_id} cannot stop its own component {cid}; "
                    "the component is already failed until repair completes"
                )
        # two stages of the same component forcing the same working component
        # offline is only a problem if their intervals overlap, which is
        # impossible because same-component stages are strictly ordered;
        # cross-component conflicts are caught by feasibility checks.

    for service in problem.services:
        if not isinstance(service, Service):
            raise ValidationError("services must be Service instances")
        if not isinstance(service.service_id, str) or not service.service_id:
            raise ValidationError("service ids must be non-empty strings")
        if service.service_id in [s.service_id for s in problem.services if s is not service]:
            raise ValidationError(f"duplicate service id: {service.service_id}")
        if service.demand < 0:
            raise ValidationError(f"service {service.service_id} demand must be >= 0")
        if service.min_capacity < 0:
            raise ValidationError(
                f"service {service.service_id} min_capacity must be >= 0"
            )
        if service.weight < 0:
            raise ValidationError(f"service {service.service_id} weight must be >= 0")

    for component in problem.components:
        for sid in component.service_capacity:
            if sid not in service_ids:
                raise ValidationError(
                    f"component {component.component_id} supplies unknown service {sid}"
                )
        for value in component.service_capacity.values():
            if value < 0:
                raise ValidationError(
                    f"component {component.component_id} has negative service capacity"
                )

    _check_mapping(problem.initial_spares, "initial_spares")
    known_spares = set(problem.initial_spares)
    for component in problem.components:
        for stage in component.stages:
            known_spares.update(stage.spare_needs)
            known_spares.update(stage.spare_releases)
    for component in problem.components:
        for stage in component.stages:
            for sid, amount in stage.spare_needs.items():
                if amount <= 0:
                    raise ValidationError(
                        f"stage {stage.stage_id} spare_needs values must be positive"
                    )
            for sid, amount in stage.spare_releases.items():
                if amount <= 0:
                    raise ValidationError(
                        f"stage {stage.stage_id} spare_releases values must be positive"
                    )
    # A spare that no stage ever releases can only be used up to the initial
    # stock; that is an ordinary feasibility condition, not a structural error.


def _check_mapping(mapping: dict[str, int], label: str) -> None:
    if not isinstance(mapping, dict):
        raise ValidationError(f"{label} must be a dict")
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            raise ValidationError(f"{label} keys must be non-empty strings")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{label} values must be non-negative integers")
