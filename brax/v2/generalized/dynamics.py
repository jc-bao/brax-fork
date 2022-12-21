# Copyright 2022 The Brax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint:disable=g-multiple-import
"""Functions for smooth forward and inverse dynamics."""
from typing import Tuple

from brax.v2 import math
from brax.v2 import scan
from brax.v2.base import Inertia, Motion, System, Transform
import jax
from jax import numpy as jp


def transform_com(
    sys: System, q: jp.ndarray, qd: jp.ndarray, x: jp.ndarray
) -> Tuple[jp.ndarray, Inertia, Motion, Motion, Motion]:
  """Transforms inertia, dof, and link velocity into center of mass frame.

  Args:
    sys: a brax system
    q: joint position vector
    qd: joint velocity vector
    x: link position in world frame

  Returns:
    com: center of mass position
    cinr: inertia in com frame
    cd: body velocities in com frame
    cdof: dofs in com frame
    cdofd: cdof velocity
  """
  # TODO: support multiple kinematic trees in the same system
  xi = x.vmap().do(sys.link.inertia.transform)
  mass = sys.link.inertia.mass
  com = jp.sum(jax.vmap(jp.multiply)(mass, xi.pos), axis=0) / jp.sum(mass)
  cinr = xi.replace(pos=xi.pos - com).vmap().do(sys.link.inertia)

  # motion dofs to global frame centered at subtree-CoM
  x_pad = x.concatenate(Transform.zero(shape=(1,)))
  pidx = jp.array(
      [
          i if t == 'f' else p
          for i, (t, p) in enumerate(zip(sys.link_types, sys.link_parents))
      ]
  )
  j = x_pad.take(pidx).vmap().do(sys.link.transform).vmap().do(sys.link.joint)

  # propagate motion through stacked joints
  def cdof_fn(typ, q, motion):
    if typ == 'f':
      return motion

    # create joint transforms and motions:
    # - rotation/velocity about axis for revolute (motion.ang)
    # - translation/velocity along axis for prismatic (motion.vel)
    rot_fn = lambda ang, q: math.normalize(math.quat_rot_axis(ang, q))[0]
    j = Transform.create(
        rot=jax.vmap(rot_fn)(motion.ang, q),
        pos=jax.vmap(jp.multiply)(motion.vel, q),
    )
    jd = motion

    # then group them by link, so each has num_dofs joints
    num_links, num_dofs = motion.ang.shape[0] // int(typ), int(typ)
    s = (num_links, num_dofs, -1)
    j_stack, jd_stack = j.reshape(s), jd.reshape(s)

    # accumulate jds as successive j transforms around joint
    j, jds = Transform.zero(shape=(num_links,)), []
    for i in range(0, num_dofs):
      jds.append(j.inv().vmap().do(jd_stack.take(i, axis=1)))
      j = j.vmap().do(j_stack.take(i, axis=1))

    # interleave jds back together to match joint stack order
    motion = jax.tree_map(lambda *x: jp.column_stack(x), *jds).reshape((-1, 3))

    return motion

  cdof = scan.link_types(sys, cdof_fn, 'qd', 'd', q, sys.dof.motion)
  ang = jax.vmap(math.rotate)(cdof.ang, j.take(sys.dof_link()).rot)
  cdof = cdof.replace(ang=ang)
  cdof = Transform.create(pos=com - j.pos).take(sys.dof_link()).vmap().do(cdof)
  cdof_qd = jax.vmap(lambda x, y: x * y)(cdof, qd)

  # forward scan down tree: accumulate link center of mass velocity
  def cd_fn(cd_parent, cdof_qd, dof_idx):
    if cd_parent is None:
      cd_parent = Motion.zero(shape=(1,))

    # cd = cd[parent] + map-sum(cdof * qd)
    cd = cd_parent.index_sum(dof_idx, cdof_qd)

    return cd

  cd = scan.tree(sys, cd_fn, 'dd', cdof_qd, sys.dof_link(depth=True))

  # propagate cd through stacked joints to calculate cdofd
  def cdofd_fn(typ, cd, cdof, cdof_qd):
    if typ == 'f':
      cdof_qd = cdof_qd.reshape((-1, 6, 3))
      cd = jax.tree_map(lambda x: jp.sum(x[:, 0:3], axis=1), cdof_qd)
      cdofd = cd.vmap().vmap(in_axes=(None, 0)).cross(cdof.reshape((-1, 6, 3)))
      cdofd = jax.tree_map(lambda x: x.at[:, 0:3].set(jp.zeros(3)), cdofd)
      return cdofd.reshape((-1, 3))

    # group cdof_qd by link, so each has num_dofs joints
    num_dofs = int(typ)
    cdof_qd = cdof_qd.reshape((cd.ang.shape[0], num_dofs, -1))
    cds = [cd]

    # accumulate cd as successive cdof velocities
    for i in range(0, num_dofs - 1):
      cds.append(cds[-1] + cdof_qd.take(i, axis=1))

    # interleave cds back together to match joint stack order
    cd = jax.tree_map(lambda *x: jp.column_stack(x), *cds).reshape((-1, 3))
    cdofd = cd.vmap().cross(cdof)

    return cdofd

  cd_p = cd.concatenate(Motion.zero(shape=(1,))).take(pidx)
  cdofd = scan.link_types(sys, cdofd_fn, 'ldd', 'd', cd_p, cdof, cdof_qd)

  return com, cinr, cd, cdof, cdofd


