# Routing Policy

The parent (Codex) makes routing decisions semantically; this document is
the decision contract. There is no keyword-scoring router.

## Model routing

Routing is Flash-first. The parent starts with `deepseek_flash` for bounded
text-only investigation whenever a read-only result can satisfy the task.
Large context, many files, long logs, initial root-cause work, and
pre-implementation proposals are not Pro triggers by themselves. Pro is the
default destination only for workspace edits, explicit Flash escalation or
failure, high-risk reasoning, and system-wide decisions that cannot be bounded
with Flash evidence. Flash proposals can be landed by the parent directly;
they do not require a redundant Pro pass.

Once a task is escalated for implementation or high-risk work, Pro is the
technical owner for that scope. The Pro assignment carries acceptance criteria,
a concrete verification target, and child or shared verification ownership.
The Flash parent checks directly observable facts such as changed files, test
exit status, and requested coverage. It sends unresolved technical uncertainty
back to Pro instead of overriding Pro with a weaker independent analysis.

### deepseek_flash (deepseek-v4-flash)

Fast bounded read-only worker: repository search, enumeration, logs,
test-output analysis, extraction, code mapping, high-volume reading, and
multi-file investigation. Flash also handles bounded SPEC-Lite root-cause
work. Flash never edits files — it returns findings, analysis or a proposed
change as text, and the parent (or Pro when escalation is genuinely needed)
lands the edit.

Examples: "Find all callers of create_job", "Find the earliest anomaly in
this 5000-line test log", "Propose the exact diff for renaming tmp to
buffer in this function".

### deepseek_pro (deepseek-v4-pro)

Deep solver/reviewer: difficult root cause, architecture, concurrency and
races, distributed consistency, state machines, security analysis,
cross-module refactors, ambiguous behavior, hard implementation.

Example: "Why do lease renew and settlement occasionally race?"

### NO_DELEGATION

Trivial work the parent can finish faster than the handoff cost; strong
visual tasks (`VISION_CRITICAL`); highly sensitive data; anything where
delegation overhead exceeds the task. Delegation needs to materially improve
specialization, context isolation, cost, or quality.

## Reasoning policies

The policy name is not the prompt. `runtime/reasoning.py` composes one of four
Execution Contracts with a policy-specific Stop Condition and, for Flash only,
short model tuning on the child's first request:

| Agent | FAST | REACT | SPEC | DEEP |
|---|---:|---:|---:|---:|
| `deepseek_flash` | yes | read-only proposal | SPEC-Lite | **invalid** |
| `deepseek_pro` | yes | implement/test | root cause | yes |

The same matrix is checked before a pending file is created and whenever an
envelope is read. Invalid combinations are never silently upgraded,
downgraded or rewritten.

- **FAST** — inspect the minimum needed → answer → stop. Search,
  extraction, simple investigation. Flash.
- **REACT** — understand the requested result → implement the smallest
  coherent solution that can satisfy the assignment → test → check explicit
  child-verifiable acceptance criteria → fix → converge. Clear requirements,
  clear path. Pro for implementation; Flash may prepare the change as a
  read-only proposal (diff or plan) for the parent to land. A runnable partial
  implementation is not completion when material criteria remain unresolved.
  Flash's read-only REACT variant instead stops after a proposal maps the
  criteria and separates child-verifiable from parent-owned checks.
- **SPEC** — inspect → trace → hypothesis → evidence → root cause →
  smallest fix → verify. Bugs, reviews, unexpected behavior. Pro (Flash may
  run a SPEC-Lite exploration).
- **DEEP** — model system → invariants → failure modes → alternatives →
  decision, with explicit decision closure. Architecture, distributed
  systems, complex concurrency, security, very hard root cause. Pro only.

Every policy converges: reason until there is enough evidence to act, then
commit. For REACT, the assignment must state `ACCEPTANCE CRITERIA`,
`VERIFICATION OWNER` (`CHILD`, `PARENT`, or `SHARED`), and a
`STOPPING CONDITION`. The child verifies only criteria within its capability
and surfaces parent-owned criteria for the parent. Unbounded reasoning,
repeated hypotheses and analysis-without-action are forbidden; an agent that
cannot continue returns `BLOCKED` (what is missing, why, minimal next step).

## Parent acceptance gate

The Codex parent checks actual artifacts, tests, runtime output, and available
visual evidence after every child return. Without a screenshot or render, a
visual criterion is `UNVERIFIED`, not passed. Only a material gap that directly
violates an explicit user requirement may trigger one bounded Pro + REACT
follow-up; after that review, remaining limitations are reported honestly.
This preserves information-driven convergence without adding a fifth policy,
acceptance-profile schema field, or runtime retry state.

Agent TOMLs contain only role and safety invariants. Dynamic investigation,
implementation and closure flows live in the Reasoning Adapter so a FAST
request does not inherit an unrelated exhaustive workflow.

Model tuning is intentionally asymmetric. Flash is reminded to use supplied
evidence directly, obey output and honesty constraints, and do extra discovery
only when a missing fact blocks the answer. Pro receives no generic tuning in
adapter version 6: the pinned DSH source reports that additional recall/converge and
few-shot anchors can reduce Pro performance, while this project already has an
explicit parent-selected policy contract. A/B/C ablation may add Pro tuning in
the future only if it earns its cost.

Standalone fallback is more conservative than Native delivery for Flash SPEC:
without a native tool environment it always returns a complete Evidence Packet
for Pro continuation. The adapter may normalize fields already returned by the
provider, but never fabricates edits, commands, tests, or observations.

These policies are project execution contracts; they are not DSH's
spec/react/transition/weak behavior bands. No weak, mixed, continuous mode, or
secondary Standard/Spec router exists here.

## Escalation

Flash returns `ESCALATE_TO_PRO` with an `EVIDENCE_PACKET`
(summary, relevant_files, observations, hypotheses, eliminated,
open_questions, recommended_next_step) when it hits multi-module evidence
conflicts, concurrency, complex state machines, unconfirmable root cause,
architecture tradeoffs, or change risk beyond the task boundary. The parent
passes that packet to Pro instead of making Pro rediscover the repository.

## Policy A/B evaluation

Policies are hypotheses, not dogma: Current vs. Contract-only vs.
Contract+Tuning runs compare correctness first, then tool calls, total tokens,
latency, unnecessary/environment reads, unbounded search, convergence,
root-cause accuracy and code correctness. Flash and Pro are scored separately;
a block whose improvement is below noise gets deleted. See `docs/eval.md`.

The no-Hook fallback accepts an optional `policy`. Without one it uses the
deterministic minimum defaults `flash → FAST` and `pro → REACT`; it does not
claim semantic-policy parity with the Codex parent. Prompt guidance is shared,
but fallback capability is not: it is an explicit text-only provider request
without native subagent tools.
