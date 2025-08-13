# Pyright control-flow graph (CFG) and type checking integration

This document explains how Pyright constructs, traverses, and uses its control-flow graph (CFG) for type narrowing and reachability. It includes exact references to files and functions in this codebase and Python-like pseudocode for the core algorithms.

- Primary sources in this repo
  - CFG node types and flags: `packages/pyright-internal/src/analyzer/codeFlowTypes.ts`
  - CFG traversal and type narrowing engine: `packages/pyright-internal/src/analyzer/codeFlowEngine.ts`
  - CFG construction (binder/walker): `packages/pyright-internal/src/analyzer/binder.ts`
  - Type evaluator integration: `packages/pyright-internal/src/analyzer/typeEvaluator.ts`
  - CFG formatting (debug): `packages/pyright-internal/src/analyzer/codeFlowUtils.ts`
  - Node-attached analysis info: `packages/pyright-internal/src/analyzer/analyzerNodeInfo.ts`
  - Related helpers: `packages/pyright-internal/src/analyzer/typeGuards.ts`, `packages/pyright-internal/src/analyzer/patternMatching.ts` (used for narrowing callbacks)

Related Pyright docs
- Type narrowing and reachability concepts: `docs/type-concepts-advanced.md` (sections "Type Narrowing", "Reachability", "Narrowing for Implied Else").


## High-level architecture

- The binder (`binder.ts`) walks the parse tree and builds a flow graph of FlowNodes (edges are stored as "antecedents" pointing backward). It also attaches the current flow point to parse nodes via `AnalyzerNodeInfo.setFlowNode` and end-of-block nodes via `setAfterFlowNode`.
- The type evaluator (`typeEvaluator.ts`) asks a `CodeFlowEngine` (`codeFlowEngine.ts`) to compute:
  - The narrowed type of an expression at a program point (using `CodeFlowAnalyzer.getTypeFromCodeFlow`).
  - The reachability of a node (using `CodeFlowEngine.getFlowNodeReachability`).
- Narrowing walks backward through the CFG from the current node to its antecedents, applying assignment effects and conditional/pattern guards; branches are unioned; loops use a fixed-point iteration with convergence limits; try/finally and context managers add special gates.


## CFG node kinds and flags (where defined)

- File: `codeFlowTypes.ts`
  - Enum `FlowFlags` defines node kinds including Start, BranchLabel, LoopLabel, Assignment, Unbind, WildcardImport, TrueCondition, FalseCondition, Call, PreFinallyGate, PostFinally, VariableAnnotation, PostContextManager, TrueNeverCondition, FalseNeverCondition, NarrowForPattern, ExhaustedMatch.
  - Node interfaces: `FlowNode`, `FlowLabel`/`FlowBranchLabel`, `FlowAssignment`, `FlowVariableAnnotation`, `FlowWildcardImport`, `FlowCondition`, `FlowNarrowForPattern`, `FlowExhaustedMatch`, `FlowCall`, `FlowPreFinallyGate`, `FlowPostFinally`, `FlowPostContextManagerLabel`.
  - Reference-key helpers: `isCodeFlowSupportedForReference`, `createKeyForReference`, `createKeysForReferenceSubexpressions`.

Key idea: nodes reference antecedents (previous flow points), enabling efficient backward traversal from a point-of-use.


## CFG construction (binder)

