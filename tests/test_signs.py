"""Signs: worked out from the routes, with what a person decides laid over, and orders that keep what was ordered."""
import itertools
from io import BytesIO

import psycopg
import pytest
from conftest import role_dsn, scripts_of
from fastapi.testclient import TestClient

from stiflyt2.api.app import create_app
from stiflyt2.domain import places, routes
from stiflyt2.domain.places import Conflict, Invalid, NotFound
from stiflyt2.ops import signs

AUTHOR = "Test Editor"
V1 = "/api/v1"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_a_distance_is_cut_down_to_the_half_kilometre_and_an_arrow_is_read_from_the_junction():
    # 7.3 km on the map is 7.665 walked: 7.5 on the sign. From 10 km, the whole kilometre.
    assert [signs.shown_km(m) for m in (7_300, 9_600, 13_180, None)] == [7.5, 10.0, 14.0, None]
    # Under half a kilometre: to the hundred metres, and never nothing.
    assert [signs.shown_km(m) for m in (400, 200, 150, 40, 0)] == [0.4, 0.2, 0.1, 0.1, 0.1] and signs.km_text(0.2) == "0,2"
    # Written as the sign manual has it: one decimal with a comma under 10 km, none from 10.
    assert signs.shown_km(7_900, 1.0) == 7.5 and signs.km_text(7.5) == "7,5" and signs.km_text(14.0) == "14" and signs.km_text(None) == ""
    # A post north of the junction: someone at the junction looks north at it. East is to the right.
    here, north = (0.0, 0.0), (0.0, 4.0)
    assert [signs.arrow(here, north, way) for way in (90, 270, 0, 20, 340, 160, 200)] == [
        "høyre", "venstre", "rett fram", "rett fram", "rett fram", "høyre", "venstre"]
    # The same sign from a post on the other side of the path points the other way.
    assert signs.arrow(here, (0.0, -4.0), 90) == "venstre"
    assert signs.bearing((0, 0), (1, 1)) == pytest.approx(45) and signs.bearing((0, 0), (-1, 0)) == pytest.approx(270)
    # A post is assumed to stand in the widest opening between the ways out: here to the south-west.
    x, y = signs.stands((100.0, 100.0), [0.0, 90.0])
    assert x < 100 and y < 100 and ((x - 100) ** 2 + (y - 100) ** 2) ** 0.5 == pytest.approx(signs.POST_OUT_M)
    assert signs.stands((0.0, 0.0), [0.0]) == pytest.approx((signs.POST_OUT_M, 0.0), abs=1e-6)


def test_two_routes_that_reach_a_place_by_different_ways_have_a_sign_each():
    def reached(route, way, metres, via=None):
        return {"node": "n", "edge": "e", "to_type": "place", "to_id": "hut", "name": "Nørdstedalseter", "route": route,
                "way": frozenset(way), "metres": metres, "via": via}

    # bre8 goes over the glacier and bre9 round it: they set out along one stretch, and are two signs.
    over, round_it = reached("bre8", "e a b", 22_227, {"kind": "glacier", "after_m": 7_574, "from": "x"}), reached("bre9", "e c d", 24_272)
    apart = signs._shared({("hut", "bre8"): over, ("hut", "bre9"): round_it})
    assert [(s["routes"], s["apart"], s["metres"], bool(s["via"])) for s in apart] == [
        (["bre8"], "bre8", 22_227, True), (["bre9"], "bre9", 24_272, False)]
    # A third route that goes the way bre9 goes shares its sign; a sign alone along its stretch needs no telling apart.
    with_it = signs._shared({1: over, 2: round_it, 3: reached("bre10", "e c d", 24_272)})
    assert [(s["routes"], s["apart"]) for s in with_it] == [(["bre8"], "bre8"), (["bre9", "bre10"], "bre9")]
    assert [(s["routes"], s["apart"]) for s in signs._shared({1: round_it, 2: reached("bre10", "e c d", 24_272)})] == [
        (["bre9", "bre10"], "")]
    # Two ways that come to the same kilometres are one sign after all (bre21 and bre62 from Arentzbu to Hyrnavollen).
    same = signs._shared({1: reached("bre21", "e a", 13_979), 2: reached("bre62", "e b", 14_150)})
    assert [(s["routes"], s["apart"]) for s in same] == [(["bre21", "bre62"], "")]
    # The two are found by keys of their own.
    assert signs.sign_key("n", "e", "place", "hut") == "n|e|place:hut" and signs.sign_key("n", "e", "place", "hut", "bre9") == "n|e|place:hut|bre9"
    assert signs._key_parts("n|e|place:hut|bre9") == ("n", "e", "place", "hut", "bre9") and signs._key_parts("n|e|place:hut")[4] == ""
    with pytest.raises(NotFound):
        signs._key_parts("n|e|place:hut|bre9|more")


