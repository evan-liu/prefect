"""
Orion's flow- and task- run orchestration machinery.
"""

import contextlib
from types import TracebackType
from typing import Dict, Iterable, List, Optional, Type, Union

import sqlalchemy as sa

from pydantic import Field

from prefect.orion.models import orm, flow_runs
from prefect.orion.schemas import states
from prefect.orion.schemas.responses import (
    SetStateStatus,
    StateAbortDetails,
    StateAcceptDetails,
    StateRejectDetails,
    StateWaitDetails,
)
from prefect.orion.utilities.schemas import PrefectBaseModel

# all valid state types in the context of a task- or flow- run transition
ALL_ORCHESTRATION_STATES = {*states.StateType, None}

# all terminal states
TERMINAL_STATES = states.TERMINAL_STATES


StateResponseDetails = Union[
    StateAcceptDetails, StateWaitDetails, StateRejectDetails, StateAbortDetails
]


class OrchestrationResult(PrefectBaseModel):
    """
    A container for the output of state orchestration.
    """

    state: Optional[states.State]
    status: SetStateStatus
    details: StateResponseDetails


class OrchestrationContext(PrefectBaseModel):
    """
    A container for a state transition, governed by orchestration rules.

    Note:
        An `OrchestrationContext` should not be instantiated directly, instead
        use the flow- or task- specific subclasses, `FlowOrchestrationContext` and
        `TaskOrchestrationContext`.

    When a flow- or task- run attempts to change state, Orion has an opportunity
    to decide whether this transition can proceed. All the relevant information
    associated with the state transition is stored in an `OrchestrationContext`,
    which is subsequently governed by nested orchestration rules implemented using
    the `BaseOrchestrationRule` ABC.

    `OrchestrationContext` introduces the concept of a state being `None` in the
    context of an intended state transition. An initial state can be `None` if a run
    is is attempting to set a state for the first time. The proposed state might be
    `None` if a rule governing the transition determines that no state change
    should occur at all and nothing is written to the database.

    Attributes:
        session: a SQLAlchemy database session
        initial_state: the initial state of a run
        proposed_state: the proposed state a run is transitioning into
        validated_state: a proposed state that has committed to the database
        rule_signature: a record of rules that have fired on entry into a
            managed context, currently only used for debugging purposes
        finalization_signature: a record of rules that have fired on exit from a
            managed context, currently only used for debugging purposes
        response_status: a SetStateStatus object used to build the API response
        response_details:a StateResponseDetails object use to build the API response

    Args:
        session: a SQLAlchemy database session
        initial_state: the initial state of a run
        proposed_state: the proposed state a run is transitioning into
    """

    class Config:
        arbitrary_types_allowed = True

    session: Optional[Union[sa.orm.Session, sa.ext.asyncio.AsyncSession]] = ...
    initial_state: Optional[states.State] = ...
    proposed_state: Optional[states.State] = ...
    validated_state: Optional[states.State]
    rule_signature: List[str] = Field(default_factory=list)
    finalization_signature: List[str] = Field(default_factory=list)
    response_status: SetStateStatus = Field(default=SetStateStatus.ACCEPT)
    response_details: StateResponseDetails = Field(default_factory=StateAcceptDetails)

    @property
    def initial_state_type(self) -> Optional[states.StateType]:
        """The state type of `self.initial_state` if it exists."""

        return self.initial_state.type if self.initial_state else None

    @property
    def proposed_state_type(self) -> Optional[states.StateType]:
        """The state type of `self.proposed_state` if it exists."""

        return self.proposed_state.type if self.proposed_state else None

    @property
    def validated_state_type(self) -> Optional[states.StateType]:
        """The state type of `self.validated_state` if it exists."""
        return self.validated_state.type if self.validated_state else None

    def safe_copy(self):
        """
        Creates a mostly-mutation-safe copy for use in orchestration rules.

        Orchestration rules govern state transitions using information stored in
        an `OrchestrationContext`. However, mutating objects stored on the context
        directly can have unintended side-effects. To guard against this,
        `self.safe_copy` can be used to pass information to orchestration rules
        without risking mutation.

        Returns:
            A mutation-safe copy of the `OrchestrationContext`
        """

        safe_copy = self.copy()

        safe_copy.initial_state = (
            self.initial_state.copy() if self.initial_state else None
        )
        safe_copy.proposed_state = (
            self.proposed_state.copy() if self.proposed_state else None
        )
        safe_copy.validated_state = (
            self.validated_state.copy() if self.validated_state else None
        )
        return safe_copy

    def entry_context(self):
        """
        A convenience method that generates input parameters for orchestration rules.

        An `OrchestrationContext` defines a state transition that is managed by
        orchestration rules which can fire hooks before a transition has been committed
        to the database. These hooks have a consistent interface which can be generated
        with this method.
        """

        safe_context = self.safe_copy()
        return safe_context.initial_state, safe_context.proposed_state, safe_context

    def exit_context(self):
        """
        A convenience method that generates input parameters for orchestration rules.

        An `OrchestrationContext` defines a state transition that is managed by
        orchestration rules which can fire hooks after a transition has been committed
        to the database. These hooks have a consistent interface which can be generated
        with this method.
        """

        safe_context = self.safe_copy()
        return safe_context.initial_state, safe_context.validated_state, safe_context


