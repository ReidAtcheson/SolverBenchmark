"""Dependency-free P1 simplex and surface FEM matrices with uniform refinement.

The meshes are conforming perturbed Cartesian triangulations, not Delaunay
meshes. See docs/mesh-generators.md for geometry, weak forms, and contracts.
"""
from array import array
import itertools
import math


GENERATORS = ('fem_diffusion_square', 'fem_diffusion_cube', 'surface_diffusion_torus')


def _unit_random(seed, index):
    """Index-addressable SplitMix64 output, converted using its top 53 bits."""
    mask = (1 << 64) - 1
    z = (seed + (index + 1) * 0x9e3779b97f4a7c15) & mask
    z = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & mask
    z = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & mask
    z ^= z >> 31
    return (z >> 11) * 2.0**-53


def box_mesh(cells, dimension, jitter, seed):
    """Freudenthal simplices on a unit box; boundary vertices stay fixed."""
    nodes = list(itertools.product(range(cells + 1), repeat=dimension))
    ids = {node: i for i, node in enumerate(nodes)}
    vertices = []
    for i, node in enumerate(nodes):
        interior = all(0 < c < cells for c in node)
        vertices.append(tuple((c + (jitter * (2*_unit_random(seed, dimension*i+k)-1)
                                      if interior else 0)) / cells for k, c in enumerate(node)))
    elements = []
    for cell in itertools.product(range(cells), repeat=dimension):
        for permutation in itertools.permutations(range(dimension)):
            node = list(cell)
            simplex = [ids[tuple(node)]]
            for axis in permutation:
                node[axis] += 1
                simplex.append(ids[tuple(node)])
            elements.append(tuple(simplex))
    return vertices, elements


def torus_mesh(major_segments, minor_segments, major_radius, minor_radius):
    def position(theta, phi):
        return ((major_radius + minor_radius*math.cos(phi))*math.cos(theta),
                (major_radius + minor_radius*math.cos(phi))*math.sin(theta),
                minor_radius*math.sin(phi))
    vertices = [position(2*math.pi*i/major_segments, 2*math.pi*j/minor_segments)
                for i in range(major_segments) for j in range(minor_segments)]
    def node(i, j): return (i % major_segments)*minor_segments + j % minor_segments
    elements = []
    for i in range(major_segments):
        for j in range(minor_segments):
            a, b, c, d = node(i,j), node(i+1,j), node(i,j+1), node(i+1,j+1)
            elements.extend(((a,b,d), (a,d,c)))
    def midpoint(a, b):
        # Midpoints in the periodic parameter domain avoid snapping coarse
        # chord midpoints to the wrong side of a thin torus.
        angles = [(math.atan2(v[1],v[0]), math.atan2(v[2],math.hypot(v[0],v[1])-major_radius))
                  for v in (a,b)]
        mid = [angles[0][k] + math.remainder(angles[1][k]-angles[0][k],2*math.pi)/2
               for k in (0,1)]
        return position(*mid)
    return vertices, elements, midpoint


def refine(vertices, elements, midpoint=None):
    """Conforming 1-to-4 triangle or 1-to-8 tetrahedral subdivision."""
    vertices = list(vertices)
    edge_nodes = {}
    def edge(a, b):
        key = tuple(sorted((a,b)))
        if key not in edge_nodes:
            point = (midpoint(vertices[a], vertices[b]) if midpoint else
                     tuple((x+y)/2 for x,y in zip(vertices[a],vertices[b])))
            edge_nodes[key] = len(vertices)
            vertices.append(point)
        return edge_nodes[key]
    children = []
    for simplex in elements:
        if len(simplex) == 3:
            a,b,c = simplex
            ab,ac,bc = edge(a,b),edge(a,c),edge(b,c)
            children.extend(((a,ab,ac),(b,bc,ab),(c,ac,bc),(ab,bc,ac)))
        else:
            a,b,c,d = simplex
            ab,ac,ad,bc,bd,cd = (edge(a,b),edge(a,c),edge(a,d),edge(b,c),edge(b,d),edge(c,d))
            children.extend(((a,ab,ac,ad),(b,ab,bc,bd),(c,ac,bc,cd),(d,ad,bd,cd),
                             (ab,cd,ac,ad),(ab,cd,ad,bd),(ab,cd,bd,bc),(ab,cd,bc,ac)))
    return vertices, children


def simplex_matrices(points):
    """Exact P1 stiffness and consistent mass on a triangle or tetrahedron.

    The edge Gram matrix also handles a triangle embedded in 3D, giving
    intrinsic piecewise-flat surface gradients without a coordinate chart.
    """
    d = len(points)-1
    edges = [[b-a for a,b in zip(points[0],p)] for p in points[1:]]
    gram = [[math.fsum(x*y for x,y in zip(a,b)) for b in edges] for a in edges]
    if d == 2:
        a,b = gram[0]; _,c = gram[1]
        determinant = a*c-b*b
        adjugate = [[c,-b],[-b,a]]
    elif d == 3:
        a,b,c = gram[0]; _,d1,e = gram[1]; _,_,f = gram[2]
        adjugate = [[d1*f-e*e,c*e-b*f,b*e-c*d1],
                    [c*e-b*f,a*f-c*c,b*c-a*e],
                    [b*e-c*d1,b*c-a*e,a*d1-b*b]]
        determinant = math.fsum(gram[0][i]*adjugate[i][0] for i in range(3))
    else:
        raise ValueError('only triangles and tetrahedra are supported')
    if not math.isfinite(determinant) or determinant <= 0:
        raise ValueError('degenerate simplex or geometry outside binary64 range')
    volume = math.sqrt(determinant)/math.factorial(d)
    inverse = [[v/determinant for v in row] for row in adjugate]
    # grad(phi_0) = -sum_{i>0} grad(phi_i).
    stiffness = [[0.0]*(d+1) for _ in range(d+1)]
    for i in range(d):
        for j in range(d): stiffness[i+1][j+1] = volume*inverse[i][j]
    for i in range(1,d+1):
        stiffness[0][i] = stiffness[i][0] = -math.fsum(stiffness[i][1:])
    stiffness[0][0] = -math.fsum(stiffness[0][1:])
    mass = [[volume*(2 if i == j else 1)/((d+1)*(d+2)) for j in range(d+1)] for i in range(d+1)]
    return stiffness, mass


