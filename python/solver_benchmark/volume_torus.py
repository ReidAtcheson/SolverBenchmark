"""P1 diffusion on a tetrahedral solid torus, without external mesh data.

A triangulated filled disk is swept around a periodic angular direction and
split into conforming tetrahedra. See docs/volume-torus.md for the geometry,
Dirichlet boundary, resolution controls, and polyhedral refinement convention.
"""
from array import array
from collections import Counter
from itertools import combinations
import math

from .mesh_problems import refine, simplex_matrices

GENERATOR = 'volume_diffusion_torus'
PARAMETERS = ('major_segments', 'minor_segments', 'radial_layers', 'refinement',
              'major_radius', 'minor_radius', 'diffusion', 'reaction')


def torus_volume_mesh(major_segments, minor_segments, radial_layers,
                      major_radius, minor_radius):
    """Return vertices and tetrahedra for a swept, filled polygonal disk.

    The disk center is one node per major-angle section, never a collapsed
    angular ring. Sorting disk vertex IDs gives matching diagonals on every
    shared prism side, including the periodic major-angle seam.
    """
    disk = [(0.0, 0.0)]
    for layer in range(1, radial_layers + 1):
        radius = minor_radius * (layer / radial_layers)
        disk.extend((radius*math.cos(2*math.pi*j/minor_segments),
                     radius*math.sin(2*math.pi*j/minor_segments))
                    for j in range(minor_segments))

    def node(layer, j):
        return 1 + (layer - 1)*minor_segments + j % minor_segments

    triangles = [(0, node(1,j), node(1,j+1)) for j in range(minor_segments)]
    for layer in range(1, radial_layers):
        for j in range(minor_segments):
            a,b = node(layer,j), node(layer,j+1)
            c,d = node(layer+1,j), node(layer+1,j+1)
            triangles.extend(((a,c,d), (a,d,b)))
    cross_count = len(disk)
    vertices = []
    for i in range(major_segments):
        theta = 2*math.pi*i/major_segments
        cosine, sine = math.cos(theta), math.sin(theta)
        vertices.extend(((major_radius+s)*cosine, (major_radius+s)*sine, z)
                        for s,z in disk)
    elements = []
    for i in range(major_segments):
        lower = i*cross_count
        upper = ((i+1) % major_segments)*cross_count
        for triangle in triangles:
            a,b,c = sorted(triangle)
            elements.extend(((lower+a, lower+b, lower+c, upper+c),
                             (lower+a, lower+b, upper+b, upper+c),
                             (lower+a, upper+a, upper+b, upper+c)))
    return vertices, elements


def boundary_vertices(elements):
    """Identify the exterior from topology, including after midpoint refinement."""
    faces = Counter(tuple(sorted(face)) for tetra in elements for face in combinations(tetra,3))
    if any(count not in (1,2) for count in faces.values()):
        raise ValueError('nonmanifold tetrahedral mesh')
    return {node for face,count in faces.items() if count == 1 for node in face}


def assemble_volume(vertices, elements, diffusion, reaction):
    boundary = boundary_vertices(elements)
    active = {node:i for i,node in enumerate(j for j in range(len(vertices)) if j not in boundary)}
    if not active:
        raise ValueError('volume mesh has no interior unknowns')
    rows = [dict() for _ in active]
    for element in elements:
        stiffness, mass = simplex_matrices([vertices[node] for node in element])
        for i,node in enumerate(element):
            if node not in active:
                continue
            row = rows[active[node]]
            for j,neighbor in enumerate(element):
                if neighbor in active:
                    column = active[neighbor]
                    value = diffusion*stiffness[i][j] + reaction*mass[i][j]
                    row[column] = row.get(column,0.0) + value
    offsets, columns, values = array('q',[0]), array('i'), array('d')
    for i,row in enumerate(rows):
        if row.get(i,0) <= 0:
            raise ValueError('volume stiffness diagonal underflowed')
        for column,value in sorted(row.items()):
            if not math.isfinite(value):
                raise ValueError('volume matrix arithmetic outside binary64 range')
            if value:
                columns.append(column)
                values.append(value)
        offsets.append(len(columns))
    return offsets, columns, values


def build(generator, p, limit, seed='0', *, max_order=(1 << 31)-1):
    """Generate a real SPD tetrahedral diffusion--reaction matrix."""
    if generator != GENERATOR:
        raise ValueError(f'unknown matrix generator: {generator}')
    if not isinstance(p,dict) or set(p) != set(PARAMETERS):
        raise ValueError(f'{generator} requires exactly {", ".join(PARAMETERS)}')

    def integer(name, minimum):
        value = p[name]
        if isinstance(value,bool) or not isinstance(value,int) or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
        return value

    def number(name):
        value = p[name]
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise ValueError(f'{name} must be finite')
        try:
            value = float(value)
        except OverflowError as error:
            raise ValueError(f'{name} must fit binary64') from error
        if not math.isfinite(value):
            raise ValueError(f'{name} must be finite')
        return value

    major,minor = integer('major_segments',4), integer('minor_segments',4)
    radial,levels = integer('radial_layers',1), integer('refinement',0)
    rmajor,rminor = number('major_radius'), number('minor_radius')
    diffusion,reaction = number('diffusion'), number('reaction')
    if not 0 < rminor < rmajor:
        raise ValueError('require major_radius > minor_radius > 0')
    if diffusion <= 0 or reaction < 0:
        raise ValueError('diffusion must be positive and reaction nonnegative')
    if isinstance(limit,bool) or not isinstance(limit,int) or limit < 1:
        raise ValueError('limit must be a positive integer')
    coarse_elements = 3*major*minor*(2*radial-1)
    # Bound tetrahedron assembly work, vertices, refinement and boundary maps
    # before allocating any mesh. Guard the exponent against unbounded recipes.
    if levels > limit.bit_length() or 16*coarse_elements > limit // 8**levels:
        raise ValueError(f'case exceeds local max_entries={limit} (volume contribution bound)')
    final_elements = coarse_elements * 8**levels
    if 4*final_elements > min(max_order, (1 << 31)-1):
        raise ValueError('volume mesh exceeds wire index or dense order limits (vertex bound)')
    try:
        vertices,elements = torus_volume_mesh(major,minor,radial,rmajor,rminor)
        for _ in range(levels):
            vertices,elements = refine(vertices,elements)
        arrays = assemble_volume(vertices,elements,diffusion,reaction)
    except (OverflowError,ZeroDivisionError) as error:
        raise ValueError('volume generator arithmetic outside binary64 range') from error
    return (*arrays, {'symmetric':'yes', 'structurally_symmetric':'yes', 'positive_definite':'yes'})