class FlowOrchestrationContext(OrchestrationContext):
    """
    A container for a flow run state transition, governed by orchestration rules.

    When a flow- run attempts to change state, Orion has an opportunity
    to decide whether this transition can proceed. All the relevant information
    associated with the state transition is stored in an `OrchestrationContext`,
    which is subsequently governed by nested orchestration rules implemented using
    the `BaseOrchestrationRule` ABC.

    `FlowOrchestrationContext` introduces the concept of a state being `None` in the
    context of an intended state transition. An initial state can be `None` if a run
    is is attempting to set a state for the first time. The proposed state might be
    `None` if a rule governing the transition determines that no state change
    should occur at all and nothing is written to the database.

    Attributes:
        session: a SQLAlchemy database session
        run: the flow run attempting to change state
        initial_state: the initial state of the run
        proposed_state: the proposed state the run is transitioning into
        validated_state: a proposed state that has committed to the database
        rule_signature: a record of rules that have fired on entry into a
            managed context, currently only used for debugging purposes
        finalization_signature: a record of rules that have fired on exit from a
            managed context, currently only used for debugging purposes
        response_status: a SetStateStatus object used to build the API response
        response_details:a StateResponseDetails object use to build the API response

    Args:
        session: a SQLAlchemy database session
        run: the flow run attempting to change state
        initial_state: the initial state of a run
        proposed_state: the proposed state a run is transitioning into
    """

    run: orm.FlowRun = ...

    async def validate_proposed_state(self) -> orm.FlowRunState:
        """
        Validates a proposed state by committing it to the database.

        After the `FlowOrchestrationContext` is governed by orchestration rules, the
        proposed state can be validated: the proposed state is added to the current
        SQLAlchemy session and is flushed. `self.validated_state` set to the flushed
        state. The state on the run is set to the validated state as well. If the
        proposed state is `None` when this method is called, nothing happens.

        Returns:
            None
        """

        if self.proposed_state is not None:
            validated_orm_state = orm.FlowRunState(
                flow_run_id=self.run.id,
                **self.proposed_state.dict(shallow=True),
            )
            self.session.add(validated_orm_state)
            self.run.set_state(validated_orm_state)
        else:
            validated_orm_state = None
        validated_state = (
            validated_orm_state.as_state() if validated_orm_state else None
        )

        await self.session.flush()
        self.validated_state = validated_state

    def safe_copy(self):
        """
        Creates a mostly-mutation-safe copy for use in orchestration rules.

        Orchestration rules govern state transitions using information stored in
        an `OrchestrationContext`. However, mutating objects stored on the context
        directly can have unintended side-effects. To guard against this,
        `self.safe_copy` can be used to pass information to orchestration rules
        without risking mutation.

        Note:
            `self.run` is an ORM model, and even when copied is unsafe to mutate

        Returns:
            A mutation-safe copy of `FlowOrchestrationContext`
        """

        return super().safe_copy()

    @property
    def run_settings(self) -> Dict:
        """Run-level settings used to orchestrate the state transition."""

        return self.run.empirical_policy

    async def task_run(self):
        return None

    async def flow_run(self):
        return self.run


