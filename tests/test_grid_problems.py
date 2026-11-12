"""Electrical invariants, branch physics, and independent differential checks."""
import cmath
import math
import unittest
from decimal import Decimal, localcontext

from solver_benchmark.electrical import line_admittance
from solver_benchmark.problems import generate_case, decode_array
from test_generated_matrices import case, matrix

from solver_benchmark.grid_problems import (
    build, network, operating_point, admittance, power_injections,
)

DC = dict(buses=15, mesh_fraction=0.35, reactance=0.1,
          impedance_spread=0.6, tap_spread=0.08)
AC = dict(DC, resistance=0.02, pv_fraction=0.35, angle_scale=0.2,
          voltage_spread=0.08, phase_shift=0.1, charging=0.02)


def dense(result):
    offsets, columns, values, _ = result
    n = len(offsets)-1; a = [[0.]*n for _ in range(n)]
    for i in range(n):
        for k in range(offsets[i],offsets[i+1]): a[i][columns[k]] = values[k]
    return a


def solve(a,b):
    a = [r[:] + [v] for r,v in zip(a,b)]; n = len(b)
    for k in range(n):
        pivot = max(range(k,n),key=lambda i:abs(a[i][k]))
        a[k],a[pivot] = a[pivot],a[k]
        if abs(a[k][k]) < 1e-13: raise AssertionError('singular matrix')
        for i in range(k+1,n):
            f = a[i][k]/a[k][k]
            for j in range(k+1,n+1): a[i][j] -= f*a[k][j]
    x = [0.]*n
    for i in range(n-1,-1,-1): x[i] = (a[i][n]-sum(a[i][j]*x[j] for j in range(i+1,n)))/a[i][i]
    return x


def branch_power(branches, angles, mags):
    """Evaluate terminal currents from the transformer circuit directly."""
    voltage = [cmath.rect(m,a) for m,a in zip(mags,angles)]
    current = [0j]*len(voltage)
    for f,t,r,x,tap,shift,charging in branches:
        ratio = cmath.rect(tap,shift)
        secondary_from = voltage[f]/ratio
        series_current = (secondary_from-voltage[t])/complex(r,x)
        current[f] += (series_current+0.5j*charging*secondary_from)/ratio.conjugate()
        current[t] += -series_current+0.5j*charging*voltage[t]
    return [v*i.conjugate() for v,i in zip(voltage,current)]


