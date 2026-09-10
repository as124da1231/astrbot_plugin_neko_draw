"""Render the dependency-free AstrBot plugin page from Jinja2 templates.

Run from the plugin root with::

    python dashboard/render.py

The generated file is committed under pages/ because AstrBot plugin pages are
static iframe documents at runtime. Jinja2 is therefore a build dependency,
not a server/runtime dependency of the plugin.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PLUGIN_ROOT / "dashboard" / "templates"
OUTPUT = PLUGIN_ROOT / "pages" / "neko-draw" / "index.html"


def main() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("index.html.j2").render(
        app_name="Neko Draw",
        plugin_name="猫娘画图",
        version="2.2.4",
        themes=(
            {"id": "forest", "name": "森林实验室", "swatch": "#63b995"},
            {"id": "atelier", "name": "奶油画室", "swatch": "#f4b83f"},
            {"id": "midnight", "name": "午夜霓虹", "swatch": "#8b7cff"},
        ),
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Rendered {OUTPUT}")


if __name__ == "__main__":
    main()