class TaskOrchestrationContext(OrchestrationContext):
    """
    A container for a task run state transition, governed by orchestration rules.

    When a task- run attempts to change state, Orion has an opportunity
    to decide whether this transition can proceed. All the relevant information
    associated with the state transition is stored in an `OrchestrationContext`,
    which is subsequently governed by nested orchestration rules implemented using
    the `BaseOrchestrationRule` ABC.

    `TaskOrchestrationContext` introduces the concept of a state being `None` in the
    context of an intended state transition. An initial state can be `None` if a run
    is is attempting to set a state for the first time. The proposed state might be
    `None` if a rule governing the transition determines that no state change
    should occur at all and nothing is written to the database.

    Attributes:
        session: a SQLAlchemy database session
        run: the task run attempting to change state
        initial_state: the initial state of the run
        proposed_state: the proposed state the run is transitioning into
        validated_state: a proposed state that has committed to the database
        rule_signature: a record of rules that have fired on entry into a
            managed context, currently only used for debugging purposes
        finalization_signature: a record of rules that have fired on exit from a
            managed context, currently only used for debugging purposes
        response_status: a SetStateStatus object used to build the API response
        response_details:a StateResponseDetails object use to build the API response

    Args:
        session: a SQLAlchemy database session
        run: the task run attempting to change state
        initial_state: the initial state of a run
        proposed_state: the proposed state a run is transitioning into
    """

    run: orm.TaskRun = ...

    async def validate_proposed_state(self) -> orm.TaskRunState:
        """
        Validates a proposed state by committing it to the database.

        After the `TaskOrchestrationContext` is governed by orchestration rules, the
        proposed state can be validated: the proposed state is added to the current
        SQLAlchemy session and is flushed. `self.validated_state` set to the flushed
        state. The state on the run is set to the validated state as well. If the
        proposed state is `None` when this method is called, nothing happens.

        Returns:
            None
        """

        if self.proposed_state is not None:
            validated_orm_state = orm.TaskRunState(
                task_run_id=self.run.id,
                **self.proposed_state.dict(shallow=True),
            )
            self.session.add(validated_orm_state)
            self.run.set_state(validated_orm_state)
        else:
            validated_orm_state = None
        validated_state = (
            validated_orm_state.as_state() if validated_orm_state else None
        )

        await self.session.flush()
        self.validated_state = validated_state

    def safe_copy(self):
        """
        Creates a mostly-mutation-safe copy for use in orchestration rules.

        Orchestration rules govern state transitions using information stored in
        an `OrchestrationContext`. However, mutating objects stored on the context
        directly can have unintended side-effects. To guard against this,
        `self.safe_copy` can be used to pass information to orchestration rules
        without risking mutation.

        Note:
            `self.run` is an ORM model, and even when copied is unsafe to mutate

        Returns:
            A mutation-safe copy of `TaskOrchestrationContext`
        """

        return super().safe_copy()

    @property
    def run_settings(self) -> Dict:
        """Run-level settings used to orchestrate the state transition."""

        return self.run.empirical_policy

    async def task_run(self):
        return self.run

    async def flow_run(self):
        return await flow_runs.read_flow_run(
            session=self.session,
            flow_run_id=self.run.flow_run_id,
        )


