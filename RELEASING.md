# Releasing aioebus to PyPI

Publishing is automated: creating a **GitHub release** runs
`.github/workflows/publish.yml`, which builds the sdist + wheel and uploads them
to PyPI via **Trusted Publishing** (OIDC — no API token is stored anywhere).

> Publishing makes the source public on PyPI. Decide that deliberately; this repo
> is otherwise private.

## One-time setup

1. **PyPI trusted publisher** (do this before the first release, as a *pending*
   publisher since the project does not exist yet):
   PyPI → account → *Publishing* → *Add a pending publisher*:
   - PyPI Project Name: `aioebus`
   - Owner: `dneprojects`
   - Repository: `aioebus`
   - Workflow name: `publish.yml`
   - Environment: `pypi`
2. **GitHub environment**: repo → Settings → Environments → *New environment*
   named `pypi`.

## Each release

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Commit, then create a GitHub release whose tag is `vX.Y.Z` (e.g. `v0.1.0`).
3. The workflow builds and publishes; check the Actions run and the PyPI page.

## Optional: dry run on TestPyPI

Add a TestPyPI pending publisher (same fields) and run the workflow manually with
the publish step pointed at `https://test.pypi.org/legacy/` to rehearse without
touching the real index.

## Build locally

```bash
python -m build          # -> dist/*.whl, dist/*.tar.gz
python -m twine check dist/*
```