- File: `binder.ts`
  - Per execution scope (module/function/lambda), the binder maintains `_currentFlowNode` and attaches it to parse nodes: `AnalyzerNodeInfo.setFlowNode(node, this._currentFlowNode!)`.
  - Entry/start nodes:
    - `_createStartFlowNode()` sets `FlowFlags.Start` when entering a module or function. Example: module and function bind paths.
  - Branches and loops:
    - `_createBranchLabel` (creates `FlowBranchLabel`), `_finishFlowLabel`, `_addAntecedent`, `_createLoopLabel` (for `while`, `for`, comprehensions), `_bindLoopStatement`.
    - `visitIf`, `visitWhile`, `visitTernary`, logical ops (`visitBinaryOperation`/`visitUnaryOperation`) wire true/false paths using `_bindConditional` and `_createFlowConditional` that produce `FlowCondition` nodes with `TrueCondition`/`FalseCondition`/`TrueNeverCondition`/`FalseNeverCondition` flags.
  - Assignments and variable annotations:
    - `_createFlowAssignment` builds `FlowAssignment` nodes (with optional `Unbind` for deletions). `_createVariableAnnotationFlowNode` adds `FlowVariableAnnotation` to separate annotation and name binding.
  - Calls and exceptions:
    - `_createCallFlowNode` emits `FlowCall` and marks that exceptions can route to except targets.
    - `visitRaise` and return paths connect to structural unreachable nodes and finally targets.
  - Try/except/finally:
    - `visitTry` builds the special "finally gate": emits `FlowPreFinallyGate` and `FlowPostFinally` (see ASCII diagram comment in `visitTry`).
  - With/async with:
    - `visitWith` builds `FlowPostContextManagerLabel` nodes that model context managers that may swallow exceptions, interpreted by the engine.
  - Match/case and pattern narrowing:
    - `visitMatch` emits per-case narrowing nodes `FlowNarrowForPattern` and a closing `FlowExhaustedMatch` gate if the match can be proven exhaustive.
  - Misc:
    - Wildcard import: `_createFlowWildcardImport` builds `FlowWildcardImport` nodes.
  - Complexity tracking (to short-circuit huge graphs): `_codeFlowComplexity` updated in `_finishFlowLabel` and per-node; recorded via `AnalyzerNodeInfo.setCodeFlowComplexity`.

References
- Start node: `_createStartFlowNode` (binder.ts)
- Conditional wiring: `_bindConditional`, `_createFlowConditional` (binder.ts)
- If/While: `visitIf`, `visitWhile` (binder.ts)
- For/Comprehensions: `visitFor`, `visitComprehension`, `_bindLoopStatement`, `_createLoopLabel` (binder.ts)
- Try/finally: `visitTry` and nodes `FlowPreFinallyGate`, `FlowPostFinally` (binder.ts/codeFlowTypes.ts)
- With: `visitWith`, context manager label creation (binder.ts)
- Match/pattern: `visitMatch`, `_createFlowNarrowForPattern`, `_createFlowExhaustedMatch` (binder.ts)
- Assignments: `_createFlowAssignment` (binder.ts)
- Variable annotations: `_createVariableAnnotationFlowNode` (binder.ts)
- Call nodes: `_createCallFlowNode` (binder.ts)
- Wildcard imports: `_createFlowWildcardImport` (binder.ts)


## How the type evaluator uses the CFG

- File: `typeEvaluator.ts`
  - Creates a `CodeFlowEngine` via `getCodeFlowEngine(evaluator, speculativeTypeTracker)` and caches a `CodeFlowAnalyzer` per execution scope and starting type (`getCodeFlowAnalyzerForNode`).
  - Entry points that call into the engine:
    - Narrowing for a reference: `getFlowTypeOfReference(reference, startNode?, options?)` → obtains `flowNode` via `AnalyzerNodeInfo.getFlowNode`, checks scope’s `codeFlowExpressions` set, then calls `CodeFlowAnalyzer.getTypeFromCodeFlow(flowNode, reference, options)`.
    - Reachability queries: `getNodeReachability`, `getAfterNodeReachability` use `codeFlowEngine.getFlowNodeReachability`.
  - Complexity limit:
    - `checkCodeFlowTooComplex(node)` consults `AnalyzerNodeInfo.getCodeFlowComplexity(scopeNode)` vs `maxCodeComplexity` to short-circuit overly-complex graphs.
  - Special usages:
    - Return type inference for unannotated functions uses code flow limits like `maxReturnTypeInferenceCodeFlowComplexity` and `maxReturnCallSiteTypeInferenceCodeFlowComplexity`.
    - `codeFlowAnalyzerCache` stores analyzers keyed by execution scope id and optional `typeAtStart` to stabilize narrowing across evaluations.

