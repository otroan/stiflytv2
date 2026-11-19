"""Owned places: the logical destinations routes run between (ADR-002).

A place has a reference point and one or more access points: graph nodes on
the ground where routes actually arrive. Connectors, which are owned edges,
join the access points to the primary one so the graph is connected through
the place.

Every function takes an open connection (role stiflyt2_api) and makes its
change inside the author's open changeset, recording an event. The caller
commits. Positions go in and out as longitude and latitude (WGS 84) and are
stored in EPSG:25833.
"""
from __future__ import annotations

import json
from datetime import date

from psycopg.rows import dict_row

from stiflyt2.domain import identity

KINDS = ("hut", "parking", "glacier_edge", "other")
# The author of places made by default. A place it made stays "not reviewed"
# until a person confirms or changes it.
AUTOMATIC = "Stiflyt (automatic)"
_POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"
_LON = "round(ST_X(ST_Transform({g}, 4326))::numeric, 6)::float8"
_LAT = "round(ST_Y(ST_Transform({g}, 4326))::numeric, 6)::float8"


class NotFound(Exception):
    pass


class Conflict(Exception):
    """The change does not fit the current state: already adopted, or edited by someone else."""


class Invalid(Exception):
    pass


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def current_changeset(conn, author: str) -> str:
    """The author's open changeset for the way they came in, opened now if there is none.

    It is their item of work (d0017): what they have changed since they last
    saved. The map, an assistant and a script each have their own, as each has
    its own undo.
    """
    # For the undo log and the history: whose change this is, and how it came in (the map, an assistant).
    through = getattr(author, "through", "") or ""
    conn.execute("SELECT set_config('stiflyt2.author', %s, true), set_config('stiflyt2.through', %s, true)",
                 (str(author), through))
    row = conn.execute(
        """SELECT id FROM core.changeset WHERE author = %s AND coalesce(through, '') = %s AND state = 'open'
           ORDER BY created_at DESC LIMIT 1""", (str(author), through)).fetchone()
    if row:
        return str(row[0])
    return str(conn.execute(
        "INSERT INTO core.changeset (title, author, through) VALUES (%s, %s, nullif(%s, '')) RETURNING id",
        (f"Changes {date.today().isoformat()}", str(author), through)).fetchone()[0])


def changeset_status(conn, author: str, through: str | None = None) -> dict | None:
    """The open changeset of an author for one way in (as `current_changeset` finds it), with how much is in it."""
    found = _rows(conn, """
        SELECT c.id, c.title, c.created_at,
               (SELECT count(*) FROM history.event e WHERE e.changeset_id = c.id)::int AS changes
        FROM core.changeset c WHERE c.author = %(a)s AND coalesce(c.through, '') = %(t)s AND c.state = 'open'
        ORDER BY c.created_at DESC LIMIT 1""",
        {"a": str(author), "t": through if through is not None else getattr(author, "through", "") or ""})
    return found[0] if found else None


def close_changeset(conn, author: str) -> None:
    conn.execute("""UPDATE core.changeset SET state = 'closed', closed_at = now()
                    WHERE author = %s AND state = 'open'""", (author,))


def _event(conn, author: str, type_: str, place_id: str, payload: dict) -> None:
    conn.execute(
        """INSERT INTO history.event (changeset_id, author, type, subject_type, subject_id, payload)
           VALUES (%s, %s, %s, 'place', %s, %s)""",
        (current_changeset(conn, author), author, type_, place_id, json.dumps(payload)))


def _place(conn, place_id: str, version: int | None = None) -> dict:
    """The place, locked for change. Raises if it is gone or was changed since `version`."""
    found = _rows(conn, """SELECT id, name, kind, primary_node_id, status, version
                           FROM core.place WHERE id = %(id)s FOR UPDATE""", {"id": place_id})
    if not found or found[0]["status"] != "confirmed":
        raise NotFound("no such place")
    if version is not None and found[0]["version"] != version:
        raise Conflict("the place has been changed since you loaded it")
    return found[0]


def _touch(conn, place_id: str, author: str) -> None:
    """Count the change. A change made by a person also means the place has been looked at."""
    conn.execute(
        """UPDATE core.place SET version = version + 1, updated_at = now(),
                  reviewed = reviewed OR %s WHERE id = %s""", (author != AUTOMATIC, place_id))


def _connect(conn, author: str, node_id: str, primary_id: str) -> None:
    """A connector from an access point to the primary one, as a straight line to begin with."""
    edge = conn.execute("INSERT INTO core.edge DEFAULT VALUES RETURNING id").fetchone()[0]
    revision = conn.execute(
        """INSERT INTO core.edge_revision
               (edge_id, rev_no, geom, a_node, b_node, kind, origin, changeset_id, author)
           SELECT %(e)s, 1, ST_MakeLine(a.geom, b.geom), a.id, b.id, 'connector', 'derived',
                  %(c)s, %(u)s
           FROM core.node a, core.node b WHERE a.id = %(a)s AND b.id = %(b)s RETURNING id""",
        {"e": edge, "a": node_id, "b": primary_id, "c": current_changeset(conn, author),
         "u": author}).fetchone()[0]
    conn.execute("UPDATE core.edge SET current_revision_id = %s WHERE id = %s", (revision, edge))