def test_a_post_near_a_hut_points_on_to_where_the_huts_signs_point():
    def sign(route, to, name, metres, way, **more):
        return {"node": "n", "edge": "e", "to_type": "place", "to_id": to, "name": name, "route": route,
                "way": frozenset(way.split()), "metres": metres, "via": None, **more}

    # Viva: Sprongdalshytta is 2.7 km off. From there signs point to Arentzbu, Sota sæter, Slæom, and back to Viva.
    at_viva = {("sprong", "bre15"): sign("bre15", "sprong", "Sprongdalshytta", 2_667, "v1 v2")}
    at_the_hut = {("viva", "bre15"): sign("bre15", "viva", "Viva", 2_667, "v1 v2"),
                  ("arentz", "bre18"): sign("bre18", "arentz", "Arentzbu", 15_531, "a1 a2"),
                  ("sota", "bre14"): sign("bre14", "sota", "Sota sæter", 18_252, "s1", via={"kind": "glacier", "after_m": 9_000, "from": "x"}),
                  ("slaeom", "bre61"): sign("bre61", "slaeom", "Slæom", 21_836, "l1"),
                  ("end", "bre99"): {**sign("bre99", "end", None, 500, "u1"), "to_type": "node"}}
    on = signs._onward(at_viva, "viva", [at_viva, at_the_hut], {"sprong": [1], "viva": [0]})
    # On to Arentzbu and Sota sæter: the same way as to the hut, the whole distance, by both routes. Not back to
    # Viva, not to Slæom (26 km on the sign), and not to an end of a route that is no place.
    assert {k[0]: (v["name"], v["metres"], v["edge"], v["route"], v["then"], v["onward"]) for k, v in on.items()} == {
        "arentz": ("Arentzbu", 18_198, "e", "bre15", ["bre18"], "Sprongdalshytta"),
        "sota": ("Sota sæter", 20_919, "e", "bre15", ["bre14"], "Sprongdalshytta")}
    assert on[("sota", "onward")]["via"] == {"kind": "glacier", "after_m": 11_667, "from": "x"}        # the ice is that much further off
    assert [(g["routes"], g["text"] if "text" in g else g["name"]) for g in signs._shared({**at_viva, **on})] == [
        (["bre15"], "Sprongdalshytta"), (["bre15", "bre18"], "Arentzbu"), (["bre15", "bre14"], "Sota sæter")]
    # A hut 6 km off is the end of the day: nothing further. And nothing to where the post points already.
    far = {("sprong", "bre15"): sign("bre15", "sprong", "Sprongdalshytta", 6_000, "v1 v2")}
    assert signs._onward(far, "viva", [far, at_the_hut], {"sprong": [1]}) == {}
    already = {**at_viva, ("arentz", "bre1"): sign("bre1", "arentz", "Arentzbu", 30_000, "z1")}
    assert set(signs._onward(already, "viva", [already, at_the_hut], {"sprong": [1]})) == {("sota", "onward")}
    # The edge of a glacier is no place to walk on from, and nowhere to point on to.
    by_the_ice = {("edge", "bre8"): sign("bre8", "edge", "Fortundalsbreen brekant nord", 1_000, "g1", kind="glacier_edge")}
    assert signs._onward(by_the_ice, None, [by_the_ice, at_the_hut], {"edge": [1]}) == {}
    to_the_ice = {**at_the_hut, ("arentz", "bre18"): sign("bre18", "arentz", "Fortundalsbreen brekant sør", 15_531, "a1 a2", kind="glacier_edge")}
    assert "arentz" not in {k[0] for k in signs._onward(at_viva, "viva", [at_viva, to_the_ice], {"sprong": [1]})}
    # Nor back along the way one came: a sign at the hut that leads over the stretch one has just walked.
    back = {**at_the_hut, ("arentz", "bre18"): sign("bre18", "arentz", "Arentzbu", 15_531, "v2 a1")}
    assert "arentz" not in {k[0] for k in signs._onward(at_viva, "viva", [at_viva, back], {"sprong": [1]})}


def test_only_a_post_at_an_end_point_points_on_past_a_hut():
    named = {"viva": "Viva", "fork": None, "tee": None, "hut": "Hytta", "passed": "Setra", "ice": "Piggebrean brekant nord"}

    kinds = {n: {"kind": "glacier_edge" if n == "ice" else "hut" if n == "hut" else "other" if named[n] else None}
             for n in named}

    class Net:
        node = property(lambda self: kinds)

        def place(self, n):
            return named[n]

        def is_end(self, n):
            return n in ("viva", "tee", "hut", "ice")

    # A place where a route ends, and a hut: end points. A junction is none, though a route ends there; nor a
    # place routes pass; nor the edge of a glacier.
    assert [signs._end_point(Net(), [n]) for n in ("viva", "hut", "fork", "tee", "passed", "ice")] == [
        True, True, False, False, False, False]