Key references in `typeEvaluator.ts`
- `getFlowTypeOfReference` (calls `getCodeFlowAnalyzerForNode`, `analyzer.getTypeFromCodeFlow`)
- `getNodeReachability`, `getAfterNodeReachability` (call `codeFlowEngine.getFlowNodeReachability`)
- `getCodeFlowAnalyzerForNode`, `codeFlowAnalyzerCache` (analyzer caching)
- `checkCodeFlowTooComplex`, `maxCodeComplexity`


## The Code Flow Engine: core algorithms

- File: `codeFlowEngine.ts`
  - Factory: `getCodeFlowEngine(evaluator, speculativeTypeTracker)` returns a `CodeFlowEngine` with:
    - `createCodeFlowAnalyzer()` → `CodeFlowAnalyzer` exposing `getTypeFromCodeFlow(flowNode, reference|undefined, options?)`.
    - `getFlowNodeReachability(flowNode, sourceFlowNode?, ignoreNoReturn?)` → reachability over the CFG.
    - `narrowConstrainedTypeVar(flowNode, typeVar)` → narrows constrained TypeVars across guards/patterns.
    - `printControlFlowGraph` → debug printing with `formatControlFlowGraph`.

### getTypeFromCodeFlow: backward traversal with narrowing and caching

- Per-reference caches
  - A separate cache per reference key: combines `createKeyForReference(reference)` and an optional `targetSymbolId` to scope cached types.
  - Caches entries as either a concrete `Type` or an `IncompleteType` record with `incompleteSubtypes` for loops.
- Walk-from-current algorithm
  - Starting at a `flowNode`, repeatedly:
    - Return cached type if available (respecting “pending”/incomplete generation guards to avoid recursion and churn).
    - Short-circuit on `Unreachable*` nodes with `Never`.
    - Skip-through linear nodes updating state:
      - `FlowVariableAnnotation`, `FlowWildcardImport`, `FlowAssignment.antecedent`, `FlowCall.antecedent`, etc.
      - `FlowCall`: if `isCallNoReturn` true, stop exploration on this path (treat as unreachable upstream).
      - `FlowAssignment` matching the reference: evaluate RHS type via `evaluator.evaluateTypesForStatement`, handle `Unbind` (yield `UnboundType` except for index/member deletes), handle partial-match kill of prior narrowings; special-case `TypedDict` key assignment narrowing when target is `x['literal_key']`.
      - Conditional nodes `FlowCondition` with `True/False`/`TrueNever/FalseNever`:
        - If reference matches, compute `typeNarrowingCallback` to refine the antecedent type; if `Never` under implied-else gate, block further exploration.
      - Pattern nodes:
        - `FlowNarrowForPattern`: evaluate case/match for subject narrowing; can also narrow subexpressions in `subject`.
        - `FlowExhaustedMatch`: if subject narrowed to `Never` at bottom, gate further exploration; otherwise project narrowed type to subexpressions.
      - Finally-gates:
        - `FlowPreFinallyGate`/`FlowPostFinally`: toggles a closed-gate set and uses speculative mode inside `finally` to evaluate “gate-open” types safely.
      - Context managers:
        - `FlowPostContextManagerLabel`: consults `isExceptionContextManager` to decide if this path should be blocked based on `blockIfSwallowsExceptions`.
      - Start node: return `options.typeAtStart` if provided.
    - Join nodes:
      - `FlowBranchLabel`: union of types from all antecedents.
      - `FlowLabel` with `LoopLabel`: fixed-point over antecedents; track per-antecedent incomplete subtype entries; apply a convergence limit; remove “incomplete unknowns” to encourage convergence.

Pseudocode sketch (Python-like, simplified):

