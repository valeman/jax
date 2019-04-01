# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import Counter
import itertools as it
import operator as op

import six
from six.moves import reduce

from .. import core
from ..core import Trace, Tracer, new_master, pack, AbstractTuple, JaxTuple
from ..util import unzip2, partial, safe_map, safe_zip, prod
from ..linear_util import transformation, transformation_with_aux, wrap_init
from ..ad_util import add_jaxvals_p
from ..tree_util import tree_map, tree_multimap
from . import partial_eval as pe

zip = safe_zip
map = safe_map

if six.PY2:
  def heap_merge(a, b):
    return sorted(a + b)
else:
  import heapq
  heap_merge = heapq.merge


@transformation
def polysimp(vals):
  newvar = pe.gensym('')
  symbols = [newvar() for _ in vals]
  new_poly = lambda symbol: Poly({Mon(symbol): one})
  with new_master(PolySimpTrace) as master:
    trace = PolySimpTrace(master, core.cur_sublevel())
    in_tracers = [PolySimpTracer(trace, new_poly(x), core.get_aval(v))
                  for x, v in zip(symbols, vals)]
    out = yield in_tracers
    out_tracer = trace.full_raise(out)
    out_poly, out_aval = out_tracer.poly, out_tracer.aval
    del master, out_tracer
  env = dict(zip(symbols, vals))
  yield eval_polynomial(env, out_poly, out_aval)


class UnitCoeff(object):
  def __repr__(self): return '1'
one = UnitCoeff()

class ZeroCoeff(object):
  def __repr__(self): return '0'
zero = ZeroCoeff()

def mul_coeffs(mul, a, b):
  if a is zero or b is zero:
    return zero
  elif a is one:
    return b
  elif b is one:
    return a
  else:
    return mul(a, b)

def add_coeffs(a, b):
  if a is zero:
    return b
  elif b is zero:
    return a
  elif a is one or b is one:
    return (1 if a is one else a) + (1 if b is one else b)
  else:
    return a + b

class Mon(tuple):
  def __new__(cls, *elts):
    return tuple.__new__(cls, sorted(elts))

class Poly(dict): pass

def mul_polynomials(mul, p1, p2):
  new_terms = {}
  for i1, i2 in it.product(p1, p2):
    mon = Mon(*heap_merge(i1, i2))
    coeff = mul_coeffs(mul, p1[i1], p2[i2])
    new_terms[mon] = add_coeffs(new_terms.get(mon, zero), coeff)
  return new_terms

def add_polynomials(p1, p2):
  indets1, indets2 = set(p1), set(p2)
  new_terms = {i: add_coeffs(p1[i], p2[i]) for i in set.intersection(indets1, indets2)}
  for i in set.difference(indets1, indets2):
    new_terms[i] = p1[i]
  for i in set.difference(indets2, indets1):
    new_terms[i] = p2[i]
  return new_terms

def apply_linear(linear_fun, p):
  return {i: linear_fun(coeff) for i, coeff in p.items()}

def eval_polynomial(env, p, aval):
  if len(env) > 1:
    raise NotImplementedError  # TODO(mattjj): see np.polyval3
  x, = env.values()
  coeffs = [zero] * (1 + max(len(mon) for mon in p))
  for mon, coeff in p.items():
    coeffs[len(mon)] = coeff
  out = eval_univariate_polynomial(x, coeffs)
  return instantiate_symbolic(aval, out)

def eval_univariate_polynomial(x, coeffs):
  out = zero
  for coeff in reversed(coeffs):
    out = add_coeffs(mul_coeffs(op.mul, out, x), coeff)
  return out

def instantiate_symbolic(aval, x):
  if x is zero or x is one:
    raise NotImplementedError  # TODO
  else:
    return x


class PolyTuple(tuple): pass

def const_poly(val):
  return Poly({Mon(): val})

class PolySimpTracer(Tracer):
  __slots__ = ['poly', 'aval']

  def __init__(self, trace, poly, aval):
    self.trace = trace
    self.poly = poly
    self.aval = aval

  def unpack(self):
    assert type(self.poly) is PolyTuple  # could generalize, but assume promise
    return map(partial(PolySimpTracer, self.trace), self.poly, self.aval)

  def full_lower(self):
    if len(self.poly) == 1:
      (mon, coeff), = self.poly.items()
      if len(mon) == 0:
        return core.full_lower(coeff)
    return self

class PolySimpTrace(Trace):
  def pure(self, val):
    return PolySimpTracer(self, const_poly(val), core.get_aval(val))

  def lift(self, val):
    return PolySimpTracer(self, const_poly(val), core.get_aval(val))

  def sublift(self, val):
    return PolySimpTracer(self, val.poly, val.aval)

  def process_primitive(self, primitive, tracers, params):
    polys_in, avals_in = unzip2((t.poly, t.aval) for t in tracers)
    aval_out = primitive.abstract_eval(*avals_in, **params)
    if primitive in addition_primitives:  # e.g. add
      p1, p2 = polys_in
      p_out = add_polynomials(p1, p2)
      return PolySimpTracer(self, p_out, aval_out)
    elif primitive in multiplication_primitives:  # e.g. mul
      p1, p2 = polys_in
      p_out = mul_polynomials(primitive.bind, p1, p2)
      return PolySimpTracer(self, p_out, aval_out)
    elif primitive in linear_primitives:  # e.g. broadcast, reduce_sum, neg
      p, = polys_in
      p_out = apply_linear(primitive.bind, p)
      return PolySimpTracer(self, p_out, aval_out)
    else:
      # could eval the polynomial in env and call bind, but we assume a promise
      assert False

  def process_call(self, call_primitive, f, tracers, params):
    raise NotImplementedError

  def post_process_call(self, _, out_tracer):
    raise NotImplementedError

  def pack(self, tracers):
    polys, avals = unzip2((t.poly, t.aval) for t in tracers)
    return PolySimpTracer(self, PolyTuple(polys), avals)


addition_primitives = set()
multiplication_primitives = set()
linear_primitives = set()