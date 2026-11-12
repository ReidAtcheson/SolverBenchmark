"""Shared, range-safe arithmetic for the generated electrical networks."""
import math


def line_admittance(resistance, reactance):
    """Return 1/(r+i*x) for finite r>=0 and x>0 without squaring their scale.

    Direct complex division can overflow its internal denominator even when
    both reciprocal components are representable. Normalize first, then divide
    by the original scale only once. A component may legitimately underflow;
    an entirely zero or nonfinite admittance is rejected.
    """
    if (not math.isfinite(resistance) or resistance < 0
            or not math.isfinite(reactance) or reactance <= 0):
        raise ValueError('line impedance must have finite resistance >= 0 and reactance > 0')
    scale = max(resistance, reactance)
    r_scaled, x_scaled = resistance / scale, reactance / scale
    denominator = r_scaled*r_scaled + x_scaled*x_scaled
    conductance = (r_scaled / denominator) / scale
    susceptance = (x_scaled / denominator) / scale
    if (not math.isfinite(conductance) or not math.isfinite(susceptance)
            or not (conductance or susceptance)):
        raise ValueError('line admittance outside binary64 range')
    return complex(conductance, -susceptance)
