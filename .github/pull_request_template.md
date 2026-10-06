## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
      (`feat:`, `fix:`, `portal:`, `docs:`, `chore:`) so the release and the
      changelog are generated correctly
- [ ] `uv lock --check` passes (run `uv lock` if you changed dependencies)
- [ ] `uv run ruff check .` and `uv run ruff format .` pass
- [ ] `uv run mypy` and `uv run pyright` pass
- [ ] `uv run pytest -q` passes, and new behaviour has a test
- [ ] No real taxpayer data in code, tests or fixtures

<!--
Do not edit CHANGELOG.md or the version in pyproject.toml: release-please
writes both from the commit messages when the release pull request is merged.
-->
