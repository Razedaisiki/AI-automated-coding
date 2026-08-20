# Supervisor Resources — Source Namespace

> **Source namespace** (see `docs/supervisor-protocol.md §0`): these files are
> part of the Supervisor package and are version-controlled. They are not
> runtime state and will never be confused with the target workspace's
> `.agent/` or `AGENTS.md`.

# Supervisor Resources

This directory contains **product resources** bundled with the Supervisor
package, not runtime state.

| File | Purpose |
|---|---|
| `parent-policy.md` | Parent Agent role, delegation, validation and completion policy. Injected into every activation prompt via `importlib.resources`. |
| `agent-state.schema.json` | Example `agent/state.json` schema consumed/validated by `supervisor/models.py`. |

None of these files should be confused with the target repository's
`AGENTS.md` or `.agent/state.json` — those belong to the target workspace
and are documented in `docs/supervisor-protocol.md §2`.
