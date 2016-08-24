#
# This file is part of pySMT.
#
#   Copyright 2014 Andrea Micheli and Marco Gario
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
import re

from warnings import warn
from fractions import Fraction
from six.moves import xrange

from pysmt.exceptions import SolverAPINotFound

try:
    import drealpy
except ImportError:
    raise SolverAPINotFound

from pysmt.logics import QF_NRA
from pysmt.oracles import get_logic

import pysmt.operators as op
from pysmt import typing as types
from pysmt.solvers.solver import IncrementalTrackingSolver, Model, Converter
from pysmt.solvers.smtlib import SmtLibBasicSolver, SmtLibIgnoreMixin
from pysmt.solvers.eager import EagerModel
from pysmt.walkers import DagWalker
from pysmt.exceptions import (SolverReturnedUnknownResultError,
                              SolverNotConfiguredForUnsatCoresError,
                              InternalSolverError,
                              DeltaSATError)
from pysmt.decorators import clear_pending_pop, catch_conversion_error
#from pysmt.solvers.interpolation import Interpolator # MG: Is this available in dreal?
from pysmt.walkers.identitydag import IdentityDagWalker


class DRealContext(object):
    """A wrapper for the dReal Context object.

    Objects within pySMT should only reference the context
    through this object. When calling a function from the underlying
    wrapper, the inner instance of dreal_env needs to be used.
    This is done using the __call__ method: e.g.,
       ctx = DRealContext()
       drealpy.function(ctx())
    """
    __slots__ = ['ctx']

    def __init__(self, logic=None):
        if logic is None:
            logic = drealpy.qf_nra
        self.ctx = drealpy.dreal_mk_context(logic)

    def __del__(self):
        drealpy.dreal_del_context(self.ctx)

    def __call__(self):
        return self.ctx

class DRealSolver(IncrementalTrackingSolver, SmtLibBasicSolver, SmtLibIgnoreMixin):

    LOGICS = [QF_NRA]

    def __init__(self, environment, logic, **options):
        # TODO: Options should be custom and include delta value
        # for delta-sat
        IncrementalTrackingSolver.__init__(self, environment=environment,
                                           logic=logic, **options)

        # TODO: Check logic
        self.dreal_ctx = DRealContext(drealpy.qf_nra)
        self._precision = 10**(-3)
        drealpy.dreal_set_precision(self.dreal_ctx(), self._precision)
        self.mgr = self.environment.formula_manager
        self.converter = DRealConverter(environment, self.dreal_ctx)

    @clear_pending_pop
    def _reset_assertions(self):
        drealpy.dreal_reset(self.dreal_ctx())

    @clear_pending_pop
    def _add_assertion(self, formula, named=None):
        self._assert_is_boolean(formula)
        term = self.converter.convert(formula)
        drealpy.dreal_assert(self.dreal_ctx(), term)
        return formula

    @clear_pending_pop
    def _solve(self, assumptions=None):
        res = None

        if assumptions is not None:
            raise NotImplementedError("dReal method check_assump is buggy!")
            bool_ass = []
            other_ass = []
            for x in assumptions:
                if x.is_literal():
                    bool_ass.append(self.converter.convert(x))
                else:
                    other_ass.append(x)

            if len(other_ass) > 0:
                self.push()
                self.add_assertion(self.mgr.And(other_ass))
                self.pending_pop = True

            if len(bool_ass) > 0:
                # TODO: Refactor this
                if len(bool_ass) == 1:
                    ass_ = bool_ass[0]
                else:
                    ass_ = drealpy.dreal_mk_and_2(self.dreal_ctx(),
                                                bool_ass[0], bool_ass[1])
                    for a in bool_ass[2:]:
                        ass_ = drealpy.dreal_mk_and_2(self.dreal_ctx(),
                                                    ass_, a)
                res = drealpy.dreal_check_assump(self.dreal_ctx(), ass_)
            else:
                res = drealpy.dreal_check(self.dreal_ctx())
        else:
            res = drealpy.dreal_check(self.dreal_ctx())

        assert res in [drealpy.l_undef, drealpy.l_true, drealpy.l_false]
        # Convert res into a valid value for pySMT
        if res == drealpy.l_undef:
            raise SolverReturnedUnknownResultError
        elif res == drealpy.l_false:
            return False
        else:
            # l_true means delta-SAT
            formula = self.mgr.And(self._assertion_stack)
            model = self.get_model()
            if not model.get_py_value(formula):
                raise DeltaSATError("Delta-SAT with precision %s" % str(self._precision))
            return True

    @clear_pending_pop
    def _push(self, levels=1):
        for _ in xrange(levels):
            drealpy.dreal_push(self.dreal_ctx())

    @clear_pending_pop
    def _pop(self, levels=1):
        for _ in xrange(levels):
            drealpy.dreal_pop(self.dreal_ctx())

    def print_model(self, name_filter=None):
        if name_filter is not None:
            raise NotImplementedError
        model = self.get_model()
        print(model)

    def get_value(self, item):
        self._assert_no_function_type(item)
        titem = self.converter.convert(item)
        ty_ = self.environment.stc.get_type(item)
        if ty_.is_bool_type():
            res = drealpy.dreal_get_bool(self.dreal_ctx(), titem)
            if res == drealpy.l_false:
                return self.mgr.FALSE()
            else:
                # This covers both l_true and l_undef
                return self.mgr.TRUE()
            raise TypeError("Cannot assign value %d to bool var" % res)
        else:
            assert ty_.is_real_type(), ty_
            lb = drealpy.dreal_get_lb(self.dreal_ctx(), titem)
            ub = drealpy.dreal_get_ub(self.dreal_ctx(), titem)
            if lb == ub:
                return self.mgr.Real(lb)
            else:
                # Print a warning here
                if ub < float("inf"):
                    v = ub
                elif lb > -float("inf"):
                    v = lb
                else:
                    v = 0
                return self.mgr.Real(v)

    def get_model(self):
        assignment = {}
        for s in self.environment.formula_manager.get_all_symbols():
            if s.is_term():
                if s.symbol_type().is_bv_type(): continue
                if s.symbol_type().is_array_type(): continue
                if s.symbol_type().is_int_type(): continue
                v = self.get_value(s)
                assignment[s] = v
        return EagerModel(assignment=assignment, environment=self.environment)

    def _exit(self):
        del self.dreal_ctx


