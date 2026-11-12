"""Seeded electrical networks and their linearizations, without external data.

Conventions, ordering, parameter ranges, and sources: docs/grid-generators.md.
"""
from array import array
import cmath
import math
import re

from .electrical import line_admittance

COMMON = ('buses', 'mesh_fraction', 'reactance', 'impedance_spread', 'tap_spread')
SPECS = {
    'geographic_grid_dc': COMMON,
    'geographic_grid_ac': COMMON + ('resistance', 'pv_fraction', 'angle_scale',
                                   'voltage_spread', 'phase_shift', 'charging'),
    'grid_dc_opf_kkt': COMMON + ('generation_cost',),
}


class _Random:
    """SplitMix64 with specified uint64 arithmetic; random() uses the top 53 bits."""
    def __init__(self, seed):
        if not isinstance(seed, str) or not re.fullmatch(r'0|[1-9][0-9]*', seed) or len(seed) > 20 or int(seed) >= 1 << 64:
            raise ValueError('seed must be a canonical uint64 string')
        self.state = int(seed)

    def random(self):
        mask = (1 << 64) - 1
        self.state = (self.state + 0x9E3779B97F4A7C15) & mask
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z ^= z >> 31
        return (z >> 11) * (1.0 / (1 << 53))


def _parameters(generator, p, limit, max_order):
    if generator not in SPECS:
        raise ValueError(f'unknown matrix generator: {generator}')
    if not isinstance(p, dict) or set(p) != set(SPECS[generator]):
        raise ValueError(f'{generator} requires exactly {", ".join(SPECS[generator])}')
    n = p['buses']
    if isinstance(n, bool) or not isinstance(n, int) or not 3 <= n <= (1 << 31) - 1:
        raise ValueError('buses must be an integer in [3, 2147483647]')
    # At most four candidate edges per bus. Bound final CSR plus intermediates
    # before constructing positions, candidate edges, Ybus, or row dictionaries.
    order = n - 1 if generator == 'geographic_grid_dc' else 2*n-2 if generator == 'geographic_grid_ac' else 3*n-1
    bound = 40*n
    if order > max_order or bound > limit:
        raise ValueError(f'case exceeds local max_entries={limit} (conservative bound {bound})')
    q = {'buses': n}
    for k in SPECS[generator][1:]:
        v = p[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} must be finite')
        try:
            v = float(v)
        except OverflowError as error:
            raise ValueError(f'{k} must fit binary64') from error
        if not math.isfinite(v):
            raise ValueError(f'{k} must be finite')
        if k in ('reactance', 'generation_cost') and not v > 0:
            raise ValueError(f'{k} must be positive')
        if k in ('mesh_fraction', 'pv_fraction') and not 0 <= v <= 1:
            raise ValueError(f'{k} must be in [0, 1]')
        if k in ('impedance_spread', 'tap_spread', 'voltage_spread') and not 0 <= v < 1:
            raise ValueError(f'{k} must be in [0, 1)')
        if k in ('resistance', 'charging') and v < 0:
            raise ValueError(f'{k} must be nonnegative')
        if k in ('angle_scale', 'phase_shift') and not 0 <= v <= math.pi:
            raise ValueError(f'{k} must be in [0, pi] radians')
        q[k] = v
    return q


