"""
An API for defining, storing, and loading collections of physical property data.
"""
from collections import defaultdict

import pandas
from simtk.openmm.app import element

from propertyestimator import unit
from propertyestimator.properties import MeasurementSource, CalculationSource
from propertyestimator.substances import Substance
from propertyestimator.utils import create_molecule_from_smiles
from propertyestimator.utils.serialization import TypedBaseModel


class PhysicalPropertyDataSet(TypedBaseModel):
    """
    An object for storing and curating data sets of both physical property
    measurements and estimated. This class defines a number of convenience
    functions for filtering out unwanted properties, and for generating
    general statistics (such as the number of properties per substance)
    about the set.
    """

    def __init__(self):
        """
        Constructs a new PhysicalPropertyDataSet object.
        """
        self._properties = {}
        self._sources = []

    @property
    def properties(self):
        """
        dict of str and list of PhysicalProperty: A list of all of the properties
        within this set, partitioned by substance identifier.

        TODO: Add a link to Substance.identifier when have access to sphinx docs.
        TODO: Investigate why PhysicalProperty is not cross-linking.

        See Also
        --------
        Substance.identifier()
        """
        return self._properties

    @property
    def sources(self):
        """list of Source: The list of sources from which the properties were gathered"""
        return self._sources

    @property
    def number_of_properties(self):
        """int: The number of properties in the data set."""
        return sum([len(properties) for properties in self._properties.values()])

    def merge(self, data_set):
        """Merge another data set into the current one.

        Parameters
        ----------
        data_set : PhysicalPropertyDataSet
            The secondary data set to merge into this one.
        """
        if data_set is None:
            return

        # TODO: Do we need to check whether merging the same data set here?
        for substance_hash in data_set.properties:

            if substance_hash not in self._properties:
                self._properties[substance_hash] = []

            self._properties[substance_hash].extend(
                data_set.properties[substance_hash])

        self._sources.extend(data_set.sources)

    def filter_by_function(self, filter_function):
        """Filter the data set using a given filter function.

        Parameters
        ----------
        filter_function : lambda
            The filter function.
        """

        filtered_properties = {}

        # This works for now - if we wish to be able to undo a filter then
        # a 'filtered' list needs to be maintained separately to the main list.
        for substance_id in self._properties:

            substance_properties = list(filter(
                filter_function, self._properties[substance_id]))

            if len(substance_properties) <= 0:
                continue

            filtered_properties[substance_id] = substance_properties

        self._properties = {}

        for substance_id in filtered_properties:
            self._properties[substance_id] = filtered_properties[substance_id]

    def filter_by_property_types(self, *property_type):
        """Filter the data set based on the type of property (e.g Density).

        Parameters
        ----------
        property_type : PropertyType or str
            The type of property which should be retained.

        Examples
        --------
        Filter the dataset to only contain densities and static dielectric constants

        >>> # Load in the data set of properties which will be used for comparisons
        >>> from propertyestimator.datasets import ThermoMLDataSet
        >>> data_set = ThermoMLDataSet.from_doi('10.1016/j.jct.2016.10.001')
        >>>
        >>> # Filter the dataset to only include densities and dielectric constants.
        >>> from propertyestimator.properties import Density, DielectricConstant
        >>> data_set.filter_by_property_types(Density, DielectricConstant)

        or

        >>> data_set.filter_by_property_types('Density', 'DielectricConstant')
        """
        property_types = []

        for type_to_retain in property_type:

            if isinstance(type_to_retain, str):
                property_types.append(type_to_retain)
            else:
                property_types.append(type_to_retain.__name__)

        def filter_function(x):
            return type(x).__name__ in property_types

        self.filter_by_function(filter_function)

    def filter_by_phases(self, phases):
        """Filter the data set based on the phase of the property (e.g liquid).

        Parameters
        ----------
        phases : PropertyPhase
            The phase of property which should be retained.

        Examples
        --------
        Filter the dataset to only include liquid properties.

        >>> # Load in the data set of properties which will be used for comparisons
        >>> from propertyestimator.datasets import ThermoMLDataSet
        >>> data_set = ThermoMLDataSet.from_doi('10.1016/j.jct.2016.10.001')
        >>>
        >>> from propertyestimator.properties import PropertyPhase
        >>> data_set.filter_by_temperature(PropertyPhase.Liquid)
        """
        def filter_function(x):
            return x.phase & phases

        self.filter_by_function(filter_function)

    def filter_by_temperature(self, min_temperature, max_temperature):
        """Filter the data set based on a minimum and maximum temperature.

        Parameters
        ----------
        min_temperature : unit.Quantity
            The minimum temperature.
        max_temperature : unit.Quantity
            The maximum temperature.

        Examples
        --------
        Filter the dataset to only include properties measured between 130-260 K.

        >>> # Load in the data set of properties which will be used for comparisons
        >>> from propertyestimator.datasets import ThermoMLDataSet
        >>> data_set = ThermoMLDataSet.from_doi('10.1016/j.jct.2016.10.001')
        >>>
        >>> from propertyestimator import unit
        >>> data_set.filter_by_temperature(min_temperature=130*unit.kelvin, max_temperature=260*unit.kelvin)
        """

        def filter_function(x):
            return min_temperature <= x.thermodynamic_state.temperature <= max_temperature

        self.filter_by_function(filter_function)

    def filter_by_pressure(self, min_pressure, max_pressure):
        """Filter the data set based on a minimum and maximum pressure.

        Parameters
        ----------
        min_pressure : unit.Quantity
            The minimum pressure.
        max_pressure : unit.Quantity
            The maximum pressure.

        Examples
        --------
        Filter the dataset to only include properties measured between 70-150 kPa.

        >>> # Load in the data set of properties which will be used for comparisons
        >>> from propertyestimator.datasets import ThermoMLDataSet
        >>> data_set = ThermoMLDataSet.from_doi('10.1016/j.jct.2016.10.001')
        >>>
        >>> from propertyestimator import unit
        >>> data_set.filter_by_temperature(min_pressure=70*unit.kilopascal, max_temperature=150*unit.kilopascal)
        """
        def filter_function(x):

            if x.thermodynamic_state.pressure is None:
                return True

            return min_pressure <= x.thermodynamic_state.pressure <= max_pressure

        self.filter_by_function(filter_function)

    def filter_by_components(self, number_of_components):
        """Filter the data set based on a minimum and maximum temperature.

        Parameters
        ----------
        number_of_components : int
            The allowed number of components in the mixture.

        Examples
        --------
        Filter the dataset to only include pure substance properties.

        >>> # Load in the data set of properties which will be used for comparisons
        >>> from propertyestimator.datasets import ThermoMLDataSet
        >>> data_set = ThermoMLDataSet.from_doi('10.1016/j.jct.2016.10.001')
        >>>
        >>> data_set.filter_by_components(number_of_components=1)
        """
        def filter_function(x):
            return x.substance.number_of_components == number_of_components

        self.filter_by_function(filter_function)

    def filter_by_elements(self, *allowed_elements):
        """Filters out those properties which were estimated for
         compounds which contain elements outside of those defined
         in `allowed_elements`.

        Parameters
        ----------
        allowed_elements: str
            The symbols (e.g. C, H, Cl) of the elements to
            retain.
        """

        def filter_function(physical_property):

            substance = physical_property.substance

            for component in substance.components:

                oe_molecule = create_molecule_from_smiles(component.smiles, 0)

                for atom in oe_molecule.GetAtoms():

                    atomic_number = atom.GetAtomicNum()
                    atomic_element = element.Element.getByAtomicNumber(atomic_number).symbol

                    if atomic_element in allowed_elements:
                        continue

                    return False

            return True

        self.filter_by_function(filter_function)

    def filter_by_smiles(self, *allowed_smiles):
        """Filters out those properties which were estimated for
         compounds which do not appear in the allowed `smiles` list.

        Parameters
        ----------
        allowed_smiles: str
            The smiles identifiers of the compounds to keep
            after filtering.
        """

        def filter_function(physical_property):

            substance = physical_property.substance

            for component in substance.components:

                if component.smiles in allowed_smiles:
                    continue

                return False

            return True

        self.filter_by_function(filter_function)

    def to_pandas(self):
        """Converts a `PhysicalPropertyDataSet` to a `pandas.DataFrame` object
        with columns of

            - 'Temperature'
            - 'Pressure'
            - 'Phase'
            - 'Number Of Components'
            - 'Component 1'
            - 'Mole Fraction 1'
            - ...
            - 'Component N'
            - 'Mole Fraction N'
            - '<Property 1> Value'
            - '<Property 1> Uncertainty'
            - ...
            - '<Property N> Value'
            - '<Property N> Uncertainty'
            - `'Source'`

        where 'Component X' is a column containing the smiles representation of component X.

        Returns
        -------
        pandas.DataFrame
            The create data frame.
        """
        # Determine the maximum number of components for any
        # given measurements.
        maximum_number_of_components = 0
        all_property_types = set()

        for substance_id in self._properties:

            if len(self._properties[substance_id]) == 0:
                continue

            substance = self._properties[substance_id][0].substance
            maximum_number_of_components = max(maximum_number_of_components, substance.number_of_components)

            for physical_property in self._properties[substance_id]:
                all_property_types.add(type(physical_property))

        # Make sure the maximum number of components is not zero.
        if maximum_number_of_components <= 0 and len(self._properties) > 0:

            raise ValueError('The data set did not contain any substances with '
                             'one or more components.')

        data_rows = []

        # Extract the data from the data set.
        for substance_id in self._properties:

            data_points_by_state = defaultdict(dict)

            for physical_property in self._properties[substance_id]:

                all_property_types.add(type(physical_property))

                # Extract the measured state.
                temperature = physical_property.thermodynamic_state.temperature.to(unit.kelvin)
                pressure = None

                if physical_property.thermodynamic_state.pressure is not None:
                    pressure = physical_property.thermodynamic_state.pressure.to(unit.kilopascal)

                phase = physical_property.phase

                # Extract the component data.
                number_of_components = physical_property.substance.number_of_components

                components = [] * maximum_number_of_components

                for index, component in enumerate(physical_property.substance.components):

                    amount = next(iter(physical_property.substance.get_amounts(component)))
                    assert isinstance(amount, Substance.MoleFraction)

                    components.append((component.smiles, amount.value))

                # Extract the value data as a string.
                value = None if physical_property.value is None else str(physical_property.value)
                uncertainty = None if physical_property.uncertainty is None else str(physical_property.uncertainty)

                # Extract the data source.
                source = None

                if isinstance(physical_property.source, MeasurementSource):

                    source = physical_property.source.reference

                    if source is None:
                        source = physical_property.source.doi

                elif isinstance(physical_property.source, CalculationSource):
                    source = physical_property.source.fidelity

                # Create the data row.
                data_row = {
                    'Temperature': str(temperature),
                    'Pressure': str(pressure),
                    'Phase': phase,
                    'Number Of Components': number_of_components
                }

                for index in range(len(components)):

                    data_row[f'Component {index + 1}'] = components[index][0]
                    data_row[f'Mole Fraction {index + 1}'] = components[index][1]

                data_row[f'{type(physical_property).__name__} Value'] = value
                data_row[f'{type(physical_property).__name__} Uncertainty'] = uncertainty

                data_row['Source'] = source

                data_points_by_state[physical_property.thermodynamic_state].update(data_row)

            for state in data_points_by_state:
                data_rows.append(data_points_by_state[state])

        # Set up the column headers.
        if len(data_rows) == 0:
            return None

        data_columns = [
            'Temperature',
            'Pressure',
            'Phase',
            'Number Of Components',
        ]

        for index in range(maximum_number_of_components):
            data_columns.append(f'Component {index + 1}')
            data_columns.append(f'Mole Fraction {index + 1}')

        for property_type in all_property_types:
            data_columns.append(f'{property_type.__name__} Value')
            data_columns.append(f'{property_type.__name__} Uncertainty')

        data_frame = pandas.DataFrame(data_rows, columns=data_columns)
        return data_frame

    def __getstate__(self):

        return {
            'properties': self._properties,
            'sources': self._sources
        }

    def __setstate__(self, state):

        self._properties = state['properties']
        self._sources = state['sources']