class DRealConverter(Converter, DagWalker):

    def __init__(self, environment, dreal_ctx):
        DagWalker.__init__(self, environment)

        self.dreal_ctx = dreal_ctx
        self.mgr = environment.formula_manager
        self._get_type = environment.stc.get_type

        self.back_memoization = {}
        return

    def back(self, expr):
        return self._walk_back(expr, self.mgr)

    @catch_conversion_error
    def convert(self, formula):
        """Convert a PySMT formula into a MathSat Term.

        This function might throw a InternalSolverError exception if
        an error during conversion occurs.
        """
        res = self.walk(formula)
        return res

    def walk_and(self, formula, args, **kwargs):
        res = drealpy.dreal_mk_and_2(self.dreal_ctx(), args[0], args[1])
        for x in args[2:]:
            res = drealpy.dreal_mk_and_2(self.dreal_ctx(), res, x)
        # res = drealpy.dreal_mk_and(self.dreal_ctx(), args, len(args))
        return res

    def walk_or(self, formula, args, **kwargs):
        res = drealpy.dreal_mk_or_2(self.dreal_ctx(), args[0], args[1])
        for x in args[2:]:
            res = drealpy.dreal_mk_or_2(self.dreal_ctx(), res, x)
        # res = drealpy.dreal_mk_or(self.dreal_ctx(), args, len(args))
        return res

    def walk_not(self, formula, args, **kwargs):
        return drealpy.dreal_mk_not(self.dreal_ctx(), args[0])

    def walk_symbol(self, formula, **kwargs):
        ty = formula.symbol_type()
        if ty.is_bool_type():
            return drealpy.dreal_mk_bool_var(self.dreal_ctx(),
                                           formula.symbol_name())
        elif ty.is_real_type():
            return drealpy.dreal_mk_unbounded_real_var(self.dreal_ctx(),
                                                       formula.symbol_name())
        elif ty.is_int_type():
            return drealpy.dreal_mk_unbounded_int_var(self.dreal_ctx(),
                                                      formula.symbol_name())
        else:
            raise NotImplementedError(ty)

    def walk_le(self, formula, args, **kwargs):
        return drealpy.dreal_mk_leq(self.dreal_ctx(), args[0], args[1])

    def walk_lt(self, formula, args, **kwargs):
        return drealpy.dreal_mk_lt(self.dreal_ctx(), args[0], args[1])

    def walk_ite(self, formula, args, **kwargs):
        i = args[0]
        t = args[1]
        e = args[2]
        return drealpy.dreal_mk_ite(self.dreal_ctx(), i, t, e)

    def walk_real_constant(self, formula, **kwargs):
        assert type(formula.constant_value()) == Fraction
        frac = formula.constant_value()
        n,d = frac.numerator, frac.denominator
        rep = str(n) + "/" + str(d)
        return drealpy.dreal_mk_num_from_string(self.dreal_ctx(), rep)

    def walk_int_constant(self, formula, **kwargs):
        assert type(formula.constant_value()) == int or \
            type(formula.constant_value()) == long
        rep = str(formula.constant_value())
        return drealpy.dreal_mk_num_from_string(self.dreal_ctx(), rep)

    def walk_bool_constant(self, formula, **kwargs):
        if formula.constant_value():
            return drealpy.dreal_mk_true(self.dreal_ctx())
        else:
            return drealpy.dreal_mk_false(self.dreal_ctx())

    def walk_plus(self, formula, args, **kwargs):
        res = drealpy.dreal_mk_plus_2(self.dreal_ctx(), args[0], args[1])
        for x in args[2:]:
            res = drealpy.dreal_mk_plus_2(self.dreal_ctx(), res, x)
        return res

    def walk_minus(self, formula, args, **kwargs):
        return drealpy.dreal_mk_minus(self.dreal_ctx(), args[0], args[1])

    def walk_equals(self, formula, args, **kwargs):
        return drealpy.dreal_mk_eq(self.dreal_ctx(), args[0], args[1])

    def walk_iff(self, formula, args, **kwargs):
        return drealpy.dreal_mk_eq(self.dreal_ctx(), args[0], args[1])

    def walk_implies(self, formula, args, **kwargs):
        neg = self.walk_not(self.mgr.Not(formula.arg(0)), [args[0]])
        return drealpy.dreal_mk_or_2(self.dreal_ctx(), neg, args[1])

    def walk_times(self, formula, args, **kwargs):
        return drealpy.dreal_mk_times_2(self.dreal_ctx(), args[0], args[1])

    def walk_div(self, formula, args, **kwargs):
        return drealpy.dreal_mk_div(self.dreal_ctx(), args[0], args[1])

    def walk_pow(self, formula, args, **kwargs):
        return drealpy.dreal_mk_pow(self.dreal_ctx(), args[0], args[1])

# EOC DRealConverter