```python
# Inputs: flowNode, reference (may be None), options
# Output: TypeResult(type, isIncomplete)

def getTypeFromCodeFlow(flowNode, reference, options):
    cache = get_cache_for(reference_key(reference, options.target_symbol_id))

    def get_from_node(node):
        while True:
            entry = cache.lookup(node)
            if entry.is_complete():
                return entry
            if entry.is_incomplete_for_current_gen():
                return entry.clean_union()
            if cache.is_pending(node):
                return Unknown(incomplete=True)

            if node.has_flag('UnreachableStructural') or node.has_flag('UnreachableStaticCondition'):
                return cache.store(node, Never)

            if node.is_any('VariableAnnotation', 'WildcardImport'):
                node = node.antecedent; continue

            if node.is_call():
                if is_call_noreturn(node):
                    return cache.store(node, None)  # stop path
                node = node.antecedent; continue

            if node.is_assignment():
                if matches_reference(node.target, reference, options.target_symbol_id):
                    if node.is_unbind and not is_index_or_member_delete(reference):
                        return cache.store(node, Unbound)
                    rhs = eval_rhs_type(node)
                    return cache.store(node, rhs.type, incomplete=rhs.isIncomplete)
                if partial_match(node.target, reference):
                    return options.typeAtStart
                node = node.antecedent; continue

            if node.is_branch_label():
                if reference and unaffected_by_branch(node, reference):
                    node = node.pre_branch_antecedent; continue
                return join([get_from_node(a) for a in node.antecedents])

            if node.is_loop_label():
                return solve_loop(node)

            if node.is_condition():
                if reference and not options.skipConditionalNarrowing:
                    narrowed = apply_narrowing(reference, node.expression, positive=node.is_true())
                    if narrowed is not None:
                        return cache.store(node, narrowed.type, incomplete=narrowed.isIncomplete)
                node = node.antecedent; continue

            if node.is_narrow_for_pattern():
                res = eval_case_or_match(node.statement)
                if not reference:
                    if is_never(res.type):
                        return cache.store(node, None, incomplete=res.isIncomplete)
                else:
                    narrowed = project_subject_narrowing(reference, node.subject, res.type)
                    return cache.store(node, narrowed, incomplete=res.isIncomplete)
                node = node.antecedent; continue

            if node.is_exhausted_match():
                subj = eval_match(node).type
                if is_never(subj):
                    return cache.store(node, Never)
                if reference:
                    narrowed = project_subject_narrowing(reference, node.subject_expression, subj)
                    return cache.store(node, narrowed)
                node = node.antecedent; continue

            if node.is_pre_finally_gate():
                return evaluate_gate_closed(node)

            if node.is_post_finally():
                return evaluate_gate_open(node)

            if node.is_post_context_manager_label():
                if should_block_path(node):
                    return cache.store(node, None)

            if node.is_start():
                return cache.store(node, options.typeAtStart.type, incomplete=options.typeAtStart.isIncomplete)

            raise AssertionError('Unexpected FlowNode kind')

    return get_from_node(flowNode)
```

### getFlowNodeReachability: structural + type-based reachability

- Walks backward like `getTypeFromCodeFlow` but returns a `Reachability` enum and memoizes per `(flowNode, sourceFlowNode?)`.
- Short-circuits with:
  - Structural unreachable flags, unconditional Start checks, and caching.
  - NoReturn calls (`FlowCall`) unless `ignoreNoReturn=True`.
  - `FlowPostContextManagerLabel` when no context manager swallows exceptions.
  - Conditional narrowing where the type becomes `Never`.
  - Pattern narrowing resulting in `Never`.
- Joins at labels consider whether any antecedent is reachable; otherwise preserves reason (by-analysis vs static-condition vs structural).

Pseudocode sketch:

