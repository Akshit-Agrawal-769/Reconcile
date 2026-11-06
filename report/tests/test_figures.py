import re
import struct

from report import figures, relations


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def test_figures_build(tmp_path):
    paths = figures.build(tmp_path)
    assert set(paths) == {"er_chen", "schema", "decomposition", "state_machine"}
    for name, p in paths.items():
        w, h = png_size(p)
        assert w >= 1500, (name, w)


def test_er_has_every_entity_and_chen_shapes():
    dot = figures.er_chen_dot()
    for entity in figures.ER_ENTITIES:
        assert f'"{entity}"' in dot
    assert "shape=diamond" in dot and "shape=ellipse" in dot and "peripheries=2" in dot
    assert "<U>" in dot          # key attributes underlined


def test_schema_figure_has_every_table():
    dot = figures.schema_dot()
    for table in relations.FINAL:
        assert re.search(rf'\b{table}\b \[', dot), table


def test_schema_figure_underlines_the_primary_key():
    dot = figures.schema_dot()
    block = dot[dot.index("app_user [label"):].split("\n")[0]
    assert "<U>user_id</U>" in block and "<U>email</U>" not in block
