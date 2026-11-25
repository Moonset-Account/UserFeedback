"""Manual smoke: print a full rollout with per-step eligible-domain evidence."""

from rolling_relayout import (
    Node,
    NodeSelector,
    Planner,
    PlannerInput,
    Replica,
    SpreadRule,
)


def main():
    nodes = (
        Node("n1", {"rack": "rk-a", "zone": "z1"}, free_slots=0),
        Node("n2", {"rack": "rk-a", "zone": "z1"}, free_slots=0),
        Node("n3", {"rack": "rk-b", "zone": "z2"}, free_slots=0),
        Node("n4", {"rack": "rk-b", "zone": "z2"}, free_slots=0),
        Node("n5", {"rack": "rk-c", "zone": "z1"}, free_slots=2),
        Node("n6", {"rack": "rk-c", "zone": "z1"}, free_slots=2),
        Node("n7", {"rack": "rk-d", "zone": "z2"}, free_slots=2),
        Node("n8", {"rack": "rk-d", "zone": "z2"}, free_slots=2),
    )
    replicas = (
        Replica("r1", NodeSelector(), current_node="n1"),
        Replica("r2", NodeSelector(), current_node="n2"),
        Replica("r3", NodeSelector(), current_node="n3"),
        Replica("r4", NodeSelector(), current_node="n4"),
    )
    rules = (
        SpreadRule("rack-spread", "rack", max_skew=1),
        SpreadRule("zone-spread", "zone", max_skew=1),
    )
    planner_input = PlannerInput(nodes, replicas, rules, min_ready=4, max_surge=1)
    plan = Planner(planner_input).plan()
    print("feasible:", plan.feasible, "actions:", plan.length)
    print("final placement:", plan.final_placement)
    for step in plan.steps:
        print(f"\nstep {step.index}: {step.action.kind} {step.action.replica_id} "
              f"on {step.action.node_id} (running {step.running_before}->{step.running_after})")
        for name, stat in step.domain_stats_before.items():
            print(f"  [{name}] eligible={stat.eligible_domains} "
                  f"counts={dict(stat.replica_counts)} eff_min={stat.effective_min} "
                  f"min_domains={stat.min_domains}")
        for name, gate in step.gate_results.items():
            print(f"  gate {name}: allowed={gate.allowed} target={gate.target_domain} "
                  f"skew={gate.observed_skew}/{gate.max_skew} after={dict(gate.replica_counts_after)}")


if __name__ == "__main__":
    main()
