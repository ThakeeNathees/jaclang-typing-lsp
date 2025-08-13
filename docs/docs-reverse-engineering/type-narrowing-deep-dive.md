# Pyright Type Narrowing: An End-to-End Deep Dive

This document explains how type narrowing works in Pyright: where it’s triggered, how the binder wires narrowing-capable expressions into the control-flow graph (CFG), how the code flow engine applies narrowing, and the algorithms behind common narrowing forms (truthiness, equality/identity, isinstance/issubclass, user TypeGuard/TypeIs, container membership, discriminated unions, typing.TypedDict, and pattern matching). Citations include file paths and primary function names.

- Core sources
  - CFG engine: `packages/pyright-internal/src/analyzer/codeFlowEngine.ts`
  - Narrowing rules: `packages/pyright-internal/src/analyzer/typeGuards.ts`
  - Pattern matching narrowing: `packages/pyright-internal/src/analyzer/patternMatching.ts`
  - Binder wiring (collects narrowing expressions for code flow): `packages/pyright-internal/src/analyzer/binder.ts`
  - Evaluator integration: `packages/pyright-internal/src/analyzer/typeEvaluator.ts`
  - Concepts: `docs/type-concepts-advanced.md` (Type Narrowing; Implied Else; Reachability)


## Where narrowing comes from in the code

- binder.ts
  - `_isNarrowingExpression(...)` identifies expressions that can narrow (names, member access, index, and negations, with pragmatic filters for Never-narrowing performance) and records them as code-flow-tracked “reference expressions”.
  - Conditional and pattern constructs (visitIf/While/Binary/Unary/Match/With/etc.) generate `FlowCondition`, `FlowNarrowForPattern`, `FlowExhaustedMatch`, and other nodes used later by the engine for narrowing.

- codeFlowEngine.ts
  - `getCodeFlowEngine(...).createCodeFlowAnalyzer().getTypeFromCodeFlow(flowNode, reference, options)` traverses backward and calls into narrowing callbacks:
    - `getTypeNarrowingCallback(...)` for boolean/equality/identity/isinstance/TypeGuard/etc.
    - `getPatternSubtypeNarrowingCallback(...)` from patternMatching.ts to project narrowing to subject subexpressions.
  - Handles implied-else (`TrueNeverCondition`/`FalseNeverCondition`) and gates (finally/context managers) that affect whether a path continues.

- typeGuards.ts
  - `getTypeNarrowingCallback(...)` returns a function that, given the current type of the reference, returns a narrowed type for positive or negative tests.
  - Implements specific narrowing helpers like `narrowTypeForTruthiness`, `narrowTypeForInstanceOrSubclass` (isinstance/issubclass/TypeIs), discriminated-literal/None field comparisons, container membership, typed-dict key/value tests, tuple None-index tests, and aliasing of conditions.

- patternMatching.ts
  - `narrowTypeBasedOnPattern(...)` and helpers implement narrowing for PEP 634 match/case patterns (sequence, literal, class, mapping, value), with safeguards against combinatorial blow-up.


## Narrowing pipeline at a glance

1) Binder detects a narrowing-capable test and records reference expressions on the current flow label. Key function: `Binder._isNarrowingExpression`.
2) The binder emits a control-flow node for the test (e.g., `FlowCondition` or `FlowNarrowForPattern`).
3) The evaluator requests `getTypeFromCodeFlow` for a reference at a program point; the engine walks backward through flow nodes.
4) When it encounters a test that affects the reference, the engine computes a narrowing callback from `typeGuards.getTypeNarrowingCallback` (or pattern callbacks) and applies it to the current/reference type, producing a narrowed type.
5) Narrowed types from different branches are unioned; loops use fixed-point iteration; special gates may stop or alter traversal.


## Key APIs and where they live

- Engine application sites (codeFlowEngine.ts)
  - Truthy/falsey conditions: `getTypeFromCodeFlow` checks `FlowCondition` and calls `getTypeNarrowingCallback(...)`.
  - Pattern narrowing: `getTypeFromCodeFlow` handles `FlowNarrowForPattern` and `FlowExhaustedMatch`, calling `getPatternSubtypeNarrowingCallback` to project narrowing to subexpressions.
  - TypedDict key assignment: special handling when the reference is an index with a literal key.