def _retire_connectors(conn, node_id: str) -> None:
    """Retire the place's own links at an access point. A link that a route walks is left alone."""
    conn.execute(
        """UPDATE core.edge e SET status = 'retired', version = version + 1
           FROM core.edge_revision r
           WHERE r.id = e.current_revision_id AND e.status = 'active' AND r.kind = 'connector'
             AND %s IN (r.a_node, r.b_node)
             AND NOT EXISTS (SELECT 1 FROM core.route_leg l
                             JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                             WHERE l.edge_id = e.id)""", (node_id,))


def _keep_connected(conn, author: str, place_id: str) -> None:
    """Give every access point that cannot reach the primary one a connector to it.

    A place must stay connected whatever is removed or re-pointed: taking out
    the access point the others were joined to would otherwise strand them.
    """
    primary = conn.execute("SELECT primary_node_id FROM core.place WHERE id = %s",
                           (place_id,)).fetchone()[0]
    nodes = {str(n) for (n,) in conn.execute(
        "SELECT id FROM core.node WHERE place_id = %s AND status = 'active'", (place_id,))}
    links = [(str(a), str(b)) for a, b in conn.execute(
        """SELECT r.a_node, r.b_node FROM core.edge e
           JOIN core.edge_revision r ON r.id = e.current_revision_id
           WHERE e.status = 'active' AND r.kind = 'connector'
             AND r.a_node = ANY(%(n)s::uuid[]) AND r.b_node = ANY(%(n)s::uuid[])""",
        {"n": list(nodes)})]
    reached, frontier = {str(primary)}, [str(primary)]
    while frontier:
        here = frontier.pop()
        for a, b in links:
            there = b if a == here else a if b == here else None
            if there and there not in reached:
                reached.add(there)
                frontier.append(there)
    for node_id in sorted(nodes - reached):
        _connect(conn, author, node_id, str(primary))


def fingerprints(conn, anchor_id: str | None = None) -> dict[str, str]:
    """What each named proposal looks like now, to tell later whether it has changed."""
    return dict(conn.execute("""
        SELECT a.id, md5(a.kind || '|' || coalesce(a.name, '') || '|' || round(ST_X(a.geom)) || ','
                         || round(ST_Y(a.geom)) || '|' || coalesce(s.spots, ''))
        FROM source_index.anchor a
        LEFT JOIN (SELECT anchor_id, string_agg(spot, ';' ORDER BY spot) AS spots
                   FROM (SELECT e.anchor_id,
                                round(ST_X(ST_Centroid(ST_Collect(e.geom)))) || ','
                                || round(ST_Y(ST_Centroid(ST_Collect(e.geom)))) || ':'
                                || string_agg(DISTINCT r.label, ',' ORDER BY r.label) AS spot
                         FROM source_index.route_end e
                         JOIN source_index.route r ON r.id = e.route_id
                         GROUP BY e.anchor_id, e.spot) x
                   GROUP BY 1) s ON s.anchor_id = a.id
        WHERE (%(id)s::text IS NULL AND a.named) OR a.id = %(id)s""", {"id": anchor_id}).fetchall())