def test_a_post_carries_the_name_of_the_place_where_a_person_says_so(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    name = routes.detail(conn, route["id"])["ends"][0]["place"]

    def here():
        found = signs.listing(conn, "bre")
        return found, next(s for s in found["sites"] if s["name"] == name)

    found, site = here()
    post = site["posts"][0]
    assert post["name_sign"] is None and found["totals"]["name_signs"] == 0 and found["totals"]["onward"] == 0
    signs.set_post(conn, post["key"], AUTHOR, name_sign=True)
    found, site = here()
    top = site["posts"][0]["name_sign"]
    assert top == {"key": f"{post['key']}|name", "text": name, "to_order": False, "ordered": None}
    assert found["totals"]["name_signs"] == 1
    assert site["posts"][0]["assumed"] is True                 # saying it has a name sign does not place the post
    # It is ordered like the signs of the post, when marked: a row with the name, and no arrow and no distance.
    signs.mark(conn, [top["key"]], AUTHOR)
    assert here()[1]["posts"][0]["name_sign"]["to_order"] is True and here()[0]["totals"]["to_order"] == 1
    rows = signs.order_rows(conn, "bre")
    row = next(r for r in rows if r["key"] == top["key"])
    assert (row["text"], row["km"], row["direction"], row["colour"], row["routes"]) == (name, None, None, "trehvit", [])
    assert [r["key"] for r in signs.order_rows(conn, "bre", [top["key"]])] == [top["key"]]
    done = signs.take_out_order(conn, "bre", AUTHOR)
    from openpyxl import load_workbook

    line = next(r for r in load_workbook(BytesIO(signs.as_workbook(signs.order(conn, done["id"])["rows"], "Skilt"))).active.iter_rows(
        min_row=2, values_only=True) if r[1] == name)
    assert (line[0], line[2], line[3], line[5]) == (1, None, None, "trehvit")
    # Said to say something else, it says that, and says it has changed since it was ordered. Then taken off.
    signs.set_post(conn, post["key"], AUTHOR, name_text="Hytta ved vannet")
    top = here()[1]["posts"][0]["name_sign"]
    assert top["text"] == "Hytta ved vannet" and top["ordered"] == {**top["ordered"], "order": done["id"], "changed": True}
    assert top["to_order"] is False                           # ordered: no longer waiting
    signs.set_post(conn, post["key"], AUTHOR, name_text="")
    assert here()[1]["posts"][0]["name_sign"]["text"] == name
    signs.set_post(conn, post["key"], AUTHOR, name_sign=False)
    assert here()[1]["posts"][0]["name_sign"] is None and top["key"] not in [r["key"] for r in signs.order_rows(conn, "bre")]


def test_a_destination_the_routes_do_not_give_is_added_to_a_post_by_hand(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    names = [e["place"] for e in routes.detail(conn, route["id"])["ends"]]
    site = next(s for s in signs.listing(conn, "bre")["sites"] if s["name"] == names[0])
    post = site["posts"][0]
    onward = next(s for s in post["signs"] if s["to"]["name"] == names[1])
    # A spot on the way, a third of the route's longest stretch along: no place of ours, so the sign is told its name.
    lon, lat = conn.execute("""
        SELECT ST_X(m.p), ST_Y(m.p) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.33), 4326) AS p) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route["id"],)).fetchone()
    asked = signs.add_destination(conn, AUTHOR, post["key"], lon=lon, lat=lat)
    assert asked["needs"] == "name" and conn.execute("SELECT count(*) FROM ops.sign WHERE added").fetchone() == (0,)
    key = signs.add_destination(conn, AUTHOR, post["key"], lon=lon, lat=lat, name="Åsete")["key"]
    assert signs.NO_STRETCH in key and "|point:" in key

    def at_the_post():
        found = next(s for s in signs.listing(conn, "bre")["sites"] if s["key"] == site["key"])
        return found, {g["key"]: g for p in found["posts"] for g in p["signs"]}

    found, hanging = at_the_post()
    added = hanging[key]
    # It points the same way as the sign to the hut beyond, is nearer, and says how it was made and by which route.
    assert (added["text"], added["added"], added["routes"], added["unnamed"], added["by_hand"]) == ("Åsete", True, ["bre18"], False, [])
    assert 0 < added["metres"] < onward["metres"] and added["km"] == signs.shown_km(added["metres"])
    assert (added["direction"], added["bearing"]) == (onward["direction"], onward["bearing"]) and added["colour"] == "trehvit"
    assert len(found["posts"]) == len(site["posts"])        # a spot to point to is no post, and no sign site
    assert [s["key"] for s in signs.listing(conn, "bre")["sites"] if s["free"]] == []
    # A person may say another arrow or text of it like of any sign, but not leave it without words.
    signs.set_sign(conn, key, AUTHOR, text="Åsete seter", direction="rett fram")
    assert (at_the_post()[1][key]["text"], at_the_post()[1][key]["direction"]) == ("Åsete seter", "rett fram")
    with pytest.raises(Invalid, match="says what the spot is called"):
        signs.set_sign(conn, key, AUTHOR, text="")
    # It is ordered with the others, and taken out and put back like them.
    signs.mark(conn, [key], AUTHOR)
    assert "Åsete seter" in [r["text"] for r in signs.order_rows(conn, "bre")]
    signs.set_sign(conn, key, AUTHOR, taken_out=True)
    assert at_the_post()[1][key]["taken_out"] is True and "Åsete seter" not in [r["text"] for r in signs.order_rows(conn, "bre")]

    # A place on the way that is no hut gets no sign by the rule, and is added the same way: once.
    ends = {e["id"] for e in route["ends"]}
    middle = places.at_node(conn, next(n["id"] for n in route["nodes"] if n["id"] not in ends), AUTHOR, "Midtveis")
    assert "Midtveis" not in [g["text"] for g in at_the_post()[1].values()]
    by_place = signs.add_destination(conn, AUTHOR, post["key"], place=str(middle))["key"]
    assert (at_the_post()[1][by_place]["text"], at_the_post()[1][by_place]["added"]) == ("Midtveis", True)
    with pytest.raises(Conflict, match="added to the post already"):
        signs.add_destination(conn, AUTHOR, post["key"], place=str(middle))
    signs.set_sign(conn, by_place, AUTHOR, taken_out=True)
    assert signs.add_destination(conn, AUTHOR, post["key"], place=str(middle)) == {"key": by_place}      # put back
    assert at_the_post()[1][by_place]["taken_out"] is False

    # A place of ours: not the one the post stands at, and not one a sign of the post points to already.
    here, there = (conn.execute("SELECT id::text FROM core.place WHERE name = %s AND status = 'confirmed'", (n,)).fetchone()[0]
                   for n in names)
    with pytest.raises(Invalid, match="the place the post stands at"):
        signs.add_destination(conn, AUTHOR, post["key"], place=here)
    with pytest.raises(Conflict, match=f"a sign to {names[1]} hangs on that post already, by bre18"):
        signs.add_destination(conn, AUTHOR, post["key"], place=there)
    with pytest.raises(Invalid, match="a place of yours or to a spot on a route"):
        signs.add_destination(conn, AUTHOR, post["key"], lon=lon + 1.0, lat=lat, name="Langt borte")
    with pytest.raises(NotFound):
        signs.set_sign(conn, signs.sign_key(post["node"], signs.NO_STRETCH, "place", there), AUTHOR, text="x")


def test_operations_is_a_role_of_its_own_and_the_route_responsible_has_it_too():
    from stiflyt2 import access

    assert access.ROLES == ("reader", "operations", "editor")
    assert [(access.Person("A", role).edits, access.Person("A", role).operates) for role in access.ROLES] == [
        (False, False), (False, True), (True, True)]
    assert not access.Person(None, "operations").operates           # nobody in particular decides nothing


def test_signs_are_worked_out_from_the_routes_and_what_a_person_decides_is_kept(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    route = routes.detail(conn, route["id"])
    names = [e["place"] for e in route["ends"]]
    assert all(names) and "bre" in [a["area"] for a in signs.areas(conn)]

    found = signs.listing(conn, "bre")
    by_name = {s["name"]: s for s in found["sites"] if s["name"]}
    assert set(names) <= set(by_name) and found["totals"]["sites"] == len(found["sites"]) and found["factor"] == 1.05
    first = by_name[names[0]]
    # A site with a name of its own does not say what it lies by; one with none says the nearest name, if any is near.
    assert all(s["near"] is None for s in found["sites"] if s["name"])
    assert all(s["near"] is None or (s["near"]["name"] and 0 <= s["near"]["metres"] <= signs.NEARBY_M)
               for s in found["sites"] if not s["name"])
    x, y = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM source_index.place WHERE name = 'Arentzbu' LIMIT 1").fetchone()
    assert signs._nearby(conn, [(x + 30, y), (x + 5000, y + 90000)]) == [
        {"name": "Arentzbu", "type": "turisthytte", "metres": 30}, None] and signs._nearby(conn, []) == []
    assert first["no_signs"] is False and first["code"] is None and first["key"].startswith("place:")
    assert "status" not in first and found["totals"]["no_signs"] == 0          # a site has no state of its own
    assert first["back_text"].startswith(f"{names[0]}\nUTM 32V ") and len(first["posts"]) >= 1
    # At one end of bre18 a sign points to the hut at its other end, the distance cut down as a sign has it.
    post = first["posts"][0]
    sign = next(s for s in post["signs"] if s["to"]["name"] == names[1])
    assert sign["text"] == names[1] and "bre18" in sign["routes"] and sign["colour"] == "trehvit" and sign["by_hand"] == []
    assert sign["km"] == signs.shown_km(sign["metres"]) and sign["metres"] == pytest.approx(route["length"]["on_foot_m"], abs=250)
    assert sign["direction"] in signs.DIRECTIONS and sign["direction"] == sign["generated"]["direction"]
    assert post["assumed"] and not post["taken_out"] and sign["ordered"] is None
    # Never a sign to where one stands, and none past a hut.
    assert all(s["to"]["name"] != names[0] for p in first["posts"] for s in p["signs"])

    # The post is moved to the other side of the junction: the arrows turn, and it is no longer assumed.
    junction = conn.execute("SELECT ST_X(g), ST_Y(g) FROM (SELECT ST_Transform(geom, 4326) AS g FROM core.node WHERE id = %s) x",
                            (post["node"],)).fetchone()
    mirrored = (2 * junction[0] - post["lon"], 2 * junction[1] - post["lat"])
    signs.set_post(conn, post["key"], AUTHOR, lon=mirrored[0], lat=mirrored[1])
    moved = next(s for s in signs.listing(conn, "bre")["sites"] if s["key"] == first["key"])["posts"][0]
    turned = next(s for s in moved["signs"] if s["key"] == sign["key"])
    assert not moved["assumed"] and (moved["lon"], moved["lat"]) == pytest.approx(mirrored, abs=1e-5)
    if "rett fram" not in (sign["direction"], turned["direction"]):
        assert {turned["direction"], sign["direction"]} == {"venstre", "høyre"}
    with pytest.raises(Invalid, match="stands by its junction"):
        signs.set_post(conn, post["key"], AUTHOR, lon=post["lon"] + 0.1, lat=post["lat"])
    signs.set_post(conn, post["key"], AUTHOR, assumed=True)

    # Said by hand: arrow, text, distance, colour. Each stands until taken away again.
    signs.set_sign(conn, sign["key"], AUTHOR, direction="rett fram", text="Til hytta", distance_m=7_900, colour="grønn")
    said = next(s for p in next(x for x in signs.listing(conn, "bre")["sites"] if x["key"] == first["key"])["posts"]
                for s in p["signs"] if s["key"] == sign["key"])
    assert (said["direction"], said["text"], said["km"], said["colour"]) == ("rett fram", "Til hytta", 7.5, "grønn")
    assert said["by_hand"] == ["colour", "direction", "distance_m", "text"] and said["generated"]["text"] == names[1]
    signs.set_sign(conn, sign["key"], AUTHOR, direction="", text="", distance_m=0, colour="")
    with pytest.raises(Invalid, match="an arrow points"):
        signs.set_sign(conn, sign["key"], AUTHOR, direction="opp")
    with pytest.raises(NotFound):
        signs.set_sign(conn, "not-a-sign", AUTHOR, text="x")

    # A further post at the same junction, and a sign moved onto it; a sign taken out.
    second = signs.add_post(conn, post["node"], AUTHOR, post["lon"] + 0.0001, post["lat"])
    assert second == f"{post['node']}:2"
    signs.set_sign(conn, sign["key"], AUTHOR, post_no=2)
    other = next(s for s in post["signs"] if s["key"] != sign["key"]) if len(post["signs"]) > 1 else None
    if other:
        signs.set_sign(conn, other["key"], AUTHOR, taken_out=True)
    now = next(s for s in signs.listing(conn, "bre")["sites"] if s["key"] == first["key"])
    two = [p for p in now["posts"] if p["node"] == post["node"]]
    assert [p["no"] for p in two] == [1, 2] and [s["key"] for s in two[1]["signs"]] == [sign["key"]]
    assert sign["key"] not in [s["key"] for s in two[0]["signs"]]
    if other:
        assert next(s for s in two[0]["signs"] if s["key"] == other["key"])["taken_out"] is True
    with pytest.raises(NotFound):
        signs.set_post(conn, f"{post['node']}:7", AUTHOR, taken_out=True)

    # Nothing is ordered until a person marks a sign: the signs hang there already.
    with pytest.raises(Invalid, match="nothing to order"):
        signs.take_out_order(conn, "bre", AUTHOR)
    signs.set_site(conn, first["key"], AUTHOR, area="bre", send_to_name="Hyttevakten", send_to_address="Fjellveien 1")
    said_of = next(s for s in signs.listing(conn, "bre")["sites"] if s["key"] == first["key"])
    assert said_of["send_to_name"] == "Hyttevakten" and said_of["code"] is None and signs.order_rows(conn, "bre") == []
    # An order is the signs a person has marked to be made: this one is worn. A sign taken out is not made.
    assert signs.mark(conn, [sign["key"], *([other["key"]] if other else [])], AUTHOR, why="worn") == (2 if other else 1)
    marked = signs.listing(conn, "bre")
    assert marked["totals"]["to_order"] == 1 and next(
        (g["to_order"], g["why"]) for s in marked["sites"] for p in s["posts"] for g in p["signs"] if g["key"] == sign["key"]) == (True, "worn")
    rows = signs.order_rows(conn, "bre")
    assert [r["key"] for r in rows] == [sign["key"]] and rows[0]["why"] == "worn"
    assert all(r["send_to"] == "Hyttevakten\nFjellveien 1" and r["back_text"] == first["back_text"] for r in rows)
    signs.mark(conn, [sign["key"]], AUTHOR, to_order=False)
    assert signs.order_rows(conn, "bre") == []
    signs.mark(conn, [sign["key"]], AUTHOR)
    # A site that is said to have no signs has none ordered, marked or not.
    signs.set_site(conn, first["key"], AUTHOR, no_signs=True)
    nothing = signs.listing(conn, "bre")
    assert nothing["totals"]["no_signs"] == 1 and next(s["no_signs"] for s in nothing["sites"] if s["key"] == first["key"])
    assert signs.order_rows(conn, "bre") == []
    signs.set_site(conn, first["key"], AUTHOR, no_signs=False)
    done = signs.take_out_order(conn, "bre", AUTHOR, note="First batch")
    assert done["signs"] == len(rows) and signs.orders(conn, "bre")[0]["note"] == "First batch"
    # The site gets its code with the first order that has a sign of it, and the order has it.
    assert next(s["code"] for s in signs.listing(conn, "bre")["sites"] if s["key"] == first["key"]) == "BRE-SS-0001"
    assert {r["site"] for r in signs.order(conn, done["id"])["rows"]} == {"BRE-SS-0001"}
    # Ordered, it is no longer waiting to be ordered. It is waiting to be put up, until a person says it is up.
    after = signs.listing(conn, "bre")
    assert signs.order_rows(conn, "bre") == [] and after["totals"]["to_order"] == 0 and after["totals"]["waiting"] == 1
    assert signs.mark_up(conn, [sign["key"], "never|ordered|place:x"], AUTHOR) == 1        # one that was never ordered hangs already
    up = signs.listing(conn, "bre")
    assert up["totals"]["waiting"] == 0 and next(
        g["ordered"]["up"] for s in up["sites"] for p in s["posts"] for g in p["signs"] if g["key"] == sign["key"]) is True
    assert signs.mark_up(conn, [sign["key"]], AUTHOR, up=False) == 1 and signs.listing(conn, "bre")["totals"]["waiting"] == 1
    kept = next(s for p in next(x for x in signs.listing(conn, "bre")["sites"] if x["key"] == first["key"])["posts"]
                for s in p["signs"] if s["key"] == sign["key"])
    assert kept["ordered"]["order"] == done["id"] and kept["ordered"]["changed"] is False
    # The sign is said to say something else: the order stays as it was, and the sign says it has changed since.
    signs.set_sign(conn, sign["key"], AUTHOR, text="Annet navn")
    later = next(s for p in next(x for x in signs.listing(conn, "bre")["sites"] if x["key"] == first["key"])["posts"]
                 for s in p["signs"] if s["key"] == sign["key"])
    assert later["ordered"]["changed"] is True and signs.listing(conn, "bre")["totals"]["changed_since_ordered"] == 1
    assert next(r for r in signs.order(conn, done["id"])["rows"] if r["key"] == sign["key"])["text"] == names[1]

    # The list for whoever makes the signs has v1's columns, one row per sign.
    from openpyxl import load_workbook

    sheet = load_workbook(BytesIO(signs.as_workbook(signs.order(conn, done["id"])["rows"], "Skilt bre"))).active
    table = list(sheet.iter_rows(values_only=True))
    assert table[0] == ("Antall", "Tekst på skiltet", "Km", "Pilretning", "Baksidetekst", "Skiltfarge", "Ruter",
                        "Sendes til (navn, adresse)")
    assert len(table) == 1 + len(rows)
    line = next(r for r in table[1:] if r[1] == names[1])
    assert line[0] == 1 and line[2] == signs.km_text(sign["km"]) and line[3] in signs.DIRECTIONS and line[5] == "trehvit"
    assert "bre18" in line[6] and line[7] == "Hyttevakten\nFjellveien 1"
    # What was decided is told: who, and what.
    told = [r[0] for r in conn.execute("SELECT type FROM ops.event ORDER BY seq").fetchall()]
    assert {"post.moved", "post.put_back", "sign.changed", "site.changed", "order.taken_out"} <= set(told)


def test_a_sign_site_is_put_at_a_spot_on_a_route_that_is_no_junction(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    names = {e["place"] for e in routes.detail(conn, route["id"])["ends"]}
    before = signs.listing(conn, "bre")["totals"]["sites"]
    # A spot in the middle of the route's longest stretch, a few metres off the line.
    lon, lat, end_lon, end_lat = conn.execute("""
        SELECT ST_X(m.p), ST_Y(m.p), ST_X(m.e), ST_Y(m.e) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 8, 0), 4326) AS p,
                             ST_Transform(ST_StartPoint(r.geom), 4326) AS e) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route["id"],)).fetchone()
    key = signs.add_site(conn, AUTHOR, "bre", lon, lat, "Ved brua")
    found = signs.listing(conn, "bre")
    site = next(s for s in found["sites"] if s["key"] == key)
    assert key.startswith("point:") and found["totals"]["sites"] == before + 1
    assert (site["free"], site["name"], site["no_signs"], site["code"], site["routes"]) == (True, "Ved brua", False, "BRE-SS-0001", ["bre18"])
    assert site["back_text"].startswith("Ved brua\nUTM 32V ") and len(site["posts"]) == 1
    post = site["posts"][0]
    # It stands on the line, and has a sign each way along the route, to the hut at either end.
    assert {s["text"] for s in post["signs"]} == names and len(post["signs"]) == 2
    assert sum(s["metres"] for s in post["signs"]) == pytest.approx(route["length"]["on_foot_m"], abs=250)
    assert {s["direction"] for s in post["signs"]} == {"venstre", "høyre"}        # the post stands beside the path
    assert abs(post["signs"][0]["bearing"] - post["signs"][1]["bearing"]) == pytest.approx(180, abs=60)
    # What a person decides about it is kept like for any other site.
    signs.set_sign(conn, post["signs"][0]["key"], AUTHOR, text="Hytta")
    signs.set_post(conn, post["key"], AUTHOR, lon=post["lon"], lat=post["lat"] + 0.00005)
    again = next(s for s in signs.listing(conn, "bre")["sites"] if s["key"] == key)["posts"][0]
    assert "Hytta" in [s["text"] for s in again["signs"]] and not again["assumed"]
    signs.mark(conn, [again["signs"][0]["key"]], AUTHOR)
    assert key.split(":")[1] in [r["key"].split("|")[0] for r in signs.order_rows(conn, "bre")]

    # Not at a junction, which has its own site; not off the routes; and only such a site is taken away.
    with pytest.raises(Invalid, match="at a junction"):
        signs.add_site(conn, AUTHOR, "bre", end_lon, end_lat)
    with pytest.raises(Invalid, match="stands on a route"):
        signs.add_site(conn, AUTHOR, "bre", lon + 1.0, lat)
    with pytest.raises(Invalid, match="no signs here"):
        signs.remove_site(conn, next(s["key"] for s in found["sites"] if not s["free"]), AUTHOR)
    signs.remove_site(conn, key, AUTHOR)
    assert key not in [s["key"] for s in signs.listing(conn, "bre")["sites"]]
    with pytest.raises(NotFound):
        signs.set_site(conn, key, AUTHOR, area="bre", name="Borte")