- Narrowing callbacks (typeGuards.ts)
  - `getTypeNarrowingCallback(evaluator, reference, testExpression, isPositiveTest, recursionCount?)`
    - Truthiness: `narrowTypeForTruthiness`.
    - Equality/identity with None, literals, discriminated fields: `narrowTypeForIsNone`, `narrowTypeForDiscriminatedLiteralFieldComparison`, `narrowTypeForDiscriminatedFieldNoneComparison`.
    - isinstance/issubclass/TypeIs: `getIsInstanceClassTypes`, `narrowTypeForInstanceOrSubclass` (internal: `narrowTypeForInstance`, `narrowTypeForInstanceOrSubclassInternal`, plus Callable/protocol special cases).
    - User-defined TypeGuard/TypeIs: `narrowTypeForUserDefinedTypeGuard`.
    - Container membership and element projection: `getElementTypeForContainerNarrowing`, `narrowTypeForContainerElementType`.
    - Aliased conditions and assignment expressions: `getTypeNarrowingCallbackForAliasedCondition`, `getTypeNarrowingCallbackForAssignmentExpression`.

- Pattern narrowing (patternMatching.ts)
  - `narrowTypeBasedOnPattern` orchestrates narrowing for match/case patterns. The engine uses companion callbacks for subexpression projection.


## Pseudocode: getTypeNarrowingCallback usage in the engine

```python
# Inside codeFlowEngine.getTypeFromCodeFlow when hitting a FlowCondition
if not options.skipConditionalNarrowing and reference is not None:
    callback = getTypeNarrowingCallback(evaluator, reference, condition.expression, is_positive(condition))
    if callback:
        # Two forms: either narrow the flow-in type (if ref is implicit) or
        # narrow the ref’s current type when we have a direct reference
        if condition.reference is None:
            flow_in = getTypeFromFlowNode(condition.antecedent)
            narrowed = callback(flow_in.type)
            if narrowed:
                return store(narrowed.type, incomplete=flow_in.isIncomplete or narrowed.isIncomplete)
        else:
            ref_info = evaluator.getTypeOfExpression(condition.reference)
            narrowed = callback(ref_info.type)
            if narrowed:
                return store(narrowed.type, incomplete=ref_info.isIncomplete or narrowed.isIncomplete)
```


## Truthiness narrowing

- Where: `typeGuards.ts` → `narrowTypeForTruthiness`
- Trigger: `if x:`, `if not x:`
- Behavior: Removes falsy or truthy components per branch using evaluator’s truthiness rules.

Pseudocode:
```python
def narrow_type_for_truthiness(type, is_positive):
    result = []
    for t in subtypes(type):
        if is_positive and can_be_truthy(t):
            result.append(remove_falsiness(t))
        elif not is_positive and can_be_falsy(t):
            result.append(remove_truthiness(t))
    return union(result)
```


## Equality/identity narrowing (None, literals, discriminated unions)

- Where: `typeGuards.ts` → helpers like `narrowTypeForIsNone`, `narrowTypeForDiscriminatedLiteralFieldComparison`, `narrowTypeForDiscriminatedFieldNoneComparison`, TypedDict/Tuple discriminated comparisons.
- Trigger: `x is None`, `x == Literal[...]`, `obj.tag == 'A'`, `d['k'] == 'v'`.
- Behavior: Filters union members by compatibility with the discriminant and refines the member types accordingly.


## isinstance / issubclass / TypeIs

- Where: `typeGuards.ts` → `getIsInstanceClassTypes`, `narrowTypeForInstanceOrSubclass`, internals `narrowTypeForInstance`, `narrowTypeForInstanceOrSubclassInternal`.
- Trigger: `isinstance(x, C)`, `issubclass(T, C)`, `typeguard(x) -> TypeIs[...]`.
- Behavior: Filters the reference type by the class filters, honoring subclass/instance semantics, protocols, Callable, and special forms. TypeIs keeps type variables “bound” differently than isinstance.

