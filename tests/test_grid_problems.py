"""Electrical invariants, branch physics, and independent differential checks."""
import cmath
import math
import unittest

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
