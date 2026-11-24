"""Scenario walk-through printing hit / re-bind reasons.

Run with:  python3 demo.py

It exercises, against purely synthetic in-memory data:

1. first prepare/exec and a fresh hit;
2. a same-named table created in an earlier namespace: a new transaction
   re-binds (shadowed) while the still-open old transaction keeps hitting the
   originally bound object;
3. DDL in namespaces outside the search path: the prepared plan is reused
   unchanged (hit_no_relevant_change);
4. a search-path switch landing on another cache key;
5. a drop forcing a re-bind.
"""

from plan_cache import (
    BindingCache,
    Catalog,
    Session,
)


def show(title: str, result) -> None:
    print(f"  {title:<48} hit={result.hit!s:<5} "
          f"oid={result.oid:<3} reason={result.reason}")


def main() -> None:
    catalog = Catalog()
    catalog.create_namespace("app")
    catalog.create_namespace("shared")
    catalog.create_namespace("misc")
    catalog.create_table("shared", "orders")  # oid 1

    cache = BindingCache()
    session = Session(catalog, cache, search_path=("app", "shared"))
    stmt_text = "SELECT * FROM orders WHERE id = $1"

    print("== 1. prepare inside T1, first bind then fresh hit ==")
    session.begin()
    stmt = session.prepare(stmt_text, "orders")
    show("T1 first execute", session.execute(stmt))
    show("T1 repeat execute", session.execute(stmt))

    print("== 2. shadow table appears in earlier namespace 'app' ==")
    shadow = catalog.create_table("app", "orders")  # oid 2
    print(f"  DDL: create_table app.orders -> oid {shadow.relation_oid}, "
          f"catalog now at v{catalog.current_version}")
    show("T1 still open, re-executes", session.execute(stmt))
    session.commit()

    session.begin()
    show("T2 new transaction", session.execute(stmt))
    session.commit()

    print("== 3. DDL outside the search path keeps the plan ==")
    catalog.create_table("misc", "orders")
    catalog.create_table("misc", "audit")
    session.begin()
    show("T3 after off-path DDL", session.execute(stmt))
    session.commit()

    print("== 4. search path switched -> different cache key ==")
    session.begin()
    session.set_search_path("shared", "app")
    show("T4 path (shared, app)", session.execute(stmt))
    session.commit()

    print("== 5. bound object dropped -> rebind to replacement ==")
    catalog.drop_table("app", "orders")
    session.begin()
    session.set_search_path("app", "shared")
    show("T5 after drop, falls back to shared.orders",
         session.execute(stmt))
    session.commit()

    print()
    print("Binding history for ('orders', ('app','shared'), stmt):")
    for binding in cache.history("orders", ("app", "shared"), stmt_text):
        print(f"  v{binding.catalog_version}: oid={binding.oid} "
              f"namespace={binding.namespace}")


if __name__ == "__main__":
    main()