def adopt(conn, anchor_id: str, author: str, name: str | None = None) -> str:
    """Make a proposed anchor an owned place, with a node for each spot where routes end.

    The name is the anchor's unless one is given: a junction or dead end has
    only the nearest place name, which may not be what the spot is called.

    The access point nearest the anchor becomes the primary one and the others
    get a connector to it. What everything was made from is recorded by value.
    A place made by a person counts as reviewed; one made by default does not.
    """
    anchor = _rows(conn, "SELECT id, kind, name FROM source_index.anchor WHERE id = %(id)s",
                   {"id": anchor_id})
    if not anchor:
        raise NotFound("no such anchor")
    if conn.execute(
            """SELECT 1 FROM core.source_reference sr JOIN core.place p ON p.id = sr.subject_id
               WHERE sr.subject_type = 'place' AND sr.ref_type = 'adopted_from'
                 AND sr.detail->>'anchor_id' = %s AND p.status = 'confirmed'""",
            (anchor_id,)).fetchone():
        raise Conflict("this anchor has already been adopted")
    a = anchor[0]
    kind = a["kind"] if a["kind"] in ("hut", "parking") else "other"
    name = ((name or "").strip() or a["name"]
            or {"hut": "Hut", "parking": "Parking"}.get(kind, "Unnamed place"))
    place_id = str(conn.execute(
        """INSERT INTO core.place (name, kind, geom, reviewed)
           SELECT %s, %s, geom, %s FROM source_index.anchor WHERE id = %s RETURNING id""",
        (name, kind, author != AUTOMATIC, anchor_id)).fetchone()[0])

    spots = _rows(conn, """
        SELECT e.spot, ST_Centroid(ST_Collect(e.geom)) AS geom,
               array_agg(DISTINCT r.label ORDER BY r.label) AS routes
        FROM source_index.route_end e JOIN source_index.route r ON r.id = e.route_id
        WHERE e.anchor_id = %(id)s GROUP BY e.spot
        ORDER BY ST_Distance(ST_Centroid(ST_Collect(e.geom)),
                             (SELECT geom FROM source_index.anchor WHERE id = %(id)s)), e.spot""",
        {"id": anchor_id})
    primary = None
    for spot in spots:
        # A node already standing at the spot, from a route made earlier, becomes
        # the access point: the route is then attached to the place.
        standing = conn.execute(
            """UPDATE core.node SET place_id = %(p)s, version = version + 1
               WHERE id = (SELECT id FROM core.node
                           WHERE status = 'active' AND place_id IS NULL
                             AND ST_DWithin(geom, %(g)s::geometry, 5)
                           ORDER BY geom <-> %(g)s::geometry LIMIT 1)
               RETURNING id""", {"p": place_id, "g": spot["geom"]}).fetchone()
        node_id = str(standing[0] if standing else conn.execute(
            "INSERT INTO core.node (geom, place_id) VALUES (%s, %s) RETURNING id",
            (spot["geom"], place_id)).fetchone()[0])
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type, detail)
               VALUES ('node', %s, 'turrutebasen', %s, 'adopted_from', %s)""",
            (node_id, f"{anchor_id}#{spot['spot']}",
             json.dumps({"anchor_id": anchor_id, "routes": spot["routes"]})))
        if primary is None:
            primary = node_id
            conn.execute("UPDATE core.place SET primary_node_id = %s WHERE id = %s",
                         (primary, place_id))
        else:
            _connect(conn, author, node_id, primary)

    source, _, external = anchor_id.partition(":")
    source_key = {"place": "stedsnavn", "info": "turrutebasen"}.get(source, "source_index")
    conn.execute(
        """INSERT INTO core.source_reference
               (subject_type, subject_id, source_key, external_id, snapshot_id, ref_type, detail)
           VALUES ('place', %s, %s, %s,
                   (SELECT snapshot_id FROM import_control.active_snapshot
                    WHERE source_key = %s AND scope = 'NO'), 'adopted_from', %s)""",
        (place_id, source_key, external, source_key,
         json.dumps({"anchor_id": anchor_id, "name": a["name"], "kind": a["kind"],
                     "fingerprint": fingerprints(conn, anchor_id)[anchor_id]})))
    _event(conn, author, "place.adopted", place_id,
           {"anchor_id": anchor_id, "name": name, "kind": kind, "access_points": len(spots)})
    return place_id


NEAR_HUT_M = 300     # a hut in Stedsnavn this near a junction lends the place its name


def name_near(conn, node_id: str) -> dict | None:
    """The hut nearest a junction, as Stedsnavn has it, if one lies near: what a place there would be called."""
    found = _rows(conn, """
        SELECT s.name, s.place_id, round(ST_Distance(s.geom, n.geom))::int AS metres
        FROM core.node n JOIN source_index.place s
          ON ST_DWithin(s.geom, n.geom, %(m)s) AND s.type = 'turisthytte' AND NOT s.alternative
        WHERE n.id = %(id)s ORDER BY s.geom <-> n.geom LIMIT 1""", {"id": node_id, "m": NEAR_HUT_M})
    return found[0] if found else None


# What an end is called after when no hut lies near: the kinds of named thing a route runs to,
# best first, each with how far from the end its name may lie (metres).
NAMED_AFTER = (("hotell", 150, "hotel"), ("campingplass", 150, "camp site"), ("parkeringsplass", 150, "car park"),
               ("fjell", 200, "summit"), ("topp", 200, "summit"), ("seterStøl", 150, "summer farm"),
               ("gard", 150, "farm"), ("bruk", 150, "farm"), ("fritidsbolig", 150, "cabin"),
               ("tuft", 60, "old site"), ("bru", 60, "bridge"))
ROAD_END_M = 25      # an end this near a road ends on the road
BY_M = 400           # a road name is told apart by a named thing this near
LONG_ROADS = ("E", "R", "F")    # a road of these categories is too long for its name to say where


GLACIER_NAMED_M = 3000   # a glacier's name this far from the ice a route crosses is the glacier meant
GLACIER_TYPES = ("isbre",)      # what Stedsnavn calls a glacier
_SIDES = (("nord", "sør"), ("øst", "vest"))


def glacier_edge(conn, node_id: str) -> dict | None:
    """Where a route steps onto the ice, or off it, at this junction: what the place there would be called.

    Named after the glacier whose name lies nearest the stretch over the ice,
    with the side of the crossing this edge is on, since a crossing has two
    edges and a name is one place's: "Fannaråkbreen brekant nord".
    """
    found = _rows(conn, """
        SELECT g.id, ST_X(n.geom) - ST_X(o.geom) AS east, ST_Y(n.geom) - ST_Y(o.geom) AS north,
               (SELECT s.name FROM source_index.place s
                WHERE s.type = ANY(%(types)s) AND NOT s.alternative AND ST_DWithin(s.geom, g.geom, %(far)s)
                ORDER BY s.geom <-> g.geom LIMIT 1) AS glacier
        FROM core.node n
        JOIN core.edge_revision g ON n.id IN (g.a_node, g.b_node) AND g.kind = 'glacier'
        JOIN core.edge e ON e.current_revision_id = g.id AND e.status = 'active'
        JOIN core.node o ON o.id = CASE WHEN g.a_node = n.id THEN g.b_node ELSE g.a_node END
        WHERE n.id = %(id)s AND EXISTS (SELECT 1 FROM core.route_leg l JOIN core.route t ON t.id = l.route_id
                                        AND t.status = 'active' WHERE l.edge_id = e.id)
          -- the ice ends here: no other stretch over ice goes on from this junction
          AND NOT EXISTS (SELECT 1 FROM core.edge_revision x JOIN core.edge xe ON xe.current_revision_id = x.id
                          AND xe.status = 'active' WHERE n.id IN (x.a_node, x.b_node) AND x.kind = 'glacier'
                            AND x.id <> g.id)
        LIMIT 1""", {"id": node_id, "far": GLACIER_NAMED_M, "types": list(GLACIER_TYPES)})
    if not found or not found[0]["glacier"]:
        return None
    east, north = found[0]["east"], found[0]["north"]
    side = _SIDES[0][north < 0] if abs(north) >= abs(east) else _SIDES[1][east < 0]
    return {"name": f"{found[0]['glacier']} brekant {side}", "kind": "glacier_edge", "stedsnavn_id": None,
            "type": "glacier edge", "metres": 0, "what": "the edge of the glacier",
            "after": f"the edge of {found[0]['glacier']}, on its {side} side as the route crosses"}


def suggest(conn, node_id: str) -> dict | None:
    """What a place at this junction would be called, and after what. None when nothing there has a name.

    A hut first. Then a named thing of a kind routes run to: a summit, a farm, a
    bridge. Then the road the route ends on, told apart by the nearest named
    thing where the road is long or its name is taken.
    """
    hut = name_near(conn, node_id)
    if hut:
        return {"name": hut["name"], "kind": "hut", "stedsnavn_id": hut["place_id"], "type": "hut",
                "metres": hut["metres"], "what": "the hut", "after": f"the hut, {hut['metres']} m away"}
    ice = glacier_edge(conn, node_id)
    if ice:
        return ice
    thing = _rows(conn, """
        SELECT s.name, s.place_id, k.word, round(ST_Distance(s.geom, n.geom))::int AS metres
        FROM core.node n
        JOIN source_index.place s ON ST_DWithin(s.geom, n.geom, %(far)s) AND NOT s.alternative
        JOIN unnest(%(types)s::text[], %(reach)s::int[], %(words)s::text[]) WITH ORDINALITY
             AS k(type, reach, word, rank) ON k.type = s.type AND ST_DWithin(s.geom, n.geom, k.reach)
        WHERE n.id = %(id)s ORDER BY k.rank, s.geom <-> n.geom, s.name LIMIT 1""",
        {"id": node_id, "far": max(k[1] for k in NAMED_AFTER), "types": [k[0] for k in NAMED_AFTER],
         "reach": [k[1] for k in NAMED_AFTER], "words": [k[2] for k in NAMED_AFTER]})
    if thing:
        t = thing[0]
        return {"name": t["name"], "kind": "other", "stedsnavn_id": t["place_id"], "type": t["word"],
                "metres": t["metres"], "what": f"the {t['word']}",
                "after": f"the {t['word']}, {t['metres']} m away"}
    road = _rows(conn, """
        SELECT r.name, r.category,
               EXISTS (SELECT 1 FROM core.place p WHERE p.status = 'confirmed' AND lower(p.name) = lower(r.name)) AS taken,
               (SELECT s.name FROM source_index.place s
                WHERE ST_DWithin(s.geom, n.geom, %(by)s) AND NOT s.alternative AND s.type <> 'adressenavn'
                  AND lower(s.name) <> lower(r.name)
                ORDER BY s.geom <-> n.geom LIMIT 1) AS by
        FROM core.node n JOIN source_index.road r ON ST_DWithin(r.geom, n.geom, %(m)s) AND r.name IS NOT NULL
        WHERE n.id = %(id)s ORDER BY r.geom <-> n.geom LIMIT 1""", {"id": node_id, "m": ROAD_END_M, "by": BY_M})
    if road:
        r = road[0]
        apart = r["by"] and (r["category"] in LONG_ROADS or r["taken"])
        return {"name": f"{r['name']} ved {r['by']}" if apart else r["name"], "kind": "other",
                "stedsnavn_id": None, "type": "road", "metres": 0, "what": "the road",
                "after": "the road it ends on"}
    return None


def _named(conn, name: str, stedsnavn_id=None) -> str | None:
    """The place that already has this name, or is named after this entry in Stedsnavn."""
    found = conn.execute(
        """SELECT p.id::text FROM core.place p WHERE p.status = 'confirmed' AND (lower(p.name) = lower(%s)
               OR EXISTS (SELECT 1 FROM core.source_reference sr
                          WHERE sr.subject_type = 'place' AND sr.subject_id = p.id
                            AND sr.source_key = 'stedsnavn' AND sr.external_id = %s)) LIMIT 1""",
        (name, str(stedsnavn_id) if stedsnavn_id else "")).fetchone()
    return found[0] if found else None


def at_node(conn, node_id: str, author: str, name: str | None = None) -> str:
    """Make a place where a route ends or passes, with that junction as its arrival point.

    Named as given. With no name given it is named after what lies there
    (`suggest`), and refused when nothing there has a name or another place
    has that name already. A hut near lends the place its position either way.
    """
    node = conn.execute("SELECT place_id FROM core.node WHERE id = %s AND status = 'active' FOR UPDATE",
                        (node_id,)).fetchone()
    if not node:
        raise NotFound("no such junction")
    if node[0]:
        raise Conflict("that junction is already an arrival point of a place")
    given = (name or "").strip()
    after = suggest(conn, node_id)
    if given and after and after["kind"] == "other" and given.lower() != after["name"].lower():
        after = None        # named otherwise by hand: what lies near has no say
    kind = after["kind"] if after else "other"      # a hut or the edge of a glacier is that whatever it is called
    name = given or (after["name"] if after else "")
    if not name:
        raise Invalid("a place needs a name")
    if not given and _named(conn, name, after["stedsnavn_id"]):
        raise Conflict(f"there is already a place called {name}: name this one by hand")
    hut = after if after and after["kind"] == "hut" else None
    place_id = str(conn.execute(
        """INSERT INTO core.place (name, kind, geom, reviewed, primary_node_id)
           SELECT %s, %s, coalesce((SELECT s.geom FROM source_index.place s WHERE s.place_id = %s LIMIT 1),
                                   n.geom), true, n.id
           FROM core.node n WHERE n.id = %s RETURNING id""",
        (name, kind, hut["stedsnavn_id"] if hut else None, node_id)).fetchone()[0])
    conn.execute("UPDATE core.node SET place_id = %s, version = version + 1 WHERE id = %s",
                 (place_id, node_id))
    if after and after["stedsnavn_id"]:
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type, detail)
               VALUES ('place', %s, 'stedsnavn', %s, 'adopted_from', %s)""",
            (place_id, str(after["stedsnavn_id"]), json.dumps({"name": after["name"], "kind": after["type"]})))
    _event(conn, author, "place.adopted", place_id,
           {"name": name, "kind": kind, "access_points": 1, "at_junction": node_id,
            **({"named_after": after["after"]} if after and not given else {})})
    return place_id