def test_an_assistant_has_the_signs_and_operations_is_enough_to_decide_about_them(loaded, monkeypatch):
    import json

    from stiflyt2 import access
    from stiflyt2.api import mcp
    from stiflyt2.config import settings

    # Who may: the sign tools want the operations role, the route tools the route responsible.
    server = mcp.Server(None, None)
    refused = server.call(access.Person("Per", "operations"), "describe_route", {"route": "bre18", "name": "x"})
    assert refused["isError"] and "editor role" in refused["content"][0]["text"]
    refused = server.call(access.Person("Kari", "reader"), "sign_site", {"area": "bre", "site": "node:x", "no_signs": True})
    assert refused["isError"] and "operations role" in refused["content"][0]["text"]
    assert all(mcp.TOOLS[t].operations for t in ("sign_site", "sign_post", "sign", "new_sign_site", "add_sign_destination",
                                                 "order_signs"))
    assert not mcp.TOOLS["signs"].changes and not mcp.TOOLS["describe_route"].operations

    monkeypatch.setattr(settings, "editor", AUTHOR)
    with TestClient(create_app()) as client:
        def tool(called, /, **arguments):
            got = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": called, "arguments": arguments}}).json()["result"]
            text = got["content"][0]["text"]
            return (text if got["isError"] else json.loads(text)), got["isError"]

        found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "bre19"}).json()
        try:
            short, refused = tool("signs", area="bre")
            assert not refused and short["totals"]["sites"] == len(short["sites"]) >= 1
            one = next(s for s in short["sites"] if "bre19" in s["routes"])
            assert set(one) >= {"key", "name", "code", "routes", "posts", "signs"} and "status" not in one
            full, _ = tool("signs", area="bre", site=one["key"])
            sign = full["sites"][0]["posts"][0]["signs"][0]
            # Dry is the default: it says what the site would be, and keeps nothing.
            after, refused = tool("sign_site", area="bre", site=one["key"], no_signs=True, name="Krysset")
            assert not refused and (after["no_signs"], after["name"]) == (True, "Krysset")
            after, refused = tool("sign", area="bre", sign=sign["key"], text="Til enden", direction="rett fram")
            said = next(g for p in after["posts"] for g in p["signs"] if g["key"] == sign["key"])
            assert not refused and (said["text"], said["direction"], said["by_hand"]) == ("Til enden", "rett fram", ["direction", "text"])
            post = full["sites"][0]["posts"][0]
            after, refused = tool("sign_post", area="bre", post=post["key"], lon=post["lon"] + 0.00003, lat=post["lat"])
            assert not refused and after["posts"][0]["assumed"] is False
            # A destination by hand: a spot that is no place needs its name; with one, the sign is there (dry).
            text, refused = tool("add_sign_destination", area="bre", post=post["key"], at=[post["lon"], post["lat"]])
            assert refused and "say what the sign is to say" in text
            after, refused = tool("add_sign_destination", area="bre", post=post["key"], at=[post["lon"], post["lat"]], name="Her")
            assert not refused and [g["text"] for p in after["posts"] for g in p["signs"] if g["added"]] == ["Her"]
            assert client.post(f"{V1}/signs/destinations", json={"post": post["key"], "lon": post["lon"], "lat": post["lat"]}
                               ).json()["needs"] == "name"
            now, _ = tool("signs", area="bre", site=one["key"])
            assert not [g for p in now["sites"][0]["posts"] for g in p["signs"] if g["added"]]
            assert now["sites"][0]["no_signs"] is False and now["sites"][0]["posts"][0]["assumed"] is True
            assert now["sites"][0]["posts"][0]["signs"][0]["by_hand"] == []
            text, refused = tool("signs", area="bre", site="Ingensteds")
            assert refused and "no sign site in bre is called" in text
            would, refused = tool("order_signs", area="bre")
            assert not refused and would == {"would_order": 0, "rows": []}
            text, refused = tool("order_signs", area="bre", dry_run=False)
            assert refused and "nothing to order" in text
        finally:
            assert client.delete(f"{V1}/owned/routes/{route['id']}").status_code == 204
        assert client.post(f"{V1}/changeset/close").status_code == 204


