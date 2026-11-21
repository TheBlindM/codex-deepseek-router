# DSH Alignment Design Decisions

## Source snapshot

The 2026-08-18 review uses `dsh-routing-suite@d924ed0`, whose gitlinks pin
`injector@f4ef59f` and `dsh-router-standard@eff787e`. Source behavior, not the
suite README's component summary, is authoritative. Exact mechanisms and file
mapping are recorded in `docs/upstream-reference-map.md`.

## Keep Codex as the only semantic router

Codex chooses delegation, modality, sensitivity, Flash/Pro, and
FAST/REACT/SPEC/DEEP. The Reasoning Adapter enforces an already selected pair;
it does not classify natural language, add weak/mixed modes, expose a
continuous reasoning level, or let a child reroute itself.

The semantic policy is Flash-first. Flash remains the default for bounded
read-only investigation even when the investigation is large or spans several
files. Pro requires a concrete hard trigger: workspace edits, Flash escalation
or demonstrated failure, high-risk reasoning, or a system-wide decision that
cannot be bounded with available evidence. Long context or speculative future
edits are not sufficient reasons to skip Flash.

The text-only fallback applies the same direction numerically: Pro requires a
normalized demand score of at least `0.80`, raised from the previous `0.55`.
This makes the fallback materially Flash-biased while preserving an escalation
boundary for genuinely saturated tasks. Explicit `flash` and `pro` modes remain
honored unchanged.

Flash-first does not make the Flash parent the technical authority for an
escalated Pro task. Once Pro owns workspace edits or high-risk reasoning, its
assignment includes the verification target and child/shared acceptance
ownership. The Flash parent performs mechanical checks over observable
artifacts and routes unresolved technical uncertainty back to Pro. This avoids
having a lower-cost parent silently override the stronger worker it invoked.

## Use additive first-turn contracts, not a DSH surface imitation

Pinned DSH Standard replaces system sections, clears contexts, limits the
first tool catalog, and promotes it after a durable tool call. Public Codex
`SubagentStart.additionalContext` is additive. A `PreToolUse` denial would
happen after the model had already seen and selected the tool, so it is not an
equivalent hidden-tool surface. The project therefore adds bounded Policy,
Policy Execution Contract, optional Model-specific Tuning, and Stop Condition
blocks without asking the child to ignore higher-priority instructions.

## Tune Flash and Pro asymmetrically

Flash receives a short reminder to use supplied evidence directly, obey
requested output and honesty constraints, and do extra discovery only when a
missing fact blocks the answer. Pro receives no generic model tuning in adapter version 7. Its behavior
comes from the parent-selected execution contract and stop condition. This
matches the pinned source's warning that extra recall/converge and few-shot
anchors can hurt Pro, while preserving this project's stronger semantic parent
router.

The text-only fallback always converts Flash SPEC into a structured Pro
handoff because it lacks native Codex tools. Missing wrapper fields may be
filled only from the provider's returned summary, findings, recommendations,
and evidence; the adapter never invents an execution claim.

## Keep transport unchanged

The one-pending-file-per-role, claim-first, TTL, quarantine, lock, rollback,
and at-most-once behavior remains intact. Same-role parallel Flash work needs
handoff-id-to-child correlation and is a separate transport V2 project, not a
prompt change.

## Make completion acceptance-driven

The 2026-08 acceptance review found that a fast REACT stop could equate
“implemented and runnable” with “accepted,” especially for visual/WebGL work.
Assignments now carry `ACCEPTANCE CRITERIA`, `VERIFICATION OWNER`, and a
`STOPPING CONDITION` as plain text. This avoids an envelope migration while
making the requested result explicit. The REACT contract requires the child to
verify child-owned criteria and surface parent-owned criteria; the parent
checks actual artifacts and may issue at most one bounded Pro + REACT
follow-up for an objective material gap. Missing visual evidence is reported as
`UNVERIFIED` rather than guessed. Native terminal events are not completion by
themselves: only a successful `completed` result enters the normal gate;
`BLOCKED`, `interrupted`, `cancelled`, and `failed` results cannot PASS.

Complex Pro + REACT assignments may add one assignment-driven `QUALITY CLOSURE`
after the first functional verification pass. That bounded pass checks relevant
integration points, edge/failure paths, regression risks, conventions/invariants,
and concrete workarounds or TODOs, then fixes only material issues and reruns
verification. Ordinary Pro assignments keep their fast functional closure.

This is the project's information-driven convergence extension, inspired by
the DSH emphasis on information completeness. It is not a DSH-native
capability, a fifth policy, an acceptance-profile schema, or a transport state
machine. Ordinary functional work remains short; quality-sensitive work keeps
the evidence needed for user acceptance.

## Evaluate causal pieces separately

The execution harness compares A Current, B Contract Only, and C Contract plus
Model Tuning across 16 fixed tasks. It records adapter version, correctness,
public usage, prompt size, and explicit fields for native tool behavior. Flash
and Pro are scored separately; guidance without stable benefit is removed.

## Gate P2 experiments

No release near-field or closure Hook is enabled. Near-field starts with a
development-only probe of child model, turn, parent exclusion, Pro exclusion,
resume, and parallel isolation. Only a reliable probe and positive C-versus-D
result can justify one Flash-only reminder after the first durable tool result.
Mechanical `SubagentStop` closure is independent, can continue at most once,
uses `stop_hook_active`, and may check only missing structural fields.