```python
def getFlowNodeReachability(flowNode, sourceFlowNode=None, ignoreNoReturn=False):
    visited = set(); closed_finally = set(); cache = {}

    def reach(node):
        if (node, sourceFlowNode) in cache: return cache[(node, sourceFlowNode)]
        if node in visited: return 'UnreachableStructural'
        visited.add(node)

        if node.has_flag('UnreachableStructural'): return store(node, 'UnreachableStructural')
        if node.has_flag('UnreachableStaticCondition'): return store(node, 'UnreachableStaticCondition')
        if node is sourceFlowNode: return store(node, 'Reachable')

        if node.is_any('VariableAnnotation','Assignment','WildcardImport','ExhaustedMatch'):
            return tail(node.antecedent)

        if node.is_narrow_for_pattern():
            t = eval_case_or_match(node.statement)
            if is_never(t.type): return store(node, 'UnreachableByAnalysis')
            return tail(node.antecedent)

        if node.is_condition():
            if narrows_to_never(node):
                return store(node, 'UnreachableByAnalysis')
            return tail(node.antecedent)

        if node.is_call() and not ignoreNoReturn and is_call_noreturn(node):
            return store(node, 'UnreachableByAnalysis')

        if node.is_label():
            statuses = [reach(a) for a in node.antecedents]
            if any(s == 'Reachable' for s in statuses): return store(node, 'Reachable')
            if any(s == 'UnreachableByAnalysis' for s in statuses): return store(node, 'UnreachableByAnalysis')
            if any(s == 'UnreachableStaticCondition' for s in statuses): return store(node, 'UnreachableStaticCondition')
            return store(node, 'UnreachableStructural')

        if node.is_start():
            return store(node, 'Reachable' if sourceFlowNode is None else 'UnreachableByAnalysis')

        if node.is_pre_finally_gate():
            if node.id in closed_finally: return store(node, 'UnreachableByAnalysis')
            return tail(node.antecedent)

        if node.is_post_finally():
            was_closed = node.pre_finally_id in closed_finally
            closed_finally.add(node.pre_finally_id)
            try:
                return store(node, reach(node.antecedent))
            finally:
                if not was_closed: closed_finally.remove(node.pre_finally_id)

        raise AssertionError

    def store(node, status):
        cache[(node, sourceFlowNode)] = status
        return status

    def tail(next_node):
        return reach(next_node)

    return reach(flowNode)
```

### Other algorithms in the engine

- `narrowConstrainedTypeVar(flowNode, typeVar)`:
  - Traverses back through guards and pattern matches to reduce a constrained TypeVar to a single remaining constraint when possible (e.g., `isinstance(x, C)` filters constraints to those compatible with `C`). Handles labels/loops, Pre/PostFinally, and ignores nodes that don’t affect the TypeVar.
- `isCallNoReturn` and `isFunctionNoReturn`:
  - Determines if a call/site is effectively NoReturn (declared `NoReturn`; overloads `NoReturn`; coroutine third type arg `Never`; or flow proves function never reaches after-node). Caches per call node id.
- `isExceptionContextManager`:
  - Detects context managers whose `__exit__`/`__aexit__` return a truthy `bool`, indicating exception swallowing.


## Keys that connect binder and evaluator

- `AnalyzerNodeInfo.setFlowNode` / `getFlowNode`, `setAfterFlowNode` / `getAfterFlowNode` attach the current and end-of-block flow nodes to parse nodes.
- `AnalyzerNodeInfo.setCodeFlowExpressions` stores a per-scope set of reference keys that actually participate in code flow. `typeEvaluator` uses this set to skip unnecessary flow analysis.
- `AnalyzerNodeInfo.setCodeFlowComplexity` and `maxCodeComplexity` act as a guardrail against pathological graphs.


## Debugging: printing the CFG

- File: `codeFlowUtils.ts`, exported `formatControlFlowGraph(flowNode: FlowNode)` produces an ASCII rendering of the graph rooted at a node, with labels for node kinds.
- Engine hook: `codeFlowEngine.printControlFlowGraph` calls this utility (behind an internal flag).


## End-to-end flow

1) Parse → `binder.ts` constructs the CFG, attaching flow nodes to parse nodes and tracking affected reference keys and complexity.
2) Type evaluation → `typeEvaluator.ts` checks complexity and whether the reference participates in code flow, then calls `CodeFlowAnalyzer.getTypeFromCodeFlow` with the node attached to that parse node.
3) `codeFlowEngine.ts` traverses backward, applying assignments/guards, narrowing, handling branches/loops/finally/context managers, pattern matching, and NoReturn detection.
4) Results are memoized to avoid recomputation and prevent recursion.