def test_signs_hang_as_the_sign_manual_has_them_and_a_site_has_a_sheet_to_print(conn):
    routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    routes.make(conn, candidate(conn, "bre17"), AUTHOR)
    routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    found = signs.listing(conn, "bre")
    # On every post: those that point right, then left, then straight on; the same way, the longest distance on top.
    for post in (p for s in found["sites"] for p in s["posts"]):
        hanging = [g for g in post["signs"] if not g["taken_out"]]
        ranks = [signs._HANGS.get(g["direction"], 3) for g in hanging]
        assert ranks == sorted(ranks)
        for a, b in itertools.pairwise(hanging):
            if (a["direction"], a["bearing"]) == (b["direction"], b["bearing"]) and None not in (a["km"], b["km"]):
                assert a["km"] >= b["km"]
    site = next(s for s in found["sites"] if s["name"] == "Arentzbu")
    signs.set_post(conn, site["posts"][0]["key"], AUTHOR, name_sign=True)
    signs.mark(conn, [site["posts"][0]["signs"][0]["key"]], AUTHOR)

    from stiflyt2.ops import sheet

    # The field sheet of the site: its name, its map (Kartverket's tiles, the post, the routes), and what hangs there.
    one = sheet.page(conn, "bre", site["key"])
    assert one.count('<section class="sheet">') == 1 and "<h1>Arentzbu</h1>" in one and "Feltark: Arentzbu" in one
    assert "cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/" in one and '<polyline points="' in one
    assert 'class="post"' in one and "Stolpe 1" in one and "stedsnavn, øverst" in one and "skal bestilles" in one
    assert all(g["text"] in one for g in site["posts"][0]["signs"] if g["text"]) and "UTM 32V" in one
    assert "<script" not in one                                 # nothing but the print button runs
    # Of every site, one page each; of an order, the sites it has signs at; and a site that is not there.
    every = sheet.page(conn, "bre")
    assert every.count('<section class="sheet">') == len(found["sites"]) and "Kryss bre" in every
    order = signs.take_out_order(conn, "bre", AUTHOR)
    of_order = sheet.page(conn, "bre", order=order["id"])
    assert of_order.count('<section class="sheet">') == 1 and f"bestilling {order['id']}, ikke oppe" in of_order
    with pytest.raises(NotFound):
        sheet.page(conn, "bre", "place:00000000-0000-0000-0000-000000000000")
    # What is written into a sheet is written as text.
    signs.set_site(conn, site["key"], AUTHOR, area="bre", name="<b>Arentzbu & co</b>")
    assert "&lt;b&gt;Arentzbu &amp; co&lt;/b&gt;" in sheet.page(conn, "bre", site["key"]) and "<b>Arentzbu" not in sheet.page(conn, "bre", site["key"])


