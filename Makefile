# Site workflow. The Pages deploy runs the same strict build as `site-build`.

.PHONY: site-serve site-build site-preview

site-serve:  ## dev server with live reload at http://127.0.0.1:8000/
	uv run properdocs serve -f mkdocs.yml

site-build:  ## strict build into site/ (what CI runs; fails on broken refs)
	uv run properdocs build --strict -f mkdocs.yml

site-preview: site-build  ## serve the built artifact at http://127.0.0.1:8001/
	uv run python -m http.server -d site 8001