class BaseOrchestrationRule(contextlib.AbstractAsyncContextManager):
    """
    An abstract base class used to implement a discrete piece of orchestration logic.

    An `OrchestrationRule` is a stateful context manager that directly governs a state
    transition. Complex orchestration is achieved by nesting multiple rules.
    Each rule runs against an `OrchestrationContext` that contains the transition
    details; this context is then passed to subsequent rules. The context can be
    modified by hooks that fire before and after a new state is validated and committed
    to the database. These hooks will fire as long as the state transition is
    considered "valid" and govern a transition by either modifying the proposed state
    before it is validated or by producing a side-effect.

    A state transition occurs whenever a flow- or task- run changes state, prompting
    Orion to decide whether or not this transition can proceed. The current state of
    the run is referred to as the "initial state", and the state a run is
    attempting to transition into is the "proposed state". Together, the initial state
    transitioning into the proposed state is the intended transition that is governed
    by these orchestration rules. After using rules to enter a runtime context, the
    `OrchestrationContext` will contain a proposed state that has been governed by
    each rule, and at that point can validate the proposed state and commit it to
    the database. The validated state will be set on the context as
    `context.validated_state`, and rules will call the `self.after_transition` hook
    upon exiting the managed context.

    Examples:

        Create a rule

        >>> class BasicRule(BaseOrchestrationRule):
        >>>     # allowed initial state types
        >>>     FROM_STATES = [StateType.RUNNING]
        >>>     # allowed proposed state types
        >>>     TO_STATES = [StateType.COMPLETED, StateType.FAILED]
        >>>
        >>>     async def before_transition(initial_state, proposed_state, ctx):
        >>>         # side effects and proposed state mutation can happen here
        >>>
        >>>     async def after_transition(initial_state, validated_state, ctx):
        >>>         # operations on states that have been validated can happen here
        >>>
        >>>     async def cleanup(intitial_state, validated_state, ctx):
        >>>         # reverts side effects generated by `before_transition` if necessary

        Use a rule

        >>> intended_transition = (StateType.RUNNING, StateType.COMPLETED)
        >>> async with BasicRule(context, *intended_transition):
        >>>     # context.proposed_state has been governed by BasicRule

        Use multiple rules

        >>> rules = [BasicRule, BasicRule]
        >>> intended_transition = (StateType.RUNNING, StateType.COMPLETED)
        >>> async with contextlib.AsyncExitStack() as stack:
        >>>     for rule in rules:
        >>>         stack.enter_async_context(rule(context, *intended_transition))
        >>>
        >>>     # context.proposed_state has been governed by all rules

    Attributes:
        FROM_STATES: list of valid initial state types this rule governs
        TO_STATES: list of valid proposed state types this rule governs
        context: the orchestration context
        from_state_type: the state type a run is currently in
        to_state_type: the proposed state type a run is transitioning into

    Args:
        context: A `FlowOrchestrationContext` or `TaskOrchestrationContext` that is
            passed between rules
        from_state_type: The state type of the initial state of a run, if this
            state type is not contained in `FROM_STATES`, no hooks will fire
        to_state_type: The state type of the proposed state before orchestration, if
            this state type is not contained in `TO_STATES`, no hooks will fire
    """

    FROM_STATES: Iterable = []
    TO_STATES: Iterable = []

    def __init__(
        self,
        context: OrchestrationContext,
        from_state_type: Optional[states.StateType],
        to_state_type: Optional[states.StateType],
    ):
        self.context = context
        self.from_state_type = from_state_type
        self.to_state_type = to_state_type
        self._invalid_on_entry = None

    async def __aenter__(self) -> OrchestrationContext:
        """
        Enter an async runtime context governed by this rule.

        The `with` statement will bind a governed `OrchestrationContext` to the target
        specified by the `as` clause. If the transition proposed by the
        `OrchestrationContext` is considered invalid on entry, entering this context
        will do nothing. Otherwise, `self.before_transition` will fire.
        """

        if await self.invalid():
            pass
        else:
            entry_context = self.context.entry_context()
            await self.before_transition(*entry_context)
            self.context.rule_signature.append(str(self.__class__))
        return self.context

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """
        Exit the async runtime context governed by this rule.

        One of three outcomes can happen upon exiting this rule's context depending on
        the state of the rule. If the rule was found to be invalid on entry, nothing
        happens. If the rule was valid on entry and continues to be valid on exit,
        `self.after_transition` will fire. If the rule was valid on entry but invalid
        on exit, the rule will "fizzle" and `self.cleanup` will fire in order to revert
        any side-effects produced by `self.before_transition`.
        """

        exit_context = self.context.exit_context()
        if await self.invalid():
            pass
        elif await self.fizzled():
            await self.cleanup(*exit_context)
        else:
            await self.after_transition(*exit_context)
            self.context.finalization_signature.append(str(self.__class__))

    async def before_transition(
        self,
        initial_state: Optional[states.State],
        proposed_state: Optional[states.State],
        context: OrchestrationContext,
    ) -> None:
        """
        Implements a hook that can fire before a state is committed to the database.

        This hook may produce side-effects or mutate the proposed state of a
        transition using one of four methods: `self.reject_transition`,
        `self.delay_transition`, `self.abort_transition`, and `self.rename_state`.

        Note:
            As currently implemented, the `before_transition` hook is not
            perfectly isolated from mutating the transition. It is a standard instance
            method that has access to `self`, and therefore `self.context`. This should
            never be modified directly. Furthermore, `context.run` is an ORM model, and
            mutating the run can also cause unintended writes to the database.

        Args:
            initial_state: The initial state of a transtion
            proposed_state: The proposed state of a transition
            context: A safe copy of the `OrchestrationContext`, with the exception of
                `context.run`, mutating this context will have no effect on the broader
                orchestration environment.

        Returns:
            None
        """

        pass

    async def after_transition(
        self,
        initial_state: Optional[states.State],
        validated_state: Optional[states.State],
        context: OrchestrationContext,
    ) -> None:
        """
        Implements a hook that can fire after a state is committed to the database.

        Args:
            initial_state: The initial state of a transtion
            validated_state: The governed state that has been committed to the database
            context: A safe copy of the `OrchestrationContext`, with the exception of
                `context.run`, mutating this context will have no effect on the broader
                orchestration environment.

        Returns:
            None
        """
        pass

    async def cleanup(
        self,
        initial_state: Optional[states.State],
        validated_state: Optional[states.State],
        context: OrchestrationContext,
    ) -> None:
        """
        Implements a hook that can fire after a state is committed to the database.

        The intended use of this method is to revert side-effects produced by
        `self.before_transition` when the transition is found to be invalid on exit.
        This allows multiple rules to be gracefully run in sequence, without logic that
        keeps track of all other rules that might govern a transition.

        Args:
            initial_state: The initial state of a transtion
            validated_state: The governed state that has been committed to the database
            context: A safe copy of the `OrchestrationContext`, with the exception of
                `context.run`, mutating this context will have no effect on the broader
                orchestration environment.

        Returns:
            None
        """
        pass

    async def invalid(self) -> bool:
        """
        Determines if a rule is invalid.

        Invalid rules do nothing and no hooks fire upon entering or exiting a governed
        context. Rules are invalid if the transition states types are not contained in
        `self.FROM_STATES` and `self.TO_STATES`, or if the context is proposing
        a transition that differs from the transition the rule was instantiated with.

        Returns:
            True if the rules in invalid, False otherwise.
        """
        # invalid and fizzled states are mutually exclusive,
        # `_invalid_on_entry` holds this statefulness
        if self.from_state_type not in self.FROM_STATES:
            self._invalid_on_entry = True
        if self.to_state_type not in self.TO_STATES:
            self._invalid_on_entry = True

        if self._invalid_on_entry is None:
            self._invalid_on_entry = await self.invalid_transition()
        return self._invalid_on_entry

    async def fizzled(self) -> bool:
        """
        Determines if a rule is fizzled and side-effects need to be reverted.

        Rules are fizzled if the transitions were valid on entry (thus firing
        `self.before_transition`) but are invalid upon exiting the governed context,
        most likely caused by another rule mutating the transition.

        Returns:
            True if the rule is fizzled, False otherwise.
        """

        if self._invalid_on_entry:
            return False
        return await self.invalid_transition()

    async def invalid_transition(self) -> bool:
        """
        Determines if the transition proposed by the `OrchestrationContext` is invalid.

        If the `OrchestrationContext` is attempting to manage a transition with this
        rule that differs from the transition the rule was instantiated with, the
        transition is considered to be invalid. Depending on the context, this either
        renders the state of the rule "invalid" or "fizzled".

        Returns:
            True if the transition is invalid, False otherwise.
        """

        initial_state_type = self.context.initial_state_type
        proposed_state_type = self.context.proposed_state_type
        return (self.from_state_type != initial_state_type) or (
            self.to_state_type != proposed_state_type
        )

    async def reject_transition(self, state: states.State, reason: str):
        """
        Rejects a proposed transition before the transition is validated.

        This method will reject a proposed transition, mutating the proposed state to
        the provided `state`. A reason for rejecting the transition is also passed on
        to the `OrchestrationContext`. Rules that reject the transition will not fizzle,
        despite the proposed state type changing.

        Args:
            state: The new proposed state
            reason: The reason for rejecting the transition
        """

        # don't run if the transition is already validated
        if self.context.validated_state:
            raise RuntimeError("The transition is already validated")

        # a rule that mutates state should not fizzle itself
        self.to_state_type = state.type
        self.context.proposed_state = state
        self.context.response_status = SetStateStatus.REJECT
        self.context.response_details = StateRejectDetails(reason=reason)

    async def delay_transition(self, delay_seconds: int, reason: str):
        """
        Delays a proposed transition before the transition is validated.

        This method will delay a proposed transition, setting the proposed state to
        `None`, signaling to the `OrchestrationContext` that no state should be
        written to the database. The number of seconds a transition should be delayed is
        passed to the `OrchestrationContext`. A reason for delaying the transition is
        also provided.

        Args:
            delay_seconds: The number of seconds the transition should be delayed
            reason: The reason for delaying the transition
        """

        # don't run if the transition is already validated
        if self.context.validated_state:
            raise RuntimeError("The transition is already validated")

        # a rule that mutates state should not fizzle itself
        self.to_state_type = None
        self.context.proposed_state = None
        self.context.response_status = SetStateStatus.WAIT
        self.context.response_details = StateWaitDetails(
            delay_seconds=delay_seconds, reason=reason
        )

    async def abort_transition(self, reason: str):
        """
        Aborts a proposed transition before the transition is validated.

        This method will abort a proposed transition, expecting no further action to
        occur for this run. The proposed state is set to `None`, signaling to the
        `OrchestrationContext` that no state should be written to the database. A
        reason for aborting the transition is also provided.

        Args:
            reason: The reason for aborting the transition
        """

        # don't run if the transition is already validated
        if self.context.validated_state:
            raise RuntimeError("The transition is already validated")

        # a rule that mutates state should not fizzle itself
        self.to_state_type = None
        self.context.proposed_state = None
        self.context.response_status = SetStateStatus.ABORT
        self.context.response_details = StateAbortDetails(reason=reason)

    async def rename_state(self, state_name):
        """
        Sets the "name" attribute on a proposed state.

        The name of a state is an annotation intended to provide rich, human-readable
        context for how a run is progressing. This method only updates the name and not
        the canonical state TYPE, and will not fizzle or invalidate any other rules
        that might govern this state transition.
        """

        self.context.proposed_state.name = state_name