class GridProblemTests(unittest.TestCase):
    def test_connected_tree_and_loops_seeded(self):
        for fraction in (0.,0.3,1.):
            p = dict(DC, mesh_fraction=fraction)
            points, branches = network(p,'23')
            self.assertEqual((points,branches), network(p,'23'))
            reached = {0}
            while True:
                previous = len(reached)
                for f,t,*_ in branches:
                    if f in reached or t in reached: reached.update((f,t))
                if len(reached) == previous: break
            self.assertEqual(len(reached),p['buses'])
            self.assertLessEqual(len(branches),4*p['buses'])
            if fraction == 0: self.assertEqual(len(branches),p['buses']-1)
            else: self.assertGreater(len(branches),p['buses']-1)
        self.assertNotEqual(network(DC,'23'),network(DC,'24'))

    def test_dc_branch_energy_and_positive_cholesky(self):
        a = dense(build('geographic_grid_dc',DC,10000,'42'))
        _, branches = network(DC,'42')
        x = [0.] + [math.sin(i*1.7) for i in range(len(a))]
        energy = sum(x[i+1]*v*x[j+1] for i,row in enumerate(a) for j,v in enumerate(row))
        exact = sum((x[f]-x[t])**2/reactance/tap for f,t,r,reactance,tap,shift,b in branches)
        self.assertAlmostEqual(energy,exact,places=11)
        l = [[0.]*len(a) for _ in a]
        for i in range(len(a)):
            for j in range(i+1):
                v = a[i][j]-sum(l[i][k]*l[j][k] for k in range(j))
                if i == j:
                    self.assertGreater(v,0); l[i][j] = math.sqrt(v)
                else: l[i][j] = v/l[j][j]

    def test_nonflat_ac_with_pv_pq_and_transformers(self):
        for fraction in (0.,0.35,1.):
            p = dict(AC,pv_fraction=fraction)
            a = dense(build('geographic_grid_ac',p,10000,'21'))
            points,branches = network(p,'21')
            angles,mags,pq = operating_point(p,points)
            self.assertEqual(len(a),p['buses']-1+len(pq))
            self.assertGreater(max(angles)-min(angles),0)
            direct = branch_power(branches,angles,mags)
            via_y = power_injections(admittance(p['buses'],branches),angles,mags)
            for x,y in zip(direct,via_y): self.assertLess(abs(x-y),1e-12)
            for col in range(len(a)):
                ap,am,mp,mm = angles[:],angles[:],mags[:],mags[:]
                eps = 1e-6
                if col < p['buses']-1:
                    ap[col+1] += eps; am[col+1] -= eps
                else:
                    bus = pq[col-p['buses']+1]; mp[bus] += eps; mm[bus] -= eps
                plus,minus = branch_power(branches,ap,mp),branch_power(branches,am,mm)
                diff = [(x-y)/(2*eps) for x,y in zip(plus,minus)]
                expected = [z.real for z in diff[1:]]+[diff[i].imag for i in pq]
                for row,value in enumerate(expected): self.assertAlmostEqual(a[row][col],value,delta=2e-8*max(1,abs(value)))

    def test_dc_opf_kkt_dispatch_balance_optimality_and_indefinite(self):
        p = dict(DC,generation_cost=2.)
        result = build('grid_dc_opf_kkt',p,10000,'5'); a = dense(result); n = p['buses']
        self.assertEqual(result[3]['positive_definite'],'no')
        self.assertEqual(a,[list(row) for row in zip(*a)])
        demand = [0.5+0.1*math.sin(i) for i in range(n)]
        rhs = [0.]*(2*n-1)+demand
        x = solve(a,rhs)
        self.assertAlmostEqual(sum(x[:n]),sum(demand),places=10)
        marginal = [p['generation_cost']*(1+i/n)*x[i] for i in range(n)]
        self.assertLess(max(marginal)-min(marginal),1e-10)
        self.assertLess(max(abs(sum(v*y for v,y in zip(row,x))-b) for row,b in zip(a,rhs)),1e-10)
        # Positive p direction and negative p/lambda direction give both signs.
        self.assertGreater(a[0][0],0)
        self.assertLess(a[0][0]-2*(p['generation_cost']+1)*a[0][2*n-1],0)

    def test_line_admittance_against_high_precision_reference(self):
        # Decimal evaluates the original reciprocal formula, independently of
        # the normalized binary64 implementation. Include unequal impedances,
        # zero resistance, subnormal outputs, and a vanished small component.
        for r,x in ((1e308,1e308), (1e308,5e307), (0.,1e308),
                    (3e-200,4e-200), (1e200,1e-200), (0.02,0.1)):
            with self.subTest(resistance=r,reactance=x), localcontext() as context:
                context.prec = 80
                dr,dx = Decimal.from_float(r),Decimal.from_float(x)
                denominator = dr*dr+dx*dx
                expected = (float(dr/denominator), float(-dx/denominator))
                actual = line_admittance(r,x)
                for value,reference in zip((actual.real,actual.imag),expected):
                    self.assertTrue(math.isfinite(value))
                    if reference == 0:
                        self.assertEqual(value,0)
                    else:
                        self.assertAlmostEqual(value/reference,1,delta=3e-15)
        for r,x in ((1e-320,1e-320), (0.,1e-320)):
            with self.subTest(resistance=r,reactance=x), self.assertRaisesRegex(ValueError,'admittance'):
                line_admittance(r,x)

    def test_geographic_extreme_impedance_public_recipe(self):
        p = dict(buses=3,mesh_fraction=0,reactance=1e308,impedance_spread=0,
                 tap_spread=0,resistance=1e308,pv_fraction=0,angle_scale=0,
                 voltage_spread=0,phase_shift=0,charging=0)
        recipe = case('geographic_grid_ac',p)
        recipe['generator']['seed'] = '1'
        problem = generate_case(recipe)
        actual = matrix(problem)
        self.assertEqual(problem['matrix']['order'],4)
        self.assertGreater(int(problem['matrix']['nonzeros']),0)
        self.assertTrue(any(decode_array('d',problem['arrays']['rhs'])))
        _,branches = network(p,'1')
        # Flat all-PQ Jacobian [B G; -G B], formed from high-precision
        # reciprocal impedances of the generated lines, not the Ybus helper.
        with localcontext() as context:
            context.prec = 80
            expected = [[Decimal(0) for _ in range(4)] for _ in range(4)]
            for i,j,r,x,_,_,_ in branches:
                dr,dx = Decimal.from_float(r),Decimal.from_float(x)
                denom = dr*dr+dx*dx
                g,b = dr/denom,dx/denom
                for rowblock,colblock,weight in ((0,0,b),(0,1,g),(1,0,-g),(1,1,b)):
                    for row,col,sign in ((i,i,1),(j,j,1),(i,j,-1),(j,i,-1)):
                        if row and col:
                            expected[2*rowblock+row-1][2*colblock+col-1] += sign*weight
            for i in range(4):
                for j in range(4):
                    reference = float(expected[i][j])
                    value = actual.get((i,j),0)
                    if reference:
                        self.assertAlmostEqual(value/reference,1,delta=5e-15)
                    else:
                        self.assertEqual(value,0)
        recipe['generator']['parameters'] = dict(p,resistance=1e-320,reactance=1e-320)
        with self.assertRaisesRegex(ValueError,'admittance'):
            generate_case(recipe)

    def test_bounds_validation_and_reproducibility(self):
        for name,p in [('geographic_grid_dc',DC),('geographic_grid_ac',AC),('grid_dc_opf_kkt',dict(DC,generation_cost=1.))]:
            self.assertEqual(build(name,p,10000,'0'),build(name,p,10000,'0'))
            with self.assertRaises(ValueError): build(name,p,40*p['buses']-1)
            with self.assertRaises(ValueError): build(name,dict(p,extra=1),10000)
            with self.assertRaises(ValueError): build(name,dict(p,buses=True),10000)
            for seed in ('-1','01','18446744073709551616',2):
                with self.assertRaises(ValueError): build(name,p,10000,seed)
        for field,value in [('reactance',0),('reactance',1e-320),('reactance',1e308),('mesh_fraction',-0.1),('tap_spread',1),('impedance_spread',float('nan')),('buses',10**20)]:
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                build('geographic_grid_dc',dict(DC,**{field:value}),10000)
        for field,value in [('angle_scale',4),('voltage_spread',1),('resistance',-1),('charging',float('inf'))]:
            with self.assertRaises(ValueError): build('geographic_grid_ac',dict(AC,**{field:value}),10000)