def test_the_front_door_is_operations_and_the_routes_have_an_address_of_their_own(loaded, monkeypatch):
    from stiflyt2.config import settings

    with TestClient(create_app()) as client:
        front, editor = ("\n".join(scripts_of(client, page).values()) for page in ("/", "/ruter"))
        assert "Signs" in client.get("/").text and 'get("signs", { area' in front
        assert "Your route" in client.get("/ruter").text
        # What the two share: the base maps and their switch, and the order as a basket on the signs page.
        shared = client.get("/static/basemap.js")
        assert shared.status_code == 200 and "tile.openstreetmap.org" in shared.text and "topograatone" in shared.text
        assert all("/static/basemap.js" in client.get(page).text for page in ("/", "/ruter"))
        assert "new BaseControl()" in front and "new BaseControl()" in editor
        assert 'id="basket"' in client.get("/").text
        # Nobody is named as editor on this server: looking is allowed, deciding is not.
        assert client.get(f"{V1}/signs/areas").status_code == 200
        assert client.patch(f"{V1}/signs", json={"key": "a|b|place:c", "text": "x"}).status_code == 403
        monkeypatch.setattr(settings, "editor", AUTHOR)
        assert client.get(f"{V1}/auth/me").json()["operates"] is True
        found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "bre19"}).json()
        try:
            sites = client.get(f"{V1}/signs", params={"area": "bre"}).json()["sites"]
            site = next(s for s in sites if "bre19" in s["routes"])
            assert client.get(f"{V1}/signs", params={"area": "norge"}).status_code == 422
            assert client.patch(f"{V1}/signs/sites/{site['key']}", json={"area": "bre", "no_signs": True}).status_code == 204
            again = next(s for s in client.get(f"{V1}/signs", params={"area": "bre"}).json()["sites"] if s["key"] == site["key"])
            assert again["no_signs"] is True and again["code"] is None
            assert client.patch(f"{V1}/signs/sites/{site['key']}", json={"no_signs": False}).status_code == 204
            post = again["posts"][0]
            assert client.patch(f"{V1}/signs/posts/{post['key']}", json={"lon": post["lon"], "lat": post["lat"] + 0.00002}).status_code == 204
            assert client.patch(f"{V1}/signs/posts/{post['key']}", json={"assumed": True}).status_code == 204
            assert client.patch(f"{V1}/signs/sites/{site['key']}", json={"no_signs": "perhaps"}).status_code == 422
            assert client.patch(f"{V1}/signs/sites/place:00000000-0000-0000-0000-000000000000", json={"no_signs": True}).status_code == 404
            assert client.post(f"{V1}/signs/orders", json={"area": "bre"}).status_code == 422       # nothing is marked
            named = [s["key"] for s in post["signs"] if not s["unnamed"]]
            if named:
                assert client.post(f"{V1}/signs/to-order", json={"keys": named, "why": "worn"}).json() == {"signs": len(named)}
                order = client.post(f"{V1}/signs/orders", json={"area": "bre", "note": "From the test"})
                assert order.status_code == 201 and order.json()["signs"] >= 1
                book = client.get(f"{V1}/signs/orders/{order.json()['id']}.xlsx")
                assert book.status_code == 200 and book.content[:2] == b"PK" and "skilt-bre-" in book.headers["content-disposition"]
                assert client.get(f"{V1}/signs/orders", params={"area": "bre"}).json()[0]["id"] == order.json()["id"]
            assert client.get(f"{V1}/signs/orders/999999.xlsx").status_code == 404
            printed = client.get(f"{V1}/signs/sheet", params={"area": "bre", "site": site["key"]})
            assert printed.status_code == 200 and printed.headers["content-type"].startswith("text/html") and "Stolpe 1" in printed.text
            assert client.get(f"{V1}/signs/sheet", params={"area": "bre", "site": "node:nowhere"}).status_code == 404
            assert client.post(f"{V1}/signs/up", json={"keys": [s["key"] for s in post["signs"]]}).status_code == 200
        finally:
            assert client.delete(f"{V1}/owned/routes/{route['id']}").status_code == 204
        assert client.post(f"{V1}/changeset/close").status_code == 204