ARRIVAL_M = 150      # an end this near what a place is named after is one of its arrival points


def take_in(conn, place_id: str, node_id: str, author: str) -> None:
    """Make a junction that is there already an arrival point of the place, linked to its primary one."""
    place = _place(conn, place_id)
    node = conn.execute("SELECT place_id FROM core.node WHERE id = %s AND status = 'active' FOR UPDATE",
                        (node_id,)).fetchone()
    if not node:
        raise NotFound("no such junction")
    if node[0]:
        raise Conflict("that junction is already an arrival point of a place")
    conn.execute("UPDATE core.node SET place_id = %s, version = version + 1 WHERE id = %s", (place_id, node_id))
    if place["primary_node_id"] is None:
        conn.execute("UPDATE core.place SET primary_node_id = %s WHERE id = %s", (node_id, place_id))
    else:
        _connect(conn, author, node_id, str(place["primary_node_id"]))
    _event(conn, author, "place.access_point_added", place_id, {"node_id": node_id, "was_a_junction": True})
    _touch(conn, place_id, author)


def update(conn, place_id: str, version: int, author: str, *, name: str | None = None,
           kind: str | None = None, lon: float | None = None, lat: float | None = None,
           primary_node_id: str | None = None) -> None:
    """Rename a place, change its kind, move its reference point or choose its primary access point."""
    place = _place(conn, place_id, version)
    if name is not None and name.strip() != place["name"]:
        if not name.strip():
            raise Invalid("a place needs a name")
        conn.execute("UPDATE core.place SET name = %s WHERE id = %s", (name.strip(), place_id))
        identity.remember(conn, "place", place_id, "name", place["name"])
        _event(conn, author, "place.renamed", place_id,
               {"from": place["name"], "to": name.strip()})
    if kind is not None and kind != place["kind"]:
        if kind not in KINDS:
            raise Invalid(f"kind must be one of {', '.join(KINDS)}")
        conn.execute("UPDATE core.place SET kind = %s WHERE id = %s", (kind, place_id))
        _event(conn, author, "place.kind_changed", place_id, {"from": place["kind"], "to": kind})
    if lon is not None and lat is not None:
        moved = conn.execute(
            f"""UPDATE core.place SET geom = {_POINT} WHERE id = %(id)s
                RETURNING round(ST_Distance(geom, (SELECT geom FROM core.place
                                                   WHERE id = %(id)s)))::int""",
            {"lon": lon, "lat": lat, "id": place_id}).fetchone()[0]
        _event(conn, author, "place.moved", place_id, {"lon": lon, "lat": lat, "metres": moved})
    if primary_node_id is not None and primary_node_id != str(place["primary_node_id"]):
        if not conn.execute(
                "SELECT 1 FROM core.node WHERE id = %s AND place_id = %s AND status = 'active'",
                (primary_node_id, place_id)).fetchone():
            raise Invalid("the primary access point must be one of the place's own")
        conn.execute("UPDATE core.place SET primary_node_id = %s WHERE id = %s",
                     (primary_node_id, place_id))
        _event(conn, author, "place.primary_changed", place_id,
               {"from": str(place["primary_node_id"]), "to": primary_node_id})
        _keep_connected(conn, author, place_id)
    _touch(conn, place_id, author)


