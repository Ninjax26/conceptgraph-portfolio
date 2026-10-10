"""Bound visualized graph size without inventing edges or changing READY scope."""


def bound_graph_context(context, *, max_nodes, max_edges):
    ids = dict.fromkeys(str(item["concept"].get("id", "")) for item in context)
    ids.pop("", None)
    for item in context:
        for node in item.get("related_concepts", []):
            if node.get("id"):
                ids.setdefault(str(node["id"]), None)
    kept_ids = set(list(ids)[:max_nodes])
    edge_keys = {}
    for item in context:
        for edge in item.get("relationships", []):
            if str(edge.get("source")) in kept_ids and str(edge.get("target")) in kept_ids:
                key = (str(edge["source"]), str(edge["target"]), str(edge["type"]))
                edge_keys.setdefault(key, None)
    kept_edges = set(list(edge_keys)[:max_edges])
    result = []
    for item in context:
        if str(item["concept"].get("id")) not in kept_ids:
            continue
        result.append({**item, **{field: [node for node in item.get(field, []) if str(node.get("id")) in kept_ids]
                                  for field in ("related_concepts", "prerequisites", "one_hop_prerequisites", "two_hop_prerequisites")},
                       "relationships": [edge for edge in item.get("relationships", [])
                                         if (str(edge["source"]), str(edge["target"]), str(edge["type"])) in kept_edges]})
    return result, len(ids) > max_nodes or len(edge_keys) > max_edges, len(kept_ids), len(kept_edges)
