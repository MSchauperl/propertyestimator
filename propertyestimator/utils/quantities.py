"""Contains a set of classes for storing and manipulating estimated quantities and
their uncertainties
"""

import numpy as np
from simtk import unit
from uncertainties import ufloat


class EstimatedQuantity:
    """A representation of an estimated quantity, which contains both the value of
    the quantity and the uncertainty in that value.

    Internally, the `uncertainty` package is used to linearly propagate uncertainties
    through simple arithmetic and scalar multiplication / division operations. The quantity
    must be accompanied by a valid string representation of its source, so as to ensure that
    only independent quantities can be combined.

    Warnings
    --------
    The implementation of this class is temporary - it will be replaced with
    a class which overrides the pint Quantity class when the codebase has
    been swapped to pint.

    Examples
    --------
    To add two **independent** quantities together:
    >>> from simtk import unit
    >>>
    >>> a_value = 5 * unit.angstrom
    >>> a_uncertainty = 0.03 * unit.angstrom
    >>>
    >>> b_value = 10 * unit.angstrom
    >>> b_uncertainty = 0.04 * unit.angstrom
    >>>
    >>> quantity_a = EstimatedQuantity(a_value, a_uncertainty, 'calc_325262315:npt_production')
    >>> quantity_b = EstimatedQuantity(b_value, b_uncertainty, 'calc_987234582:npt_production')
    >>>
    >>> quantity_addition = quantity_a + quantity_b

    To subtract one **independent** quantity from another:

    >>> quantity_subtraction = quantity_b - quantity_a

    Attempting to add / subtract quantities from the same source (i.e not independent)
    will raise a `DependantValuesException`.

    >>> c_value = 5.04 * unit.angstrom
    >>> c_uncertainty = 0.029 * unit.angstrom
    >>>
    >>> quantity_c = EstimatedQuantity(c_value, c_uncertainty, 'calc_325262315:npt_production')
    >>>
    >>> # The below will raise a DependantValuesException.
    >>> quantity_addition = quantity_a + quantity_c

    To multiply by a scalar:

    >>> quantity_scalar_multiply = quantity_a * 2.0

    To divide by a scalar:

    >>> quantity_scalar_divide = quantity_a / 2.0
    """

    @property
    def value(self):
        return self._value

    @property
    def uncertainty(self):
        return self._uncertainty

    @property
    def sources(self):
        return self._sources

    def __init__(self, value, uncertainty, *sources):
        """Constructs a new TaggedEstimatedQuantity object.
        Parameters
        ----------
        value: unit.Quantity
            The value of the estimated quantity.
        uncertainty: unit.Quantity
            The uncertainty in the value of the estimated quantity.
        sources: str
            A list of string representations of where this value came from. This
            value is employed whenever this object is involved in any
            mathematical operations to ensure values from the same source
            are not accidentally combined.

            An example of this may be the file path of the trajectory from
            which this value was derived, or the name of the workflow protocol
            which calculated it.
        """
        assert sources is not None and len(sources) > 0

        for source in sources:
            assert isinstance(source, str)

        assert value is not None and uncertainty is not None

        assert isinstance(value, unit.Quantity)
        assert isinstance(uncertainty, unit.Quantity)

        assert value.unit.is_compatible(uncertainty.unit)

        self._value = value
        self._uncertainty = uncertainty
        self._sources = list(sources)

    def __add__(self, other):

        assert isinstance(other, EstimatedQuantity)

        for source in other.sources:
            if source in self.sources:
                raise DependantValuesException()

        self_ufloat, self_unit = EstimatedQuantity._get_uncertainty_object(self)
        other_ufloat, other_unit = EstimatedQuantity._get_uncertainty_object(other)

        assert self_unit == other_unit

        result_ufloat = self_ufloat + other_ufloat

        result_value = result_ufloat.nominal_value * self_unit
        result_uncertainty = result_ufloat.std_dev * self_unit

        result_sources = []
        result_sources.extend(self.sources)
        result_sources.extend(other.sources)

        return EstimatedQuantity(result_value, result_uncertainty, *result_sources)

    def __sub__(self, other):

        assert isinstance(other, EstimatedQuantity)
        
        for source in other.sources:
            if source in self.sources:
                raise DependantValuesException()

        self_ufloat, self_unit = EstimatedQuantity._get_uncertainty_object(self)
        other_ufloat, other_unit = EstimatedQuantity._get_uncertainty_object(other)

        assert self_unit == other_unit

        result_ufloat = self_ufloat - other_ufloat

        result_value = result_ufloat.nominal_value * self_unit
        result_uncertainty = result_ufloat.std_dev * self_unit

        result_sources = []
        result_sources.extend(self.sources)
        result_sources.extend(other.sources)

        return EstimatedQuantity(result_value, result_uncertainty, *result_sources)

    def __mul__(self, other):

        # We only support multiplication by a scalar here.
        assert np.issubdtype(type(other), float) or np.issubdtype(type(other), int)

        self_ufloat, self_unit = EstimatedQuantity._get_uncertainty_object(self)

        result_ufloat = self_ufloat * other

        result_value = result_ufloat.nominal_value * self_unit
        result_uncertainty = result_ufloat.std_dev * self_unit

        result_sources = []
        result_sources.extend(self.sources)

        return EstimatedQuantity(result_value, result_uncertainty, *result_sources)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __truediv__(self, other):

        self_ufloat, self_unit = EstimatedQuantity._get_uncertainty_object(self)

        result_ufloat = self_ufloat / other

        result_value = result_ufloat.nominal_value * self_unit
        result_uncertainty = result_ufloat.std_dev * self_unit

        result_sources = []
        result_sources.extend(self.sources)

        return EstimatedQuantity(result_value, result_uncertainty, *result_sources)

    def __getstate__(self):

        return {
            'value': self.value,
            'uncertainty': self.uncertainty,
            'sources': self.sources
        }

    def __setstate__(self, state):

        self._value = state['value']
        self._uncertainty = state['uncertainty']
        self._sources = state['sources']

    @staticmethod
    def _get_uncertainty_object(estimated_quantity):
        """Converts a `EstimatedQuantity` object into an uncertainties
        `ufloat` representation.
        Parameters
        ----------
        estimated_quantity: EstimatedQuantity
            The quantity to create the uncertainties object for.
        Returns
        -------
        ufloat
            The ufloat representation of the estimated quantity object.
        unit.Unit
            The unit of the values encoded in the ufloat object.
        """

        value_in_default_unit_system = estimated_quantity.value.in_unit_system(unit.md_unit_system)
        uncertainty_in_default_unit_system = estimated_quantity.uncertainty.in_unit_system(unit.md_unit_system)

        value_unit = value_in_default_unit_system.unit
        unitless_value = estimated_quantity.value.value_in_unit(value_unit)

        uncertainty_unit = uncertainty_in_default_unit_system.unit
        unitless_uncertainty = estimated_quantity.uncertainty.value_in_unit(uncertainty_unit)

        assert value_unit == uncertainty_unit

        return ufloat(unitless_value, unitless_uncertainty), value_unit

    def __str__(self):

        return f'{self.value} +/- {self.uncertainty} ({", ".join(self.sources)})'

    def __repr__(self):

        return f'<EstimatedQuantity value={self.value} ' \
            f'uncertainty={self.uncertainty} sources=[{", ".join(self.sources)}]>'


class DependantValuesException(ValueError):
    """An exception which is raised when arithmetic operations are applied
    to two quantities which are not independent."""

    def __init__(self):

        super().__init__('The two quantities came from the same source, and so'
                         'cannot be treated as independent. The propagation /'
                         'calculation of uncertainties must be handled by'
                         'more sophisticated methods than this class employs.')