def review(conn, place_id: str, version: int, author: str) -> None:
    """A person has looked at the place and it is right as it is."""
    _place(conn, place_id, version)
    _event(conn, author, "place.reviewed", place_id, {})
    _touch(conn, place_id, author)


def make_default(conn, area: str | None = None) -> dict:
    """Make a place of every proposed hut and parking place that is not one yet.

    Places made here are marked as not reviewed. While a place is unreviewed
    it follows its proposal: if the proposal has changed, the place is made
    again. A place a person has reviewed or changed is never touched, and a
    proposal whose place a person withdrew is left alone.

    area: only anchors where routes with this three-letter code prefix end.
    """
    current = fingerprints(conn)
    if area:
        inside = {a for (a,) in conn.execute(
            """SELECT DISTINCT e.anchor_id FROM source_index.route_end e
               JOIN source_index.route r ON r.id = e.route_id WHERE r.area_prefix = lower(%s)""",
            (area,))}
        current = {a: f for a, f in current.items() if a in inside}
    places = {anchor: (str(place_id), reviewed, fingerprint) for anchor, place_id, reviewed, fingerprint
              in conn.execute("""
        SELECT sr.detail->>'anchor_id', p.id, p.reviewed, sr.detail->>'fingerprint'
        FROM core.source_reference sr JOIN core.place p ON p.id = sr.subject_id
        WHERE sr.subject_type = 'place' AND sr.ref_type = 'adopted_from'
          AND p.status = 'confirmed'""")}
    withdrawn = {a for (a,) in conn.execute("""
        SELECT DISTINCT sr.detail->>'anchor_id'
        FROM history.event e
        JOIN core.source_reference sr ON sr.subject_type = 'place' AND sr.subject_id = e.subject_id
                                     AND sr.ref_type = 'adopted_from'
        WHERE e.type IN ('place.retired', 'place.merged_into') AND e.author <> %s""", (AUTOMATIC,))}
    done = {"made": 0, "made again": 0, "kept": 0, "left withdrawn": 0, "withdrawn": 0}
    for anchor_id, fingerprint in sorted(current.items()):
        if anchor_id in places:
            place_id, reviewed, was = places[anchor_id]
            if reviewed or was == fingerprint:
                done["kept"] += 1
                continue
            retire(conn, place_id, AUTOMATIC)
            adopt(conn, anchor_id, AUTOMATIC)
            done["made again"] += 1
        elif anchor_id in withdrawn:
            done["left withdrawn"] += 1
        else:
            adopt(conn, anchor_id, AUTOMATIC)
            done["made"] += 1
    if not area:
        # An unreviewed place whose proposal no longer exists goes with it.
        for anchor_id, (place_id, reviewed, _) in places.items():
            if anchor_id not in current and not reviewed and anchor_id.startswith(("place:", "info:")):
                retire(conn, place_id, AUTOMATIC)
                done["withdrawn"] += 1
    close_changeset(conn, AUTOMATIC)
    return done


