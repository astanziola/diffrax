import functools as ft
from dataclasses import field
from typing import Callable, Optional, Tuple

import equinox as eqx
import jax
import jax.lax as lax
import jax.numpy as jnp

from ..custom_types import Array, PyTree, Scalar
from ..misc import nextafter, nextbefore, ravel_pytree, unvmap
from ..solution import RESULTS
from ..solver import AbstractSolver
from .base import AbstractStepSizeController


def _rms_norm(x: PyTree) -> Scalar:
    x, _ = ravel_pytree(x)
    if x.size == 0:
        return 0
    sqnorm = jnp.mean(x ** 2)
    cond = sqnorm == 0
    # Double-where trick to avoid NaN gradients.
    # See JAX issues #5039 and #1052.
    _sqnorm = jnp.where(cond, 1.0, sqnorm)
    return jnp.where(cond, 0.0, jnp.sqrt(_sqnorm))


# Empirical initial step selection algorithm from:
# E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential Equations I:
# Nonstiff Problems", Sec. II.4, 2nd edition.
@ft.partial(eqx.filter_jit, filter_spec=eqx.is_array)
def _select_initial_step(
    t0: Scalar,
    y0: Array["state"],  # noqa: F821
    args: PyTree,
    solver: AbstractSolver,
    rtol: Scalar,
    atol: Scalar,
    unravel_y: jax.tree_util.Partial,
    norm: Callable[[Array], Scalar],
):
    f0 = solver.func_for_init(t0, y0, args)
    scale = atol + jnp.abs(y0) * rtol
    d0 = norm(unravel_y(y0 / scale))
    d1 = norm(unravel_y(f0 / scale))

    _cond = (d0 < 1e-5) | (d1 < 1e-5)
    _d1 = jnp.where(_cond, 1, d1)
    h0 = jnp.where(_cond, 1e-6, 0.01 * (d0 / _d1))

    t1 = t0 + h0
    y1 = y0 + h0 * f0
    f1 = solver.func_for_init(t1, y1, args)
    d2 = norm(unravel_y((f1 - f0) / scale)) / h0

    h1 = jnp.where(
        (d1 <= 1e-15) | (d2 <= 1e-15),
        jnp.maximum(1e-6, h0 * 1e-3),
        (0.01 * jnp.maximum(d1, d2)) ** (1 / solver.order),
    )

    return jnp.minimum(100 * h0, h1)


def _scale_error_estimate(
    y_error: Array["state"],  # noqa: F821
    y0: Array["state"],  # noqa: F821
    y1_candidate: Array["state"],  # noqa: F821
    unravel_y: callable,
    rtol: Scalar,
    atol: Scalar,
    norm: Callable[[Array], Scalar],
) -> Scalar:
    scale = y_error / (atol + jnp.maximum(y0, y1_candidate) * rtol)
    scale = unravel_y(scale)
    return norm(scale)


_do_not_set_at_init = object()  # Is set during wrap instead


_ControllerState = Tuple[Array[(), bool], Array[(), bool]]