def inverse(
    sys: System,
    qd: jp.ndarray,
    cinr: jp.ndarray,
    cd: jp.ndarray,
    cdof: jp.ndarray,
    cdofd: jp.ndarray,
) -> jp.ndarray:
  """Calculates the system's forces given input motions.

  This function computes inverse dynamics using the Newton-Euler algorithm:

  https://scaron.info/robot-locomotion/recursive-newton-euler-algorithm.html

  Args:
    sys: a brax system
    qd: joint velocity vector
    cinr: inertia in com frame
    cd: body velocities in com frame
    cdof: dofs in com frame
    cdofd: cdof velocity

  Returns:
    tau: generalized forces resulting from joint positions and velocities
  """
  # forward scan over tree: accumulate link center of mass acceleration
  def cdd_fn(cdd_parent, cdofd, qd, dof_idx):
    if cdd_parent is None:
      cdd_parent = Motion.create(vel=-sys.gravity.reshape((1, 3)))

    # cdd = cdd[parent] + map-sum(cdofd * qd)
    cdd = cdd_parent.index_sum(dof_idx, jax.vmap(lambda x, y: x * y)(cdofd, qd))

    return cdd

  cdd = scan.tree(sys, cdd_fn, 'ddd', cdofd, qd, sys.dof_link(depth=True))

  # cfrc_flat = cinr * cdd + cd x (cinr * cd)
  def frc(cinr, cdd, cd):
    return cinr.mul(cdd) + cd.cross(cinr.mul(cd))

  cfrc_flat = jax.vmap(frc)(cinr, cdd, cd)

  # backward scan up tree: accumulate link center of mass forces
  def cfrc_fn(cfrc_child, cfrc):
    if cfrc_child is not None:
      cfrc += cfrc_child
    return cfrc

  cfrc = scan.tree(sys, cfrc_fn, 'l', cfrc_flat, reverse=True)

  # tau = cdof * cfrc[dof_link]
  tau = jax.vmap(lambda x, y: x.dot(y))(cdof, cfrc.take(sys.dof_link()))

  return tau


def _passive(sys: System, q: jp.ndarray, qd: jp.ndarray) -> jp.ndarray:
  """Calculates the system's passive forces given input motion and position."""

  def stiffness_fn(typ, q, dof):
    if typ in 'fb':
      return jp.zeros_like(dof.stiffness)
    return -q * dof.stiffness

  frc = scan.link_types(sys, stiffness_fn, 'qd', 'd', q, sys.dof)
  frc -= sys.dof.damping * qd

  return frc


def forward(
    sys: System,
    q: jp.ndarray,
    qd: jp.ndarray,
    cinr: Inertia,
    cd: Motion,
    cdof: Motion,
    cdofd: Motion,
    tau: jp.ndarray,
) -> jp.ndarray:
  """Calculates the system's motion given input forces.

  This method builds and solves the linear system: M @ qdd = -C + tau

  where M is the joint space inertia matrix, "mass matrix" and
        C is the bias force calculated by inverse()

  Args:
    sys: a brax system
    q: joint position vector
    qd: joint velocity vector
    cinr: inertia in com frame
    cd: body velocities in com frame
    cdof: dofs in com frame
    cdofd: cdof velocity
    tau: joint force input vector

  Returns:
    qdd: joint acceleration vector
  """
  qfrc_passive = _passive(sys, q, qd)
  qfrc_bias = inverse(sys, qd, cinr, cd, cdof, cdofd)
  qfrc = qfrc_passive - qfrc_bias + tau

  return qfrc