def add_access_point(conn, place_id: str, lon: float, lat: float, author: str) -> str:
    """A new access point at a position, connected to the primary one."""
    place = _place(conn, place_id)
    node_id = str(conn.execute(
        f"INSERT INTO core.node (geom, place_id) VALUES ({_POINT}, %(p)s) RETURNING id",
        {"lon": lon, "lat": lat, "p": place_id}).fetchone()[0])
    if place["primary_node_id"] is None:
        conn.execute("UPDATE core.place SET primary_node_id = %s WHERE id = %s",
                     (node_id, place_id))
    else:
        _connect(conn, author, node_id, str(place["primary_node_id"]))
    _event(conn, author, "place.access_point_added", place_id,
           {"node_id": node_id, "lon": lon, "lat": lat})
    _touch(conn, place_id, author)
    return node_id


def remove_access_point(conn, place_id: str, node_id: str, author: str) -> None:
    """The node stops being part of the place. It stays in the graph as an ordinary node."""
    place = _place(conn, place_id)
    if not conn.execute(
            "SELECT 1 FROM core.node WHERE id = %s AND place_id = %s AND status = 'active'",
            (node_id, place_id)).fetchone():
        raise NotFound("no such access point at this place")
    if node_id == str(place["primary_node_id"]):
        raise Invalid("choose another primary access point before removing this one")
    conn.execute("UPDATE core.node SET place_id = NULL, version = version + 1 WHERE id = %s",
                 (node_id,))
    _retire_connectors(conn, node_id)
    _keep_connected(conn, author, place_id)
    _event(conn, author, "place.access_point_removed", place_id, {"node_id": node_id})
    _touch(conn, place_id, author)


def retire(conn, place_id: str, author: str) -> None:
    """Withdraw a place, its access points and its connectors. Its anchor can be adopted again."""
    _place(conn, place_id)
    for (node_id,) in conn.execute(
            "SELECT id FROM core.node WHERE place_id = %s AND status = 'active'",
            (place_id,)).fetchall():
        _retire_connectors(conn, str(node_id))
    # A node that a route runs through stays in the graph; it only leaves the place.
    conn.execute(
        """UPDATE core.node n SET version = version + 1, place_id = NULL
           WHERE n.place_id = %s AND n.status = 'active' AND EXISTS (
               SELECT 1 FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
               WHERE e.status = 'active' AND r.kind <> 'connector' AND n.id IN (r.a_node, r.b_node))""",
        (place_id,))
    conn.execute("""UPDATE core.node SET status = 'retired', version = version + 1
                    WHERE place_id = %s AND status = 'active'""", (place_id,))
    conn.execute("""UPDATE core.place SET status = 'retired', version = version + 1,
                    updated_at = now() WHERE id = %s""", (place_id,))
    _event(conn, author, "place.retired", place_id, {})


