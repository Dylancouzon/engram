"""map_points: the 2D projection + neighbors behind the dashboard map."""

from __future__ import annotations

import math

from conftest import make_store


def test_map_points_shape_and_neighbors(config):
    """Two disjoint-vocabulary clusters must project apart, and each point's
    nearest neighbors must be from its own cluster — exercises the PCA seed
    and the cosine-neighbor math against real Edge vectors."""
    store = make_store(config)
    try:
        cats = ["cat pet fluffy purr", "cat pet sleepy nap",
                "cat pet whiskers paws"]
        code = ["rust code fast safe", "rust code memory build",
                "rust code async trait"]
        group = {}
        for text in cats + code:
            mid = store.remember(text)[0].memory.id
            group[mid] = "cat" if text in cats else "code"

        points = store.map_points(neighbors=2)
        assert len(points) == 6
        for p in points:
            assert isinstance(p["x"], float) and isinstance(p["y"], float)
            assert not math.isnan(p["x"]) and not math.isnan(p["y"])
            # Top-2 neighbors of a point share its cluster.
            assert all(group[n] == group[p["id"]] for n in p["neighbors"])

        # Cluster centroids separate in the projection.
        coords = {p["id"]: (p["x"], p["y"]) for p in points}
        def centroid(g):
            pts = [coords[i] for i in group if group[i] == g]
            return (sum(x for x, _ in pts) / len(pts),
                    sum(y for _, y in pts) / len(pts))
        cx, cy = centroid("cat")
        dx, dy = centroid("code")
        between = math.hypot(cx - dx, cy - dy)
        within = max(
            math.hypot(coords[i][0] - (cx if group[i] == "cat" else dx),
                       coords[i][1] - (cy if group[i] == "cat" else dy))
            for i in group
        )
        assert between > within  # groups are more apart than they are spread
    finally:
        store.close()


def test_map_points_empty(config):
    store = make_store(config)
    try:
        assert store.map_points() == []
    finally:
        store.close()