## Exact references (file and symbol)

- CFG node definitions: `analyzer/codeFlowTypes.ts`
  - `FlowFlags`, `FlowNode`, `FlowLabel`, `FlowBranchLabel`, `FlowAssignment`, `FlowVariableAnnotation`, `FlowWildcardImport`, `FlowCondition`, `FlowNarrowForPattern`, `FlowExhaustedMatch`, `FlowCall`, `FlowPreFinallyGate`, `FlowPostFinally`, `FlowPostContextManagerLabel`
  - `isCodeFlowSupportedForReference`, `createKeyForReference`, `createKeysForReferenceSubexpressions`
- CFG construction (binder): `analyzer/binder.ts`
  - Start: `_createStartFlowNode`
  - Labels: `_createBranchLabel`, `_createLoopLabel`, `_finishFlowLabel`, `_addAntecedent`, `_bindLoopStatement`
  - Conditions: `_bindConditional`, `_createFlowConditional`, `_bindNeverCondition`
  - Visitors building graph: `visitIf`, `visitWhile`, `visitFor`, `visitComprehension`, `visitTernary`, `visitBinaryOperation`, `visitUnaryOperation`, `visitTry`, `visitWith`, `visitMatch`, `visitExcept`, `visitRaise`, `visitReturn`
  - Assignment/annotation/call/import: `_createFlowAssignment`, `_createVariableAnnotationFlowNode`, `_createCallFlowNode`, `_createFlowWildcardImport`, `_createFlowNarrowForPattern`, `_createFlowExhaustedMatch`
  - Complexity: `_codeFlowComplexity` and `AnalyzerNodeInfo.setCodeFlowComplexity`
- Engine and algorithms: `analyzer/codeFlowEngine.ts`
  - Factory: `getCodeFlowEngine`, analyzer: `createCodeFlowAnalyzer`
  - Narrowing: `getTypeFromCodeFlow` and helpers for branch/loop/finally nodes
  - Reachability: `getFlowNodeReachability`
  - TypeVar narrowing: `narrowConstrainedTypeVar`
  - NoReturn detection: `isCallNoReturn`, `isFunctionNoReturn`
  - Context manager detection: `isExceptionContextManager`
  - Debug: `printControlFlowGraph`
- Type evaluator integration: `analyzer/typeEvaluator.ts`
  - Engine creation and caching: `getCodeFlowEngine(...)`, `getCodeFlowAnalyzerForNode`, `codeFlowAnalyzerCache`
  - Main use sites: `getFlowTypeOfReference`, `getNodeReachability`, `getAfterNodeReachability`
  - Complexity guard: `checkCodeFlowTooComplex`, constants `maxCodeComplexity`, `maxReturnTypeInferenceCodeFlowComplexity`, `maxReturnCallSiteTypeInferenceCodeFlowComplexity`
- CFG print utility: `analyzer/codeFlowUtils.ts` → `formatControlFlowGraph`
- Node info attachment: `analyzer/analyzerNodeInfo.ts` → `setFlowNode`, `getFlowNode`, `setAfterFlowNode`, `setCodeFlowExpressions`, `setCodeFlowComplexity`


## Notes on performance and limits

- Complexity limits in `typeEvaluator.ts` avoid high-cost analyses: if exceeded, flow analysis is skipped and results are marked incomplete.
- Loop convergence uses per-antecedent incomplete entries and a convergence attempt limit; “incomplete unknown” subtypes may be removed to help convergence.
- Per-reference caches prevent repeated traversals; `pending` guards avoid re-entrant recursion.


## Appendix: related conceptual docs

- Repo doc: `docs/type-concepts-advanced.md` → Sections "Type Narrowing", "Narrowing for Implied Else", and "Reachability".
- Online docs: https://microsoft.github.io/pyright/#/docs/type-concepts-advanced and https://microsoft.github.io/pyright/#/docs/internals
