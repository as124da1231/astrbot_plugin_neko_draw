# Image Studio Web UI

The AstrBot plugin page is authored with Jinja2 and rendered to a static page,
because AstrBot discovers `pages/<page-name>/index.html` as a static iframe.
The generated page is committed so users do not need Jinja2 at plugin runtime.

## Rebuild

Install Jinja2 in the development environment, then run from the plugin root:

```bash
python dashboard/render.py
```

Source files:

- `dashboard/templates/index.html.j2` — application shell and page structure
- `dashboard/templates/macros/ui.html.j2` — reusable UI macros
- `pages/neko-draw/assets/style.css` — three visual themes and responsive layout
- `pages/neko-draw/assets/app.js` — AstrBot bridge, history and schema-driven configuration editor

Theme colors are CSS custom properties at the start of `style.css`. The built-in
themes are `atelier`, `midnight`, and `forest`.