NEARBY_M = 300          # how far round a place its loose ends and its doubles are looked for
SAME_NAME_M = 3000      # a place of the same name this near is most likely the same place


def merge(conn, place_id: str, version: int, other_id: str, author: str) -> None:
    """Make two places one: the other's arrival points become this place's, and the other is withdrawn.

    This place keeps its name, its kind and its building mark; the other's
    name is kept as one it has gone by. No junction is moved and no route is
    changed: the routes that arrived at the other now arrive at this one.
    What pointed at the other is led on to this one (`core.succession`).
    """
    if str(place_id) == str(other_id):
        raise Invalid("that is the same place")
    place, other = _place(conn, place_id, version), _place(conn, other_id)
    nodes = [str(n) for (n,) in conn.execute(
        "SELECT id FROM core.node WHERE place_id = %s AND status = 'active' ORDER BY id", (other_id,))]
    for node_id in nodes:
        _retire_connectors(conn, node_id)
    conn.execute("UPDATE core.node SET place_id = %s, version = version + 1 WHERE place_id = %s AND status = 'active'",
                 (place_id, other_id))
    if place["primary_node_id"] is None and other["primary_node_id"] is not None:
        conn.execute("UPDATE core.place SET primary_node_id = %s WHERE id = %s", (other["primary_node_id"], place_id))
    conn.execute("""UPDATE core.place SET status = 'retired', version = version + 1, updated_at = now()
                    WHERE id = %s""", (other_id,))
    _keep_connected(conn, author, place_id)
    identity.succeed(conn, "place", str(other_id), [str(place_id)], "joined")
    if other["name"].strip().lower() != place["name"].strip().lower():
        identity.remember(conn, "place", str(place_id), "name", other["name"])
    _event(conn, author, "place.merged", place_id,
           {"taken_in": str(other_id), "name": other["name"], "kind": other["kind"], "access_points": len(nodes)})
    _event(conn, author, "place.merged_into", other_id, {"into": str(place_id)})
    _touch(conn, place_id, author)


def around(conn, place_id: str) -> dict:
    """What lies round a place and may belong to it.

    `ends`: junctions within 300 m where a route ends and that are not arrival
    points of this place, each with the routes there (and whether each ends
    there or passes), and the place it belongs to if it belongs to another.
    `places`: other places within 300 m, or of the same name within 3 km.
    Measured from where the routes arrive (the primary arrival point).
    """
    if not conn.execute("SELECT 1 FROM core.place WHERE id = %s AND status = 'confirmed'", (place_id,)).fetchone():
        raise NotFound("no such place")
    here = """(SELECT p.id, p.name, coalesce(n.geom, p.geom) AS at FROM core.place p
               LEFT JOIN core.node n ON n.id = p.primary_node_id WHERE p.id = %(id)s) here"""
    ends = _rows(conn, f"""
        SELECT n.id::text, {_LON.format(g="n.geom")} AS lon, {_LAT.format(g="n.geom")} AS lat,
               round(ST_Distance(n.geom, here.at))::int AS metres, n.place_id::text AS place_id,
               (SELECT o.name FROM core.place o WHERE o.id = n.place_id AND o.status = 'confirmed') AS place,
               t.routes
        FROM {here}, core.node n, LATERAL (
            SELECT jsonb_agg(jsonb_build_object('id', r.id, 'code', r.code, 'version', r.version,
                                                'ends_here', r.legs = 1) ORDER BY r.code) AS routes,
                   bool_or(r.legs = 1) AS ends
            FROM (SELECT rt.id, rt.code, rt.version, count(*) AS legs
                  FROM core.route_leg l JOIN core.route rt ON rt.id = l.route_id AND rt.status = 'active'
                  JOIN core.edge e ON e.id = l.edge_id AND e.status = 'active'
                  JOIN core.edge_revision v ON v.id = e.current_revision_id
                  WHERE n.id IN (v.a_node, v.b_node) AND v.a_node <> v.b_node
                  GROUP BY rt.id, rt.code, rt.version) r) t
        WHERE n.status = 'active' AND n.place_id IS DISTINCT FROM here.id
          AND ST_DWithin(n.geom, here.at, %(near)s) AND t.ends
        ORDER BY metres, n.id""", {"id": place_id, "near": NEARBY_M})
    others = _rows(conn, f"""
        SELECT o.id::text, o.name, o.kind, o.reviewed, round(ST_Distance(coalesce(pn.geom, o.geom), here.at))::int AS metres,
               lower(o.name) = lower(here.name) AS same_name,
               (SELECT count(*) FROM core.node x WHERE x.place_id = o.id AND x.status = 'active')::int AS access_points
        FROM {here}, core.place o LEFT JOIN core.node pn ON pn.id = o.primary_node_id
        WHERE o.status = 'confirmed' AND o.id <> here.id
          AND (ST_DWithin(coalesce(pn.geom, o.geom), here.at, %(near)s)
               OR (lower(o.name) = lower(here.name) AND ST_DWithin(coalesce(pn.geom, o.geom), here.at, %(same)s)))
        ORDER BY metres, o.id""", {"id": place_id, "near": NEARBY_M, "same": SAME_NAME_M})
    return {"ends": ends, "places": others}


