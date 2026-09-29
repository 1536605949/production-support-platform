# -*- coding: utf-8 -*-
"""Verified planning: a plan is a typed, acyclic dependency graph.

Where this sits in the pipeline::

    intent → plan_tools → **verify_plan** → governance → execute

Governance already answered "may this tool be called at all" (whitelist,
argument contract, role, risk/approval, result contract). It could not answer
"is this *sequence* of calls coherent" — because until now there was no
sequence to speak of: ``plan_tools`` returned a flat list of ``(name, args)``
and the loop fired them in order. A flat list cannot express that
``execute_refund`` must consume the ``proposal_id`` that
``create_refund_proposal`` just produced, so nothing could check it.

Two properties this module deliberately keeps:

* **Deterministic.** The plan is built by plain code in
  :func:`gateway.app.core.engine.plan_tools`. The model never picks a tool,
  never orders steps and never chooses a dependency. A verified plan is only
  as trustworthy as its producer, and a model-produced plan would move the
  trust boundary without adding a check.
* **Fail-closed.** :func:`verify_plan` runs before the first tool executes.
  A plan that fails any check produces no business calls at all — not a
  partially executed prefix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Mapping

from pydantic import TypeAdapter

# --------------------------------------------------------------------------- #
# Semantic id types
# --------------------------------------------------------------------------- #
# Every identifier in this system is a string on the wire, which means a type
# contract over ``str`` accepts ``proposal_id`` where ``order_id`` is required.
# That is not a hypothetical: "execute the refund for *this* order" and
# "execute *this* proposal" are different operations, and confusing them is the
# expensive kind of bug on the money path.
#
# So identifiers get nominal types. They are ``str`` subclasses, so they cost
# nothing at runtime and serialise identically, but they are not interchangeable
# with each other or with a bare ``str``.
class OrderId(str):
    """An order identifier. Not a proposal, not a sku."""


class ProposalId(str):
    """A refund proposal identifier. Not an order."""


class SkuId(str):
    """A catalogue entry identifier."""


class UserId(str):
    """An end-customer identifier."""


class TicketId(str):
    """A support ticket identifier."""


class RuleId(str):
    """An after-sale rule identifier."""


#: Refinement applied by field name when reading a declared type.
#:
#: ``args_model`` annotations and business payloads both say plain ``str`` —
#: Pydantic has no way to express "this string is an order id" and neither does
#: JSON. Rather than maintain a second, hand-written copy of every tool's
#: argument types (which would drift from the Pydantic model), the *name*
#: carries the semantics and this table resolves it. One rule, applied
#: uniformly to inputs and outputs, so there is no asymmetry to get wrong.
SEMANTIC_IDS: dict[str, type] = {
    'order_id': OrderId,
    'proposal_id': ProposalId,
    'sku_id': SkuId,
    'user_id': UserId,
    'ticket_id': TicketId,
    'rule_id': RuleId,
}


def refine(name: str, declared: type) -> type:
    """Give a declared type its semantic identity, if it has one.

    Only applies to ``str``-declared fields: a field called ``order_id`` that
    is declared ``int`` is a bug elsewhere and should not be silently relabelled.
    """
    if declared is str:
        return SEMANTIC_IDS.get(name, str)
    return declared


# --------------------------------------------------------------------------- #
# Plan model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InputRef:
    """A tool argument whose value comes from an earlier step's output.

    This is the only way one step can feed another, which is what makes the
    dependency graph derivable instead of declared.
    """

    step_id: str
    field: str

    def describe(self) -> str:
        return f'{self.step_id}.{self.field}'


@dataclass(frozen=True)
class Requirement:
    """A precondition on an earlier step's output.

    Violating it blocks the dependent step. ``blocked_when`` is the value that
    means "do not proceed".

    A **missing** field never blocks. The source step may have failed (the
    after-sale rule service being unreachable is not a refusal), and only a
    definite negative answer should stop the chain. This is the same
    three-valued discipline the rules themselves use, and collapsing it would
    turn "I cannot tell" into "no" — denying returns the shop owes, in a way
    that looks like strictness rather than a bug.
    """

    step_id: str
    field: str
    blocked_when: Any

    def violated(self, value: Any, present: bool) -> bool:
        if not present:
            return False
        if self.blocked_when is None or isinstance(self.blocked_when, bool):
            return value is self.blocked_when
        return value == self.blocked_when


@dataclass(frozen=True)
class PlanStep:
    """One tool call inside a plan.

    ``arguments`` holds literal values only; anything derived from a previous
    step goes in ``inputs``. Keeping the two apart is what lets the verifier
    tell "this argument was written by the planner" from "this argument is
    supplied by step 2" — and therefore check the second kind.
    """

    step_id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, InputRef] = field(default_factory=dict)
    requires: tuple[Requirement, ...] = ()

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Control edges, *derived* from references.

        Never declared separately. A plan that declares ``depends_on: [step-1]``
        while its arguments actually read from ``step-2`` has two different
        answers to "what does this depend on", and whichever the verifier reads
        is the wrong one for some caller. Deriving makes that state
        unrepresentable.
        """
        edges = {ref.step_id for ref in self.inputs.values()}
        edges |= {req.step_id for req in self.requires}
        edges.discard(self.step_id)
        return tuple(sorted(edges))

    def argument_names(self) -> set[str]:
        return set(self.arguments) | set(self.inputs)