# https://diffeq.sciml.ai/stable/extras/timestepping/
# are good notes on different step size control algorithms.
class IController(AbstractStepSizeController):
    # Default tolerances taken from scipy.integrate.solve_ivp
    rtol: Scalar = 1e-3
    atol: Scalar = 1e-6
    safety: Scalar = 0.9
    ifactor: Scalar = 10.0
    dfactor: Scalar = 0.2
    norm: Callable = _rms_norm
    dtmin: Optional[Scalar] = None
    dtmax: Optional[Scalar] = None
    force_dtmin: bool = True
    unvmap_dt: bool = False
    step_ts: Optional[Array["steps"]] = None  # noqa: F821
    jump_ts: Optional[Array["steps"]] = None  # noqa: F821
    unravel_y: callable = field(repr=False, default=_do_not_set_at_init)
    direction: Scalar = field(repr=False, default=_do_not_set_at_init)

    def wrap(self, unravel_y: callable, direction: Scalar):
        return type(self)(
            rtol=self.rtol,
            atol=self.atol,
            safety=self.safety,
            ifactor=self.ifactor,
            dfactor=self.dfactor,
            norm=self.norm,
            dtmin=self.dtmin,
            dtmax=self.dtmax,
            force_dtmin=self.force_dtmin,
            unvmap_dt=self.unvmap_dt,
            step_ts=None if self.step_ts is None else self.step_ts * direction,
            jump_ts=None if self.jump_ts is None else self.jump_ts * direction,
            unravel_y=unravel_y,
            direction=direction,
        )

    def init(
        self,
        t0: Scalar,
        y0: Array["state"],  # noqa: F821
        dt0: Optional[Scalar],
        args: PyTree,
        solver: AbstractSolver,
    ) -> Tuple[Scalar, _ControllerState]:
        if dt0 is None:
            dt0 = _select_initial_step(
                t0,
                y0,
                args,
                solver,
                self.rtol,
                self.atol,
                self.unravel_y,
                self.norm,
            )
            # So this stop_gradient is a choice I'm not 100% convinced by.
            #
            # (Note that we also do something similar lower down, by stopping the
            # gradient through the multiplicative factor updating the step size, and
            # the following discussion is in reference to them both, collectively.)
            #
            # - This dramatically speeds up gradient computations. e.g. at time of
            #   writing, the neural ODE example goes from 0.3 seconds/iteration down to
            #   0.1 seconds/iteration.
            # - On some problems this actually improves training behaviour. e.g. at
            #   time of writing, the neural CDE example fails to train if these
            #   stop_gradients are removed.
            # - I've never observed this hurting training behaviour.
            # - Other libraries (notably torchdiffeq) do this by default without
            #   remark. The idea is that "morally speaking" the time discretisation
            #   shouldn't really matter, it's just some minor implementation detail of
            #   the ODE solve. (e.g. part of the folklore of neural ODEs is that "you
            #   don't need to backpropagate through rejected steps".)
            #
            # However:
            # - This feels morally wrong from the point of view of differentiable
            #   programming.
            # - That "you don't need to backpropagate through rejected steps" feels a
            #   bit questionable. They _are_ part of the computational graph and do
            #   have a subtle effect on the choice of step size, and the choice of step
            #   step size does have a not-so-subtle effect on the solution computed.
            # - This does mean that certain esoteric optimisation criteria, like
            #   optimising wrt parameters of the adaptive step size controller itself,
            #   might fail?
            # - It's entirely opaque why these stop_gradients should either improve the
            #   speed of backpropagation, or why they should improve training behavior.
            #
            # I would welcome your thoughts, dear reader, if you have any insight!
            dt0 = lax.stop_gradient(dt0)
        if self.unvmap_dt:
            dt0 = jnp.min(unvmap(dt0))
        if self.dtmax is not None:
            dt0 = jnp.minimum(dt0, self.dtmax)
        if self.dtmin is None:
            at_dtmin = jnp.array(False)
        else:
            at_dtmin = dt0 <= self.dtmin
            dt0 = jnp.maximum(dt0, self.dtmin)

        t1 = self._clip_step_ts(t0, t0 + dt0)
        t1, jump_next_step = self._clip_jump_ts(t0, t1)

        return t1, (jump_next_step, at_dtmin)

    def adapt_step_size(
        self,
        t0: Scalar,
        t1: Scalar,
        y0: Array["state"],  # noqa: F821
        y1_candidate: Array["state"],  # noqa: F821
        args: PyTree,
        y_error: Optional[Array["state"]],  # noqa: F821
        solver_order: int,
        controller_state: _ControllerState,
    ) -> Tuple[Array[(), bool], Scalar, Scalar, Array[(), bool], _ControllerState, int]:
        del args
        if y_error is None:
            raise ValueError(
                "Cannot use adaptive step sizes with a solver that does not provide "
                "error estimates."
            )
        prev_dt = t1 - t0
        made_jump, at_dtmin = controller_state

        #
        # Figure out how things went on the last step: error, and whether to
        # accept/reject it.
        #

        scaled_error = _scale_error_estimate(
            y_error, y0, y1_candidate, self.unravel_y, self.rtol, self.atol, self.norm
        )
        keep_step = scaled_error < 1
        if self.dtmin is not None:
            keep_step = keep_step | at_dtmin
        if self.unvmap_dt:
            keep_step = jnp.all(unvmap(keep_step))

        #
        # Adjust next step size
        #

        # Double-where trick to avoid NaN gradients.
        # See JAX issues #5039 and #1052.
        #
        # (Although we've actually since added a stop_gradient afterwards, this is kept
        # for completeness, e.g. just in case we ever remove the stop_gradient.)
        cond = scaled_error == 0
        _scaled_error = jnp.where(cond, 1.0, scaled_error)
        factor = lax.cond(
            cond,
            lambda _: self.ifactor,
            self._scale_factor,
            (solver_order, keep_step, _scaled_error),
        )
        factor = lax.stop_gradient(factor)  # See note in init above.
        if self.unvmap_dt:
            factor = jnp.min(unvmap(factor))
        dt = prev_dt * factor

        #
        # Clip next step size based on dtmin/dtmax
        #

        result = jnp.full_like(t0, RESULTS.successful)
        if self.dtmax is not None:
            dt = jnp.minimum(dt, self.dtmax)
        if self.dtmin is None:
            at_dtmin = jnp.array(False)
        else:
            if not self.force_dtmin:
                result = jnp.where(dt < self.dtmin, RESULTS.dt_min_reached, result)
            at_dtmin = dt <= self.dtmin
            dt = jnp.maximum(dt, self.dtmin)

        #
        # Clip next step size based on step_ts/jump_ts
        #

        if jnp.issubdtype(t1.dtype, jnp.inexact):
            _t1 = jnp.where(made_jump, nextafter(t1), t1)
        else:
            _t1 = t1
        next_t0 = jnp.where(keep_step, _t1, t0)
        next_t1 = self._clip_step_ts(next_t0, next_t0 + dt)
        next_t1, next_made_jump = self._clip_jump_ts(next_t0, next_t1)

        controller_state = (next_made_jump, at_dtmin)
        return keep_step, next_t0, next_t1, made_jump, controller_state, result

    def _scale_factor(self, operand):
        order, keep_step, scaled_error = operand
        dfactor = jnp.where(keep_step, 1, self.dfactor)
        exponent = 1 / order
        return jnp.clip(
            self.safety / scaled_error ** exponent, a_min=dfactor, a_max=self.ifactor
        )

    def _clip_step_ts(self, t0: Scalar, t1: Scalar) -> Scalar:
        if self.step_ts is None:
            return t1
        if self.unvmap_dt:
            # Need to think about how to implement this correctly.
            #
            # If t is batched, and has the same time at different batch elements,
            # then we should get the same number (at floating point precision) in all
            # batch elements. I think this precludes most arithmetic operations for
            # accomplishing this.
            raise NotImplementedError(
                "The interaction between step_ts and unvmap_dt has not been "
                "implemented. Set unvmap_dt=False instead."
            )

        # TODO: it should be possible to switch this O(nlogn) for just O(n) by keeping
        # track of where we were last, and using that as a hint for the next search.
        t0_index = jnp.searchsorted(self.step_ts, t0)
        t1_index = jnp.searchsorted(self.step_ts, t1)
        # This minimum may or may not actually be necessary. The left branch is taken
        # iff t0_index < t1_index <= len(self.step_ts), so all valid t0_index s must
        # already satisfy the minimum.
        # However, that branch is actually executed unconditionally and then where'd,
        # so we clamp it just to be sure we're not hitting undefined behaviour.
        t1 = jnp.where(
            t0_index < t1_index,
            self.step_ts[jnp.minimum(t0_index, len(self.step_ts) - 1)],
            t1,
        )
        return t1

    def _clip_jump_ts(self, t0: Scalar, t1: Scalar) -> Tuple[Scalar, Array[(), bool]]:
        if self.jump_ts is None:
            return t1, jnp.full_like(t1, fill_value=False, dtype=bool)
        if self.unvmap_dt:
            raise NotImplementedError(
                "The interaction between jump_ts and unvmap_dt has not been "
                "implemented. Set unvmap_dt=False instead."
            )
        if self.jump_ts is not None and not jnp.issubdtype(
            self.jump_ts.dtype, jnp.inexact
        ):
            raise ValueError(
                f"jump_ts must be floating point, not {self.jump_ts.dtype}"
            )
        if not jnp.issubdtype(t1.dtype, jnp.inexact):
            raise ValueError(
                "t0, t1, dt0 must be floating point when specifying jump_t. Got "
                f"{t1.dtype}."
            )
        t0_index = jnp.searchsorted(self.step_ts, t0)
        t1_index = jnp.searchsorted(self.step_ts, t1)
        cond = t0_index < t1_index
        t1 = jnp.where(
            cond,
            nextbefore(self.jump_ts[jnp.minimum(t0_index, len(self.step_ts) - 1)]),
            t1,
        )
        next_made_jump = jnp.where(cond, True, False)
        return t1, next_made_jump