def assemble(vertices, elements, diffusion, reaction, dirichlet, require_resolved_reaction=False):
    active = [i for i,v in enumerate(vertices) if not dirichlet or all(c != 0 and c != 1 for c in v)]
    mapping = {node: i for i,node in enumerate(active)}
    rows = [dict() for _ in active]
    reaction_sums = [0.0] * len(active)
    stiffness_scales = [0.0] * len(active)
    for element in elements:
        stiffness, mass = simplex_matrices([vertices[i] for i in element])
        for i, node_i in enumerate(element):
            if node_i not in mapping: continue
            row = rows[mapping[node_i]]
            for j, node_j in enumerate(element):
                if node_j not in mapping: continue
                column = mapping[node_j]
                value = diffusion*stiffness[i][j] + reaction*mass[i][j]
                if require_resolved_reaction:
                    reaction_sums[mapping[node_i]] += reaction*mass[i][j]
                    stiffness_scales[mapping[node_i]] += abs(diffusion*stiffness[i][j])
                row[column] = row.get(column,0.0) + value
    if require_resolved_reaction:
        # A positive mass term must survive assembly; otherwise a formally SPD
        # torus recipe can silently turn into a numerically singular Laplacian.
        for i, row in enumerate(rows):
            threshold = 64*math.ulp(stiffness_scales[i])
            if (not math.isfinite(reaction_sums[i]) or reaction_sums[i] <= threshold
                    or math.fsum(row.values()) <= threshold):
                raise ValueError('torus reaction is not resolved above binary64 stiffness roundoff')
    offsets, columns, values = array('q',[0]),array('i'),array('d')
    for row in rows:
        for column,value in sorted(row.items()):
            if not math.isfinite(value): raise ValueError('matrix arithmetic outside binary64 range')
            if value:
                columns.append(column); values.append(value)
        offsets.append(len(columns))
    if any(rows[i].get(i,0) <= 0 for i in range(len(rows))):
        raise ValueError('matrix diagonal underflowed')
    return offsets,columns,values


def build(generator, p, limit, seed='0', *, max_order=(1 << 31)-1):
    torus = generator == 'surface_diffusion_torus'
    if generator not in GENERATORS: raise ValueError(f'unknown matrix generator: {generator}')
    keys = (('major_segments','minor_segments','major_radius','minor_radius') if torus else ('cells','jitter')) + ('refinement','diffusion','reaction')
    if not isinstance(p,dict) or set(p) != set(keys):
        raise ValueError(f'{generator} requires exactly {", ".join(keys)}')
    def integer(key, minimum):
        v = p[key]
        if isinstance(v,bool) or not isinstance(v,int) or v < minimum:
            raise ValueError(f'{key} must be an integer >= {minimum}')
        return v
    def number(key):
        v = p[key]
        if isinstance(v,bool) or not isinstance(v,(int,float)):
            raise ValueError(f'{key} must be finite')
        try: v = float(v)
        except OverflowError as error: raise ValueError(f'{key} must be finite') from error
        if not math.isfinite(v): raise ValueError(f'{key} must be finite')
        return v
    levels = integer('refinement',0)
    diffusion, reaction = number('diffusion'),number('reaction')
    if diffusion <= 0 or reaction < 0 or (torus and reaction == 0):
        raise ValueError('diffusion must be positive; reaction must be nonnegative (positive on torus)')
    dimension = 3 if generator == 'fem_diffusion_cube' else 2
    if torus:
        major,minor = integer('major_segments',4),integer('minor_segments',4)
        rmajor,rminor = number('major_radius'),number('minor_radius')
        if not 0 < rminor < rmajor: raise ValueError('require major_radius > minor_radius > 0')
        elements = 2*major*minor
    else:
        cells = integer('cells',2); jitter = number('jitter')
        if not 0 <= jitter <= 0.15: raise ValueError('jitter must be in [0,0.15]')
        elements = math.factorial(dimension)*cells**dimension
    # Bound assembly contributions as well as CSR size, before allocating any
    # mesh. Bound the exponent before computing it for adversarial recipes.
    if levels > limit.bit_length() or elements*(dimension+1)**2 > limit // (2**dimension)**levels:
        raise ValueError(f'case exceeds local max_entries={limit} (mesh contribution bound)')
    final_elements = elements*(2**dimension)**levels
    # Every vertex is incident to at least one element. This deliberately
    # conservative bound includes boundary vertices before their elimination.
    if (dimension+1)*final_elements > max_order:
        raise ValueError('mesh exceeds wire index or dense order limits (vertex bound)')
    if torus:
        vertices, simplices, midpoint = torus_mesh(major,minor,rmajor,rminor)
    else:
        vertices, simplices = box_mesh(cells,dimension,jitter,int(seed))
        midpoint = None
    for _ in range(levels): vertices,simplices = refine(vertices,simplices,midpoint)
    if len(vertices) > (1 << 31)-1: raise ValueError('mesh exceeds wire index limits')
    arrays = assemble(vertices,simplices,diffusion,reaction,not torus,require_resolved_reaction=torus)
    return (*arrays, {'symmetric':'yes','structurally_symmetric':'yes','positive_definite':'yes'})
