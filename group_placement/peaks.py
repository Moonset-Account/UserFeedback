"""Per-Pod peak simultaneous resource usage computation.

The lifecycle modeled here mirrors the Kubernetes init/app-container model:

* Init containers start one at a time, in order.
* An ordinary init container runs alone (besides previously started resident
  init containers) and exits before the next one starts.
* A resident init container, once started, stays running forever.
* Only after every ordinary init container has exited do all business
  (app) containers start together, alongside every resident init container.

For each lifecycle stage we record the maximum simultaneous CPU and memory
usage.  A Pod's reserved peak is the largest stage usage plus the Pod's
fixed per-resource overhead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Pod


@dataclass(frozen=True)
class Stage:
    """A point in the Pod lifecycle and what is running then."""

    name: str
    index: int
    running: tuple[str, ...]


@dataclass(frozen=True)
class StageUsage:
    """Simultaneous resource usage observed at one lifecycle stage."""

    stage: Stage
    cpu: float
    memory: float


@dataclass(frozen=True)
class PodPeak:
    """Peak reservation for one Pod and the stage that produced it."""

    pod_name: str
    cpu_peak: float
    memory_peak: float
    cpu_stage: Stage
    memory_stage: Stage
    stages: tuple[StageUsage, ...]


def _stage_name(pod: Pod, index: int) -> str:
    if index < len(pod.inits):
        return f"init:{pod.inits[index].name}"
    return "app:all"


def compute_pod_peak(pod: Pod) -> PodPeak:
    """Compute the two resource peaks (CPU, memory) for a single Pod.

    Returns a :class:`PodPeak` carrying the peak value for each resource,
    the stage at which each peak occurs, and the full per-stage usage
    breakdown (raw usage, without overhead).

    Ties keep the earliest stage: a later stage only replaces the recorded
    peak stage when its usage is strictly greater.
    """

    stages: list[StageUsage] = []

    # Resources still held by resident init containers at the current point.
    resident_cpu = 0.0
    resident_memory = 0.0
    running: list[str] = []

    # Seed the peak with the zero-usage app stage so an all-zero Pod still
    # reports a concrete stage instead of None.
    app_seed = Stage(name="app:all", index=len(pod.inits), running=())
    cpu_peak = 0.0
    memory_peak = 0.0
    cpu_stage: Stage | None = app_seed
    memory_stage: Stage | None = app_seed

    def consider(stage: Stage, cpu: float, memory: float) -> None:
        nonlocal cpu_peak, memory_peak, cpu_stage, memory_stage
        stages.append(StageUsage(stage=stage, cpu=cpu, memory=memory))
        if cpu > cpu_peak:
            cpu_peak = cpu
            cpu_stage = stage
        if memory > memory_peak:
            memory_peak = memory
            memory_stage = stage

    for index, init in enumerate(pod.inits):
        stage = Stage(
            name=_stage_name(pod, index),
            index=index,
            running=tuple(running + [init.name]),
        )
        consider(
            stage,
            resident_cpu + init.cpu,
            resident_memory + init.memory,
        )
        if init.resident:
            resident_cpu += init.cpu
            resident_memory += init.memory
            running.append(init.name)

    app_stage = Stage(
        name="app:all",
        index=len(pod.inits),
        running=tuple(running + [app.name for app in pod.apps]),
    )
    app_cpu = resident_cpu + sum(app.cpu for app in pod.apps)
    app_memory = resident_memory + sum(app.memory for app in pod.apps)
    consider(app_stage, app_cpu, app_memory)

    # A Pod with no init and no app containers still produces one app stage
    # at zero usage, so the peak stages are never None.
    assert cpu_stage is not None and memory_stage is not None

    return PodPeak(
        pod_name=pod.name,
        cpu_peak=cpu_peak + pod.cpu_overhead,
        memory_peak=memory_peak + pod.memory_overhead,
        cpu_stage=cpu_stage,
        memory_stage=memory_stage,
        stages=tuple(stages),
    )