def network(p, seed):
    """Build connected geographic topology and (from,to,r,x,tap,shift,bc) lines.

    Callers validate p and preflight resources before calling this helper.
    """
    n = p['buses']; rng = _Random(seed); width = math.isqrt(n-1) + 1
    points = [((i % width + 0.2*(2*rng.random()-1))/width,
               (i // width + 0.2*(2*rng.random()-1))/width) for i in range(n)]
    candidates = []
    for i in range(n):
        for di, dj in ((1,0), (0,1), (1,1), (-1,1)):
            col, row = i % width + di, i // width + dj
            j = row*width + col
            if 0 <= col < width and j < n:
                length = math.dist(points[i], points[j])*width
                candidates.append((length*(0.5+rng.random()), i, j, length))
    candidates.sort()
    roots = list(range(n)); sizes = [1]*n
    def root(i):
        while roots[i] != i:
            roots[i] = roots[roots[i]]; i = roots[i]
        return i
    selected, extra = [], []
    for _, i, j, length in candidates:
        a, b = root(i), root(j)
        if a != b:
            if sizes[a] < sizes[b]: a,b = b,a
            roots[b] = a; sizes[a] += sizes[b]
            selected.append((i,j,length))
        else:
            extra.append((i,j,length))
    selected.extend(edge for edge in extra if rng.random() < p['mesh_fraction'])
    selected.sort()
    branches = []
    for i,j,length in selected:
        factor = length*(1+p['impedance_spread']*(2*rng.random()-1))
        r, x = p.get('resistance',0)*factor, p['reactance']*factor
        tap = 1+p['tap_spread']*(2*rng.random()-1)
        shift = p.get('phase_shift',0)*(2*rng.random()-1)
        charging = p.get('charging',0)*length
        if not all(math.isfinite(v) for v in (r,x,tap,charging)) or x <= 0 or tap <= 0:
            raise ValueError('line parameters outside binary64 range')
        branches.append((i,j,r,x,tap,shift,charging))
    return points, branches


def operating_point(p, points):
    """Smooth non-flat voltage field, slack bus 0 fixed at 1+0j."""
    angles = [p['angle_scale']*(x-points[0][0]) for x,y in points]
    magnitudes = [1+p['voltage_spread']*math.sin(2*math.pi*x)*math.sin(2*math.pi*y) for x,y in points]
    angles[0], magnitudes[0] = 0., 1.
    # Deterministic evenly spaced PV buses, exactly floor(fraction*(n-1)).
    count = int(p['pv_fraction']*(len(points)-1))
    pv = {1+(k*(len(points)-1))//count for k in range(count)} if count else set()
    pq = [i for i in range(1,len(points)) if i not in pv]
    return angles, magnitudes, pq


def _add(rows, i, j, value):
    rows[i][j] = rows[i].get(j, 0) + value


def admittance(n, branches):
    rows = [dict() for _ in range(n)]
    for i,j,r,x,tap,shift,charging in branches:
        y = line_admittance(r,x); t = cmath.rect(tap,shift); diagonal = y+0.5j*charging
        for a,b,v in ((i,i,diagonal/tap**2), (j,j,diagonal), (i,j,-y/t.conjugate()), (j,i,-y/t)):
            if not math.isfinite(v.real) or not math.isfinite(v.imag):
                raise ValueError('admittance outside binary64 range')
            _add(rows,a,b,v)
    return rows


def power_injections(ybus, angles, magnitudes):
    voltage = [cmath.rect(m,a) for m,a in zip(magnitudes,angles)]
    return [voltage[i]*sum(y*voltage[j] for j,y in row.items()).conjugate() for i,row in enumerate(ybus)]


def _jacobian(ybus, angles, magnitudes, pq):
    n = len(ybus); m = n-1
    rows = [dict() for _ in range(m+len(pq))]
    qindex = {bus:m+k for k,bus in enumerate(pq)}
    voltage = [cmath.rect(v,a) for v,a in zip(magnitudes, angles)]
    for i in range(1,n):
        vi = voltage[i]
        current = sum(y*voltage[j] for j,y in ybus[i].items())
        for j,y in ybus[i].items():
            # Differentiate S_i = V_i conj(sum_j Y_ij V_j).
            da = -1j*vi*(y*voltage[j]).conjugate()
            dv = vi*(y*voltage[j]/magnitudes[j]).conjugate()
            if i == j:
                da += 1j*vi*current.conjugate()
                dv += vi/magnitudes[i]*current.conjugate()
            if j:
                _add(rows,i-1,j-1,da.real)
                if i in qindex: _add(rows,qindex[i],j-1,da.imag)
            if j in qindex:
                _add(rows,i-1,qindex[j],dv.real)
                if i in qindex: _add(rows,qindex[i],qindex[j],dv.imag)
    return rows


def _csr(rows, positive):
    offsets, columns, values = array('q',[0]), array('i'), array('d')
    for row in rows:
        for col,value in sorted(row.items()):
            if not math.isfinite(value): raise ValueError('matrix outside binary64 range')
            if value:
                columns.append(col); values.append(value)
        offsets.append(len(columns))
    structural = all(rows[j].get(i,0) != 0 for i,row in enumerate(rows) for j,v in row.items() if v)
    symmetric = structural and all(rows[j].get(i,0) == v for i,row in enumerate(rows) for j,v in row.items() if v)
    properties = {'symmetric':'yes' if symmetric else 'no',
                  'structurally_symmetric':'yes' if structural else 'no',
                  'positive_definite':positive}
    return offsets, columns, values, properties


def build(generator, p, limit, seed='0', *, max_order=(1 << 31)-1):
    p = _parameters(generator,p,limit,max_order)
    try:
        return _build(generator,p,seed)
    except (OverflowError, ZeroDivisionError) as error:
        raise ValueError('grid generator arithmetic outside binary64 range') from error


def _build(generator,p,seed):
    points, branches = network(p,seed); n = p['buses']
    if generator == 'geographic_grid_ac':
        ybus = admittance(n,branches)
        angles, magnitudes, pq = operating_point(p,points)
        return _csr(_jacobian(ybus,angles,magnitudes,pq), 'unknown')
    laplacian = [dict() for _ in range(n)]
    for i,j,_,x,tap,_,_ in branches:
        weight = 1/x/tap
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError('line susceptance outside binary64 range')
        for a,b,v in ((i,i,weight),(j,j,weight),(i,j,-weight),(j,i,-weight)):
            _add(laplacian,a,b,v)
    if generator == 'geographic_grid_dc':
        rows = [{j-1:v for j,v in laplacian[i].items() if j} for i in range(1,n)]
        return _csr(rows,'yes')
    # Equality-constrained DC economic dispatch. Variables [p_g (n), theta
    # (n-1), lambda (n)]; all bus balances p_g - B[:,1:] theta = demand.
    rows = [dict() for _ in range(3*n-1)]; multipliers = 2*n-1
    for i in range(n):
        _add(rows,i,i,p['generation_cost']*(1+i/n))
        _add(rows,i,multipliers+i,1.); _add(rows,multipliers+i,i,1.)
        for j,value in laplacian[i].items():
            if j:
                _add(rows,n+j-1,multipliers+i,-value)
                _add(rows,multipliers+i,n+j-1,-value)
    return _csr(rows,'no')
