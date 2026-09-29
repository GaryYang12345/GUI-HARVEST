# GUI-HARVEST method contract

GUI-HARVEST optimizes an executable GUI-agent harness while keeping the GUI
backbone, task definitions, evaluators, environments, and resource limits
frozen.

## Evidence visibility

| split | optimizer visibility | use |
|---|---|---|
| Search | scores, trajectories, screenshots, tool outputs | diagnosis, clustering, behavioral checks, utility |
| Validation | aggregate task scores through outer runner | utility gate only |
| sealed Test | no IDs or outcomes before stopping | final generalization |

Search, Validation, and Test are fixed before any rollout. The split is
stratified by domain: each domain receives Search and Validation quotas in
proportion to its size, Test takes the rest, and tasks inside a domain are
drawn uniformly at random with a public seed. No score, outcome, or task
content is used. The same three suites are used for every backbone. The public
manifest exposes Search and Validation suites and a commitment to sealed Test.

## Roles

1. **Evidence Analyst** compares repeated runs of one Search task and emits
   citation-verified findings.
2. **Cross-task Clusterer** groups findings inside one deterministic outcome
   category into recurring behavior modes with membership rules, scope,
   support, and boundary cases. An outcome category is derived from the
   decisive action and machine result. A mode is an open name inside that
   category and never crosses categories.
3. **Harness Engineer** is the only source-aware editing role. It freezes an
   intervention and observable predictions before changing candidate source.
4. **Validator** judges each prediction on one target Search task using
   pre-edit evidence and post-edit repeated executions.

Counting, hashes, citation existence, score aggregation, code checks,
promotion, rollback, stopping, and ledger writes are deterministic programs.
Behavioral interpretation remains with the task-local Analyst or Validator.

## Round

An attempt counts when one L1-valid candidate enters GUI evaluation.

```text
current harness Search/Validation K-runs
  → build or reuse Search findings and modes
  → freeze Engineer intervention
  → L0/L1
  → candidate Search/Validation K-runs
  → utility and behavior gates
  → ledger
  → promote or rollback
```

After promotion, candidate Search runs become evidence under the new harness
and modes are rebuilt. After rollback, current-harness evidence and modes are
reused and the Engineer must propose a materially different edit.

A round is the search at one fixed harness state. Each GUI-evaluated candidate
is one attempt in the current round; a rollback stays in the round and only a
promotion starts the next one. Defaults are `K=3`, at most `B=10` rounds, and at
most `P=5` attempts per round.

## Gates

L0 checks capability paths, diff bounds, syntax, and forbidden task-specific
content. L1 executes configured import, unit, interface, and smoke checks.

For utility \(J\), official promotion requires:

```text
ΔSearch ≥ 0
ΔValidation ≥ 0
max(ΔSearch, ΔValidation) > 0
```

Every required behavioral prediction needs more supported than contradicted
determinate task verdicts. Promotion requires L0, L1, utility, and behavioral
gates. All gates are evaluated independently so a failed candidate still
produces complete feedback.

## Ledger and stopping

Every GUI-evaluated candidate records:

- behavior hash and source patch;
- target modes and pre-registered predictions;
- Search/Validation score changes and largest regressions;
- task-level Validator verdicts and citations;
- gate decisions and effective revalidations;
- structured feedback for the next Engineer.

Optimization stops when all `P` attempts of a round are rejected, or after `B`
rounds. L0/L1 repairs before GUI evaluation consume no attempt. The selected
harness is frozen before sealed Test opens.

## Runtime validity

Each rollout begins from a clean snapshot. Startup failures, missing
screenshots, evaluator failures, and unrecoverable runner exceptions are
invalid and produce a task/run-specific rerun audit for the external evaluator.
Partial scores remain raw utility values. A Search bundle is diagnosed when at
least one valid run scores zero.

## Schemas

Public artifacts use `gui-harvest-*-v1` schema identifiers. The implementation
ships prompts, Harness Card/capability templates, ledger, round records, and a
tool-independent Engineer brief.

