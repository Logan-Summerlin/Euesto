# Documentation index

Start with [`../AGENTS.md`](../AGENTS.md) for the durable invariants and [`../README.md`](../README.md) for the product overview. The references below are authoritative; when two documents disagree, fix the one that is wrong rather than adding a third.

## Reading order

| Read this | When you need |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, ownership and dependency rules, data flow, and the security boundary. |
| [TOOLS.md](TOOLS.md) | The thirteen public tools: arguments, modes, permissions, results, and error codes. |
| [LIMITS.md](LIMITS.md) | Effective defaults, hard ceilings, and how requested values are clamped. |
| [PUBLICATION.md](PUBLICATION.md) | Staging, manifests, approval, hash validation, conflicts, and recovery. |
| [EGRESS.md](EGRESS.md) | Network modes, the one-shot dependency setup container, and the allowlisted registry proxy. |
| [TESTING.md](TESTING.md) | Test tiers, the validator, the one authoritative list of required checks, and CI workflows. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and the rules for changing a public tool, schema, or limit. |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptoms and fixes for runtime, workspace, staging, and publication failures. |
| [../docker/README.container.md](../docker/README.container.md) | Operating the container runtime and verifying it. |

## Plans and roadmap

Each living plan and the roadmap live only in this directory, one authoritative copy each. A plan moves to [`../archived-doc/`](../archived-doc/README.md) once it is fully implemented or superseded.

| Document | Scope |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Completed, active, planned, deferred, and non-goal status, including open items carried from archived plans. |
| [AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md) | Phased plan to widen agent capability inside the sandbox. Phases 0–5 are implemented; Phases 6–7 (long-running projects, simple setup) are proposed. |

## Naming and ownership rules

- Files in `docs/` are `UPPER_SNAKE_CASE.md`. Files in `archived-doc/` are lowercase-hyphenated.
- No spaces or dates in document names; dates belong in `CHANGELOG.md` or commit history.
- The repository root holds only `README.md`, `AGENTS.md`, and `CHANGELOG.md` as documentation.
- Each fact has one home. Other documents link to it instead of restating it.

`tests/structural/test_documentation_layout.py` and `tests/structural/test_documentation_contract.py` enforce these rules, the index, relative links, and the tool and limit values.

## Local-only state

`.local-chat-checkpoints/` is a runtime artifact. It is git-ignored and must not be committed.