@dataclass(frozen=True)
class Plan:
    """A whole turn's tool activity, as a graph."""

    plan_id: str
    steps: tuple[PlanStep, ...] = ()
    intent: str = ''

    def step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def index_of(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.step_id == step_id:
                return i
        return -1

    def tool_names(self) -> tuple[str, ...]:
        return tuple(s.tool for s in self.steps)


# --------------------------------------------------------------------------- #
# Verification result
# --------------------------------------------------------------------------- #
#: Rejection reasons. Stable strings: they end up in metrics labels, logs and
#: the evaluation dataset, so they are part of the contract, not debug text.
REASONS = (
    'unknown_tool',
    'duplicate_step_id',
    'unknown_argument',
    'missing_argument',
    'invalid_arguments',
    'self_dependency',
    'missing_dependency',
    'cycle_detected',
    'invalid_reference',
    'missing_output',
    'type_mismatch',
    'invalid_sequence',
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ''


@dataclass(frozen=True)
class PlanVerification:
    """Outcome of :func:`verify_plan`.

    ``checks`` is the full ledger — every gate and whether it passed — which is
    what the Decision Inspector renders. ``reason`` is the single rejection
    code, chosen by check order so the most fundamental failure is the one
    reported.
    """

    ok: bool
    checks: tuple[CheckResult, ...] = ()
    reason: str = ''
    detail: str = ''
    plan_id: str = ''
    steps: int = 0

    def ledger(self) -> list[dict]:
        return [{'name': c.name, 'ok': c.ok, 'detail': c.detail} for c in self.checks]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _declared_output_type(spec, field_name: str) -> type | None:
    for out in getattr(spec, 'outputs', ()):
        if out.name == field_name:
            return refine(out.name, out.type)
    return None


def _declared_input_type(spec, arg_name: str) -> type | None:
    model = spec.args_model
    info = getattr(model, 'model_fields', {}).get(arg_name)
    if info is None:
        return None
    return refine(arg_name, info.annotation)


def _assignable(source: type, target: type) -> bool:
    """Can a value of ``source`` be used where ``target`` is required?

    Nominal, not structural: ``OrderId`` is a ``str``, but a plain ``str`` is
    not an ``OrderId``. Narrowing (``OrderId`` → ``str``) is fine; widening is
    not, because it loses the guarantee the target relies on.
    """
    if source is target:
        return True
    # ``int`` widens to ``float``. ``bool`` is a subclass of ``int`` in Python,
    # but ``True`` as an amount is a bug, not a number — so it is excluded.
    if source is int and target is float:
        return True
    if issubclass(source, str) and target is str:
        return True
    return False


def _type_name(t: type) -> str:
    return getattr(t, '__name__', str(t))


def _validate_literals(spec, arguments: Mapping[str, Any]) -> str:
    """Validate the literal arguments, field by field. Empty string means OK.

    Deliberately *not* ``args_model.model_validate(arguments)``: fields that are
    supplied by ``inputs`` have no value at verification time, so whole-model
    validation would report those as missing and never reach the literals at
    all. A per-field adapter (the annotation plus whatever constraints Pydantic
    stored as metadata) checks exactly the values the planner actually wrote.

    This has to happen here rather than in governance, because governance runs
    per step: a malformed literal on step 3 would only be caught after steps 1
    and 2 had already produced side effects.
    """
    fields = getattr(spec.args_model, 'model_fields', {})
    for name, value in arguments.items():
        info = fields.get(name)
        if info is None:
            continue
        # ``Annotated[X, *metadata]`` is not valid syntax (a starred expression
        # is rejected in that subscript), so the alias is built through the
        # special form's own entry point — which is what the subscript desugars
        # to anyway. Constraints live in ``FieldInfo.metadata``, so dropping it
        # would silently accept a 1-character ``order_id``.
        parts = (info.annotation, *info.metadata)
        target = Annotated.__class_getitem__(parts) if len(parts) > 1 else info.annotation
        try:
            TypeAdapter(target).validate_python(value)
        except Exception as exc:  # noqa: BLE001 - ValidationError, text only
            return f'{name}={value!r} 不满足契约: {str(exc)[:150]}'
    return ''


def verify_plan(plan: Plan, registry: Mapping[str, Any]) -> PlanVerification:
    """Check a plan is well-formed, acyclic and type-correct.

    Pure: no I/O, no metrics, no database. Everything it needs is the plan and
    the tool registry, which is what makes it exhaustively testable — and what
    lets the engine call it *before* the first tool executes.

    An **empty plan is valid**. Most turns legitimately call no tool at all (a
    knowledge question is answered from retrieved context), so an empty plan is
    the normal case, not a malformed one.
    """
    checks: list[CheckResult] = []

    def reject(reason: str, detail: str) -> PlanVerification:
        checks.append(CheckResult(reason, False, detail))
        return PlanVerification(ok=False, checks=tuple(checks), reason=reason,
                                detail=detail, plan_id=plan.plan_id,
                                steps=len(plan.steps))

    def ok(name: str, detail: str = '') -> None:
        checks.append(CheckResult(name, True, detail))

    if not plan.steps:
        ok('structure', '空 plan：本轮不需要调用工具')
        return PlanVerification(ok=True, checks=tuple(checks), plan_id=plan.plan_id,
                                steps=0)

    # -- 1. step_id uniqueness ------------------------------------------------- #
    seen: set[str] = set()
    for s in plan.steps:
        if s.step_id in seen:
            return reject('duplicate_step_id', f'step_id 重复: {s.step_id}')
        seen.add(s.step_id)
    ok('unique_step_id', f'{len(plan.steps)} 个 step_id 唯一')

    # -- 2. tools exist and arguments fit their contract ----------------------- #
    for s in plan.steps:
        spec = registry.get(s.tool)
        if spec is None:
            return reject('unknown_tool', f'{s.step_id} 调用了未注册工具 {s.tool}')

        given = s.argument_names()
        fields = getattr(spec.args_model, 'model_fields', {})
        unknown = sorted(given - set(fields))
        if unknown:
            return reject('unknown_argument',
                          f'{s.step_id}({s.tool}) 传入未声明参数 {unknown}')

        required = {name for name, info in fields.items() if info.is_required()}
        missing = sorted(required - given)
        if missing:
            return reject('missing_argument',
                          f'{s.step_id}({s.tool}) 缺少必填参数 {missing}')
    ok('tool_and_arguments', f'{len(plan.steps)} 个 step 的工具与参数名合法')

    # -- 3. literal arguments satisfy the Pydantic contract ------------------- #
    # Done here rather than left to governance so that an arity or range error
    # is a *plan* rejection: it must be found before the first tool runs, not
    # halfway through.
    for s in plan.steps:
        spec = registry[s.tool]
        if not s.arguments:
            continue
        problem = _validate_literals(spec, s.arguments)
        if problem:
            return reject('invalid_arguments', f'{s.step_id}({s.tool}) {problem}')
    ok('literal_arguments', '字面参数通过 Pydantic 契约')

    # -- 4. dependencies resolve --------------------------------------------- #
    # Self-references are checked against the raw references rather than through
    # ``PlanStep.dependencies``: that property discards the step's own id (so a
    # self-edge never shows up as an edge), which would make this check
    # unreachable and the rejection code dead weight.
    for s in plan.steps:
        for arg_name, ref in s.inputs.items():
            if ref.step_id == s.step_id:
                return reject('self_dependency',
                              f'{s.step_id}.{arg_name} 引用了自己')
        for req in s.requires:
            if req.step_id == s.step_id:
                return reject('self_dependency',
                              f'{s.step_id} 的前置条件引用了自己')
        for dep in s.dependencies:
            if plan.step(dep) is None:
                return reject('missing_dependency',
                              f'{s.step_id} 依赖不存在的 {dep}')
    ok('dependencies_resolve', '所有依赖都指向本 plan 内的 step')

    # -- 5. acyclicity ------------------------------------------------------- #
    # Checked before the forward-reference rule on purpose: a cyclic graph is
    # *also* a graph with a forward reference, and "cycle" is the more useful
    # thing to report. Kahn's algorithm: repeatedly drop nodes with no
    # remaining unvisited dependency.
    remaining = {s.step_id: set(s.dependencies) for s in plan.steps}
    settled: set[str] = set()
    while True:
        ready = [sid for sid, deps in remaining.items()
                 if sid not in settled and deps <= settled]
        if not ready:
            break
        settled.update(ready)
    if len(settled) != len(plan.steps):
        stuck = sorted(set(remaining) - settled)
        return reject('cycle_detected', f'依赖图存在环，无法拓扑排序: {stuck}')
    ok('acyclic', '依赖图无环')

    # -- 6. declaration order is a valid execution order --------------------- #
    # Execution is declaration order, so every reference must point backwards.
    # Requiring this removes "which order do I run these in" as a question.
    for i, s in enumerate(plan.steps):
        for ref in s.inputs.values():
            if plan.index_of(ref.step_id) > i:
                return reject('invalid_reference',
                              f'{s.step_id} 前向引用 {ref.describe()}'
                              '（执行顺序即声明顺序，引用只能指向更早的 step）')
        for req in s.requires:
            if plan.index_of(req.step_id) > i:
                return reject('invalid_reference',
                              f'{s.step_id} 前向引用 {req.step_id}.{req.field}')
    ok('no_forward_reference', '所有引用都指向更早的 step')

    # -- 7. referenced fields exist and their types match -------------------- #
    for s in plan.steps:
        spec = registry[s.tool]
        for arg_name, ref in s.inputs.items():
            source = plan.step(ref.step_id)
            source_spec = registry[source.tool]
            source_type = _declared_output_type(source_spec, ref.field)
            if source_type is None:
                return reject(
                    'missing_output',
                    f'{s.step_id}.{arg_name} 引用了 {ref.describe()}，'
                    f'但 {source.tool} 的输出契约里没有 {ref.field}')
            target_type = _declared_input_type(spec, arg_name)
            if target_type is None:
                return reject('unknown_argument',
                              f'{s.step_id}({s.tool}) 没有参数 {arg_name}')
            if not _assignable(source_type, target_type):
                return reject(
                    'type_mismatch',
                    f'{s.step_id}.{arg_name} ← {ref.describe()}: '
                    f'expected={_type_name(target_type)} '
                    f'actual={_type_name(source_type)}')
        for req in s.requires:
            source = plan.step(req.step_id)
            source_spec = registry[source.tool]
            if _declared_output_type(source_spec, req.field) is None:
                return reject(
                    'missing_output',
                    f'{s.step_id} 的前置条件引用了 {req.step_id}.{req.field}，'
                    f'但 {source.tool} 的输出契约里没有 {req.field}')
    ok('type_contracts', '跨步引用的字段存在且类型兼容')

    # -- 8. high-risk tools have their precondition ------------------------- #
    # "execute_refund exists" is not enough — it has to be in *this* plan,
    # earlier, or the money path is running on a proposal this turn never
    # established.
    for s in plan.steps:
        spec = registry[s.tool]
        for needed in getattr(spec, 'requires_predecessor', ()):
            if needed not in plan.tool_names():
                return reject(
                    'invalid_sequence',
                    f'{s.step_id}({s.tool}) 缺少前置工具 {needed}')
            if plan.index_of(next(x.step_id for x in plan.steps if x.tool == needed)) > \
                    plan.index_of(s.step_id):
                return reject(
                    'invalid_sequence',
                    f'{s.step_id}({s.tool}) 的前置工具 {needed} 出现在它之后')
    ok('preconditions', '高危工具的前置工具都在更早的位置')

    return PlanVerification(ok=True, checks=tuple(checks), plan_id=plan.plan_id,
                            steps=len(plan.steps))