class BaseUniversalRule(contextlib.AbstractAsyncContextManager):
    """
    An abstract base class used to implement privileged bookkeeping logic.

    Note:
        In almost all cases, use the `BaseOrchestrationRule` base class instead.

    Beyond the orchestration rules implemented with the `BaseOrchestrationRule` ABC,
    Universal rules are not stateful, and fire their before- and after- transition hooks
    on every state transition unless the proposed state is `None`, indicating that no
    state should be written to the database. Because there are no guardrails in place
    to prevent directly mutating state or other parts of the orchestration context,
    universal rules should only be used with care.

    Attributes:
        FROM_STATES: list of valid initial state types this rule governs
        TO_STATES: list of valid proposed state types this rule governs
        context: the orchestration context

    Args:
        context: A `FlowOrchestrationContext` or `TaskOrchestrationContext` that is
            passed between rules
    """

    FROM_STATES: Iterable = ALL_ORCHESTRATION_STATES
    TO_STATES: Iterable = ALL_ORCHESTRATION_STATES

    def __init__(
        self,
        context: OrchestrationContext,
    ):
        self.context = context

    async def __aenter__(self):
        """
        Enter an async runtime context governed by this rule.

        The `with` statement will bind a governed `OrchestrationContext` to the target
        specified by the `as` clause. If the transition proposed by the
        `OrchestrationContext` has been nullified on entry and `context.proposed_state`
        is `None`, entering this context will do nothing. Otherwise
        `self.before_transition` will fire.
        """

        if not self.nullified_transition():
            await self.before_transition(self.context)
            self.context.rule_signature.append(str(self.__class__))
        return self.context

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """
        Exit the async runtime context governed by this rule.

        If the transition has been nullified upon exiting this rule's context, nothing
        happens. Otherwise, `self.after_transition` will fire on every non-null
        proposed_state.
        """

        if not self.nullified_transition():
            await self.after_transition(self.context)
            self.context.finalization_signature.append(str(self.__class__))

    async def before_transition(self, context) -> None:
        """
        Implements a hook that fires before a state is committed to the database.

        Args:
            context: the `OrchestrationContext` that contains transition details

        Returns:
            None
        """

        pass

    async def after_transition(self, context) -> None:
        """
        Implements a hook that can fire after a state is committed to the database.

        Args:
            context: the `OrchestrationContext` that contains transition details

        Returns:
            None
        """

        pass

    def nullified_transition(self) -> bool:
        """
        Determines if the transition has been nullified.

        Transitions are nullified if the proposed state is `None`, indicating that
        nothing should be written to the database.

        Returns:
            True if the transition is nullified, False otherwise.
        """

        return self.context.proposed_state is None