def detail(conn, place_id: str) -> dict:
    found = _rows(conn, f"""
        SELECT p.id, p.name, p.kind, p.version, p.primary_node_id, p.reviewed,
               {_LON.format(g="p.geom")} AS lon, {_LAT.format(g="p.geom")} AS lat,
               (SELECT sr.detail->>'anchor_id' FROM core.source_reference sr
                WHERE sr.subject_type = 'place' AND sr.subject_id = p.id
                  AND sr.ref_type = 'adopted_from' LIMIT 1) AS adopted_from
        FROM core.place p WHERE p.id = %(id)s AND p.status = 'confirmed'""", {"id": place_id})
    if not found:
        raise NotFound("no such place")
    place = found[0]
    place["access_points"] = _rows(conn, f"""
        SELECT n.id, n.id = p.primary_node_id AS is_primary, n.name,
               {_LON.format(g="n.geom")} AS lon, {_LAT.format(g="n.geom")} AS lat,
               round(ST_Distance(n.geom, p.geom))::int AS from_reference_m,
               round(ST_Distance(n.geom, (SELECT geom FROM core.node
                                          WHERE id = p.primary_node_id)))::int AS from_primary_m,
               coalesce((SELECT jsonb_agg(DISTINCT rt.code ORDER BY rt.code) FROM core.route_leg l
                         JOIN core.route rt ON rt.id = l.route_id AND rt.status = 'active'
                         JOIN core.edge e ON e.id = l.edge_id AND e.status = 'active'
                         JOIN core.edge_revision v ON v.id = e.current_revision_id
                         WHERE n.id IN (v.a_node, v.b_node)),
                        (SELECT sr.detail->'routes' FROM core.source_reference sr
                         WHERE sr.subject_type = 'node' AND sr.subject_id = n.id LIMIT 1),
                        '[]') AS routes
        FROM core.node n JOIN core.place p ON p.id = n.place_id
        WHERE n.place_id = %(id)s AND n.status = 'active'
        ORDER BY (n.id = p.primary_node_id) DESC, from_reference_m, n.id""", {"id": place_id})
    place["connectors"] = _rows(conn, """
        SELECT e.id, r.origin, round(ST_Length(r.geom))::int AS length_m
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.node n ON n.id = r.a_node
        WHERE e.status = 'active' AND r.kind = 'connector' AND n.place_id = %(id)s
        ORDER BY length_m, e.id""", {"id": place_id})
    place["earlier"] = identity.earlier(conn, "place", place_id)
    place["history"] = _rows(conn, """
        SELECT at, author, type, payload FROM history.event
        WHERE subject_type = 'place' AND subject_id = %(id)s ORDER BY seq DESC LIMIT 30""",
        {"id": place_id})
    return place


def collection(conn) -> dict:
    """Every confirmed place with its other access points and connectors, as GeoJSON for the map.

    A place is one point on the map: where its routes arrive, which is its
    primary access point. Only further access points are separate features.
    """
    rows = conn.execute("""
        SELECT ST_AsGeoJSON(ST_Transform(coalesce(pn.geom, p.geom), 4326), 6)::json,
               jsonb_build_object('type', 'place', 'id', p.id, 'name', p.name, 'kind', p.kind,
                   'reviewed', p.reviewed,
                   'access_points', (SELECT count(*) FROM core.node n
                                     WHERE n.place_id = p.id AND n.status = 'active'),
                   'adopted_from', (SELECT sr.detail->>'anchor_id' FROM core.source_reference sr
                                    WHERE sr.subject_type = 'place' AND sr.subject_id = p.id
                                      AND sr.ref_type = 'adopted_from' LIMIT 1),
                   'taken_in', (SELECT coalesce(jsonb_agg(sr.detail->>'anchor_id'), '[]')
                                FROM core.succession s JOIN core.source_reference sr
                                  ON sr.subject_type = 'place' AND sr.subject_id = s.old_id
                                 AND sr.ref_type = 'adopted_from' AND sr.detail ? 'anchor_id'
                                WHERE s.subject_type = 'place' AND s.new_id = p.id))
        FROM core.place p LEFT JOIN core.node pn ON pn.id = p.primary_node_id
        WHERE p.status = 'confirmed'
        UNION ALL
        SELECT ST_AsGeoJSON(ST_Transform(n.geom, 4326), 6)::json,
               jsonb_build_object('type', 'access', 'id', n.id, 'place_id', n.place_id)
        FROM core.node n JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.status = 'active' AND n.id IS DISTINCT FROM p.primary_node_id
        UNION ALL
        SELECT ST_AsGeoJSON(ST_Transform(r.geom, 4326), 6)::json,
               jsonb_build_object('type', 'connector', 'id', e.id, 'place_id', n.place_id,
                                  'origin', r.origin)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.node n ON n.id = r.a_node
        WHERE e.status = 'active' AND r.kind = 'connector' AND n.place_id IS NOT NULL""").fetchall()
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": g, "properties": p} for g, p in rows]}