Pseudocode (simplified):
```python
def narrow_instance_or_subclass(var_type, class_filters, is_positive, is_instance_check, is_type_is):
    if not is_positive:
        return remove_assignable(var_type, class_filters)
    kept = []
    for vt in subtypes(var_type):
        if assignable(vt, class_filters, allow_subclasses=is_instance_check):
            kept.append(bind_typevars(vt) if not is_type_is else vt)
    return union(kept)
```


## User-defined TypeGuard / TypeIs

- Where: `typeGuards.ts` → recognition in `getTypeNarrowingCallback`, narrowing in `narrowTypeForUserDefinedTypeGuard`.
- Behavior: For TypeGuard[T], positive path narrows to T; negative path keeps original (non-strict). For TypeIs[T], applies “strict” behavior and preserves bindings as required.


## Container membership and element projection

- Where: `typeGuards.ts` → `getElementTypeForContainerNarrowing`, `narrowTypeForContainerElementType`.
- Trigger: `if elem in container:` or derived element-type checks.
- Behavior: Infers element types from specialized containers and narrows reference types accordingly, with safety checks for disjointness and literals.


## TypedDict-specific narrowing

- Where: `typeGuards.ts` → `narrowTypeForTypedDictKey`, `narrowTypeForDiscriminatedDictEntryComparison`; engine also narrows on literal-key assignments and comparisons.
- Behavior: Narrows a TypedDict to retain/adjust entry types when keys are tested or assigned literal values; respects extra/required entries and discriminated unions over keys.


## Pattern matching (PEP 634)

- Where: `patternMatching.ts` → `narrowTypeBasedOnPattern` and helpers; engine integration via `FlowNarrowForPattern` and `FlowExhaustedMatch` with `getPatternSubtypeNarrowingCallback`.
- Behavior: Narrows the subject type (and can project to subexpressions) based on the matched pattern: sequences, classes, literals, mappings, as-patterns, value patterns. Includes performance caps like `maxSequencePatternTupleExpansionSubtypes`.


## Aliased conditions and assignment expression tests

- Where: `typeGuards.ts` → `getTypeNarrowingCallbackForAliasedCondition`, `getTypeNarrowingCallbackForAssignmentExpression`.
- Trigger: `if (n := expr): ...`, or if/while condition bound to a local that is then tested elsewhere.
- Behavior: Traces through aliasing to reuse the same narrowing effect.


## Integration with CFG and evaluator

- Engine entry: `codeFlowEngine.getCodeFlowEngine(...).createCodeFlowAnalyzer().getTypeFromCodeFlow(...)`.
- Evaluator call sites: `typeEvaluator.ts` → `getFlowTypeOfReference` uses flow only if the binder recorded the expression and complexity thresholds allow.
- Performance controls: complexity limits in evaluator, recursion prevention and fixed-point convergence in engine, and empirical caps in patternMatching.


## Tests to consult

- `packages/pyright-internal/src/tests/samples/` contains many sample files: `typeGuard*.py`, `typeNarrowing*.py`, `TypeNarrowingFalsy*`, `typeNarrowingCallable*`, TypedDict-related samples.
- `packages/pyright-internal/src/tests/typeEvaluator1.test.ts` references these samples; use them as ground truth for narrowing behaviors.


## Quick references (files and symbols)

- Binder: `_isNarrowingExpression`, `visitIf`, `visitWhile`, `visitBinaryOperation`, `visitUnaryOperation`, `visitMatch`, `_createFlowConditional`, `_createFlowNarrowForPattern`, `_createFlowExhaustedMatch`.
- Engine: `getTypeFromCodeFlow`, `getPatternSubtypeNarrowingCallback`, TypedDict assignment handling.
- Type guards: `getTypeNarrowingCallback`, `narrowTypeForTruthiness`, `narrowTypeForInstanceOrSubclass`, `narrowTypeForUserDefinedTypeGuard`, container/typed-dict/discriminant helpers.
- Pattern matching: `narrowTypeBasedOnPattern`, helpers for sequence/literal/class/mapping/value patterns.
- Evaluator integration: `getFlowTypeOfReference`, `checkCodeFlowTooComplex`.
