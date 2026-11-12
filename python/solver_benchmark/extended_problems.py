"""Small, deterministic sparse model problems; see docs/generated-matrices.md."""
from array import array
import math


def build(generator, p, limit, seed="0", *, max_order=(1 << 31)-1):
    def integer(k, minimum=1):
        v = p[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise ValueError(f'{k} must be an integer >= {minimum}')
        return v
    def number(k, minimum=None):
        v = p[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} must be finite')
        try:
            v = float(v)
        except OverflowError as e:
            raise ValueError(f'{k} must be finite') from e
        if not math.isfinite(v) or (minimum is not None and v <= minimum):
            raise ValueError(f'{k} is out of range')
        return v
    specs = {
        'convection_diffusion_2d': ('nx', 'ny', 'diffusion', 'velocity_x', 'velocity_y'),
        'helmholtz_2d': ('nx', 'ny', 'wavenumber'),
        'convection_diffusion_3d': ('nx', 'ny', 'nz', 'diffusion', 'velocity_x', 'velocity_y', 'velocity_z'),
        'helmholtz_3d': ('nx', 'ny', 'nz', 'wavenumber'),
        'grid_dc_power_flow': ('buses', 'reactance', 'chord_stride'),
        'grid_ac_jacobian': ('buses', 'resistance', 'reactance', 'chord_stride'),
        'triangular_fem_poisson': ('nx', 'ny'),
    }
    if generator not in specs:
        raise ValueError(f'unknown matrix generator: {generator}')
    if not isinstance(p, dict) or set(p) != set(specs[generator]):
        raise ValueError(f'{generator} requires exactly {", ".join(specs[generator])}')
    if generator.startswith('grid_'):
        n = integer('buses', 3); stride = integer('chord_stride', 2)
        if stride >= n:
            raise ValueError('chord_stride must be smaller than buses')
        order = (n - 1) * (2 if generator == 'grid_ac_jacobian' else 1)
        bound = 20 * n
    else:
        nx, ny = integer('nx'), integer('ny')
        nz = integer('nz') if generator.endswith('3d') else 1
        order = nx * ny * nz
        bound = 9 * order
    if order > max_order or order > limit or bound > limit:
        raise ValueError(f'case exceeds local max_entries={limit}')
    rows = [dict() for _ in range(order)]
    def add(i, j, v):
        rows[i][j] = rows[i].get(j, 0.0) + v
    if generator.startswith(('convection_diffusion_', 'helmholtz_')):
        transport = generator.startswith('convection_diffusion_')
        sizes = [nx, ny] + ([nz] if generator.endswith('3d') else [])
        if transport:
            diffusion = number('diffusion', 0)
            velocities = [number('velocity_' + axis) for axis in 'xyz'[:len(sizes)]]
            shift = 0
        else:
            diffusion = 1
            velocities = [0] * len(sizes)
            k = number('wavenumber', 0)
            shift = k * k
        strides = [1, nx, nx * ny]
        for i in range(order):
            diag = -shift
            for m, step, v in zip(sizes, strides, velocities):
                c, h = (i // step) % m, 1 / (m + 1)
                w = diffusion / h**2
                if not math.isfinite(w) or w <= 0:
                    raise ValueError('diffusion weights must be positive finite binary64')
                diag += 2*w + abs(v)/h
                if c: add(i, i-step, -w-max(v, 0)/h)
                if c+1 < m: add(i, i+step, -w-max(-v, 0)/h)
            add(i, i, diag)
        asymmetric = any(v and m > 1 for m, v in zip(sizes, velocities))
        props = {'symmetric': 'no' if asymmetric else 'yes', 'structurally_symmetric': 'yes',
                 'positive_definite': 'yes' if transport and not asymmetric else 'unknown'}
    elif generator == 'triangular_fem_poisson':
        # Two linear triangles per cell, Dirichlet nodes eliminated. An alternating
        # diagonal gives a non-Cartesian adjacency without external meshing tools.
        dx, dy = 1/(nx+1), 1/(ny+1)
        for y in range(ny+1):
            for x in range(nx+1):
                a, b, c, d = (x,y), (x+1,y), (x,y+1), (x+1,y+1)
                tris = ((a,b,d),(a,d,c)) if (x+y)%2 == 0 else ((a,b,c),(b,d,c))
                for tri in tris:
                    coords = [(xx*dx + (0.17*dx if 0 < xx <= nx and 0 < yy <= ny and (xx+yy)%2 else 0),
                               yy*dy + (0.11*dy if 0 < xx <= nx and 0 < yy <= ny and xx%2 else 0)) for xx,yy in tri]
                    det = ((coords[1][0]-coords[0][0])*(coords[2][1]-coords[0][1]) -
                           (coords[2][0]-coords[0][0])*(coords[1][1]-coords[0][1]))
                    gradients = [(coords[(i+1)%3][1]-coords[(i+2)%3][1],
                                  coords[(i+2)%3][0]-coords[(i+1)%3][0]) for i in range(3)]
                    for i,(xi,yi) in enumerate(tri):
                        if not (1 <= xi <= nx and 1 <= yi <= ny): continue
                        for j,(xj,yj) in enumerate(tri):
                            if 1 <= xj <= nx and 1 <= yj <= ny:
                                val = sum(gradients[i][t]*gradients[j][t] for t in (0,1))/(2*abs(det))
                                if val: add((yi-1)*nx+xi-1, (yj-1)*nx+xj-1, val)
        props = {'symmetric':'yes', 'structurally_symmetric':'yes', 'positive_definite':'yes'}
    else:
        x = number('reactance', 0)
        resistance = number('resistance', 0) if generator == 'grid_ac_jacobian' else 0
        # Connected path plus chords. Bus zero is the fixed slack bus.
        edges = [(i,i+1) for i in range(n-1)] + [(i,i+stride) for i in range(n-stride)]
        if generator == 'grid_dc_power_flow':
            weights = ((0,0,1/x),)
        else:
            denom = resistance**2 + x**2
            g, b = resistance/denom, x/denom
            # Flat-start polar power Jacobian: [B G; -G B].
            weights = ((0,0,b),(0,1,g),(1,0,-g),(1,1,b))
        m = n-1
        for i,j in edges:
            for a,b,w in weights:
                if i: add(a*m+i-1,b*m+i-1,w)
                if j: add(a*m+j-1,b*m+j-1,w)
                if i and j:
                    add(a*m+i-1,b*m+j-1,-w); add(a*m+j-1,b*m+i-1,-w)
        props = {'symmetric':'yes' if generator == 'grid_dc_power_flow' else 'no',
                 'structurally_symmetric':'yes', 'positive_definite':'yes' if generator == 'grid_dc_power_flow' else 'unknown'}
    offsets, columns, values = array('q',[0]), array('i'), array('d')
    for row in rows:
        for j,v in sorted(row.items()):
            if not math.isfinite(v): raise ValueError('matrix contains nonfinite values')
            if v != 0:
                columns.append(j); values.append(v)
        offsets.append(len(columns))
    return offsets, columns, values, props
