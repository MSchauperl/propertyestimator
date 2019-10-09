"""
A collection of density physical property definitions.
"""
from propertyestimator import unit
from propertyestimator.datasets.plugins import register_thermoml_property
from propertyestimator.properties import PhysicalProperty, PropertyPhase
from propertyestimator.properties.plugins import register_estimable_property
from propertyestimator.protocols import analysis, reweighting, miscellaneous
from propertyestimator.protocols.utils import generate_base_simulation_protocols, generate_base_reweighting_protocols, \
    generate_gradient_protocol_group
from propertyestimator.storage import StoredSimulationData
from propertyestimator.utils.quantities import EstimatedQuantity
from propertyestimator.utils.statistics import ObservableType
from propertyestimator.workflow import WorkflowOptions
from propertyestimator.workflow.schemas import WorkflowSchema, ProtocolReplicator
from propertyestimator.workflow.utils import ProtocolPath, ReplicatorValue


@register_estimable_property()
@register_thermoml_property(thermoml_string='Mass density, kg/m3', supported_phases=PropertyPhase.Liquid)
class Density(PhysicalProperty):
    """A class representation of a density property"""

    @property
    def multi_component_property(self):
        return False

    @property
    def required_data_class(self):
        return StoredSimulationData

    @staticmethod
    def get_default_workflow_schema(calculation_layer, options=None):

        if calculation_layer == 'SimulationLayer':
            return Density.get_default_simulation_workflow_schema(options)
        elif calculation_layer == 'ReweightingLayer':
            return Density.get_default_reweighting_workflow_schema(options)

        return None

    @staticmethod
    def get_default_simulation_workflow_schema(options=None):
        """Returns the default workflow to use when estimating this property
        from direct simulations.

        Parameters
        ----------
        options: WorkflowOptions
            The default options to use when setting up the estimation workflow.

        Returns
        -------
        WorkflowSchema
            The schema to follow when estimating this property.
        """

        # Define the protocol which will extract the average density from
        # the results of a simulation.
        extract_density = analysis.ExtractAverageStatistic('extract_density')
        extract_density.statistics_type = ObservableType.Density

        # Define the protocols which will run the simulation itself.
        protocols, value_source, output_to_store = generate_base_simulation_protocols(extract_density,
                                                                                      options)

        # Set up the gradient calculations
        reweight_density_template = reweighting.ReweightStatistics('')
        reweight_density_template.statistics_type = ObservableType.Density
        reweight_density_template.statistics_paths = [ProtocolPath('statistics_file_path',
                                                                   protocols.converge_uncertainty.id,
                                                                   protocols.production_simulation.id)]

        coordinate_source = ProtocolPath('output_coordinate_file', protocols.equilibration_simulation.id)
        trajectory_source = ProtocolPath('trajectory_file_path', protocols.converge_uncertainty.id,
                                         protocols.production_simulation.id)
        statistics_source = ProtocolPath('statistics_file_path', protocols.converge_uncertainty.id,
                                         protocols.production_simulation.id)

        gradient_group, gradient_replicator, gradient_source = \
            generate_gradient_protocol_group(reweight_density_template,
                                             [ProtocolPath('force_field_path', 'global')],
                                             ProtocolPath('force_field_path', 'global'),
                                             coordinate_source,
                                             trajectory_source,
                                             statistics_source)

        # Build the workflow schema.
        schema = WorkflowSchema(property_type=Density.__name__)
        schema.id = '{}{}'.format(Density.__name__, 'Schema')

        schema.protocols = {
            protocols.build_coordinates.id: protocols.build_coordinates.schema,
            protocols.assign_parameters.id: protocols.assign_parameters.schema,
            protocols.energy_minimisation.id: protocols.energy_minimisation.schema,
            protocols.equilibration_simulation.id: protocols.equilibration_simulation.schema,
            protocols.converge_uncertainty.id: protocols.converge_uncertainty.schema,
            protocols.extract_uncorrelated_trajectory.id: protocols.extract_uncorrelated_trajectory.schema,
            protocols.extract_uncorrelated_statistics.id: protocols.extract_uncorrelated_statistics.schema,
            gradient_group.id: gradient_group.schema
        }

        schema.replicators = [gradient_replicator]

        schema.outputs_to_store = {'full_system': output_to_store}

        schema.gradients_sources = [gradient_source]
        schema.final_value_source = value_source

        return schema

    @staticmethod
    def get_default_reweighting_workflow_schema(options):
        """Returns the default workflow to use when estimating this property
        by reweighting existing data.

        Parameters
        ----------
        options: WorkflowOptions
            The default options to use when setting up the estimation workflow.

        Returns
        -------
        WorkflowSchema
            The schema to follow when estimating this property.
        """

        data_replicator_id = 'data_replicator'

        # The protocol which will be used to calculate the densities from
        # the existing data.
        density_calculation = analysis.ExtractAverageStatistic(f'calc_density_$({data_replicator_id})')
        density_calculation.statistics_type = ObservableType.Density

        reweight_density = reweighting.ReweightStatistics(f'reweight_density')
        reweight_density.statistics_type = ObservableType.Density

        reweighting_protocols, data_replicator = generate_base_reweighting_protocols(density_calculation,
                                                                                     reweight_density,
                                                                                     options,
                                                                                     data_replicator_id)

        # Set up the gradient calculations
        coordinate_path = ProtocolPath('output_coordinate_path', reweighting_protocols.concatenate_trajectories.id)
        trajectory_path = ProtocolPath('output_trajectory_path', reweighting_protocols.concatenate_trajectories.id)

        reweight_density_template = reweighting.ReweightStatistics('')
        reweight_density_template.statistics_type = ObservableType.Density
        reweight_density_template.statistics_paths = ProtocolPath('output_statistics_path',
                                                                  reweighting_protocols.decorrelate_statistics.id)

        gradient_group, gradient_replicator, gradient_source = \
            generate_gradient_protocol_group(reweight_density_template,
                                             ProtocolPath('force_field_path',
                                                          reweighting_protocols.unpack_stored_data.id),
                                             ProtocolPath('force_field_path', 'global'),
                                             coordinate_path,
                                             trajectory_path,
                                             replicator_id='grad',
                                             use_subset_of_force_field=False,
                                             effective_sample_indices=ProtocolPath('effective_sample_indices',
                                                                                   reweighting_protocols.
                                                                                   mbar_protocol.id))

        schema = WorkflowSchema(property_type=Density.__name__)
        schema.id = '{}{}'.format(Density.__name__, 'Schema')

        schema.protocols = {protocol.id: protocol.schema for protocol in reweighting_protocols}
        schema.protocols[gradient_group.id] = gradient_group.schema

        schema.replicators = [data_replicator, gradient_replicator]

        schema.gradients_sources = [gradient_source]
        schema.final_value_source = ProtocolPath('value', reweighting_protocols.mbar_protocol.id)

        return schema


@register_estimable_property()
@register_thermoml_property('Excess molar volume, m3/mol', supported_phases=PropertyPhase.Liquid)
class ExcessMolarVolume(PhysicalProperty):
    """A class representation of an excess molar volume property"""

    @property
    def multi_component_property(self):
        return True

    @property
    def required_data_class(self):
        return StoredSimulationData

    @staticmethod
    def get_default_workflow_schema(calculation_layer, options=None):

        if calculation_layer == 'SimulationLayer':
            return ExcessMolarVolume.get_default_simulation_workflow_schema(options)
        elif calculation_layer == 'ReweightingLayer':
            return ExcessMolarVolume.get_default_reweighting_workflow_schema(options)

        return None

    @staticmethod
    def _get_simulation_protocols(id_suffix, gradient_replicator_id, replicator_id=None, weight_by_mole_fraction=False,
                                  component_substance_reference=None, full_substance_reference=None, options=None):

        """Returns the set of protocols which when combined in a workflow
        will yield the molar volume of a substance.

        Parameters
        ----------
        id_suffix: str
            A suffix to append to the id of each of the returned protocols.
        gradient_replicator_id: str
            The id of the replicator which will clone those protocols which will
            estimate the gradient of the molar volume with respect to a given parameter.
        replicator_id: str, optional
            The id of the replicator which will be used to clone these protocols.
            This will be appended to the id of each of the returned protocols if
            set.
        weight_by_mole_fraction: bool
            If true, an extra protocol will be added to weight the calculated
            molar volume by the mole fraction of the component.
        component_substance_reference: ProtocolPath or PlaceholderInput, optional
            An optional protocol path (or replicator reference) to the component substance
            whose enthalpy is being estimated.
        full_substance_reference: ProtocolPath or PlaceholderInput, optional
            An optional protocol path (or replicator reference) to the full substance
            whose enthalpy of mixing is being estimated. This cannot be `None` if
            `weight_by_mole_fraction` is `True`.
        options: WorkflowOptions
            The options to use when setting up the workflows.

        Returns
        -------
        BaseSimulationProtocols
            The protocols used to estimate the molar volume of a substance.
        DivideValue
            The protocol used to calculate the number of molar molecules in
            the system.
        ProtocolPath
            A reference to the estimated molar volume.
        WorkflowSimulationDataToStore
            An object which describes the default data from a simulation to store,
            such as the uncorrelated statistics and configurations.
        ProtocolGroup
            The group of protocols which will calculate the gradient of the reduced potential
            with respect to a given property.
        ProtocolReplicator
            The protocol which will replicate the gradient group for every gradient to
            estimate.
        ProtocolPath
            A reference to the value of the gradient.
        """

        if replicator_id is not None:
            id_suffix = f'{id_suffix}_$({replicator_id})'

        if component_substance_reference is None:
            component_substance_reference = ProtocolPath('substance', 'global')

        if weight_by_mole_fraction is True and full_substance_reference is None:

            raise ValueError('The full substance reference must be set when weighting by'
                             'the mole fraction')

        # Define the protocol which will extract the average molar volume from
        # the results of a simulation.
        extract_volume = analysis.ExtractAverageStatistic(f'extract_volume{id_suffix}')
        extract_volume.statistics_type = ObservableType.Volume

        # Define the protocols which will run the simulation itself.
        simulation_protocols, value_source, output_to_store = generate_base_simulation_protocols(extract_volume,
                                                                                                 options,
                                                                                                 id_suffix)

        # Divide the volume by the number of molecules in the system
        number_of_molecules = ProtocolPath('output_number_of_molecules', simulation_protocols.build_coordinates.id)
        built_substance = ProtocolPath('output_substance', simulation_protocols.build_coordinates.id)

        number_of_molar_molecules = miscellaneous.DivideValue(f'number_of_molar_molecules{id_suffix}')
        number_of_molar_molecules.value = number_of_molecules
        number_of_molar_molecules.divisor = (1.0 * unit.avogadro_number).to('mole**-1')

        extract_volume.divisor = ProtocolPath('result', number_of_molar_molecules.id)

        # Use the correct substance.
        simulation_protocols.build_coordinates.substance = component_substance_reference
        simulation_protocols.assign_parameters.substance = built_substance
        output_to_store.substance = built_substance

        conditional_group = simulation_protocols.converge_uncertainty

        if weight_by_mole_fraction:
            # The component workflows need an extra step to multiply their molar volumes by their
            # relative mole fraction.
            weight_by_mole_fraction = miscellaneous.WeightByMoleFraction(f'weight_by_mole_fraction{id_suffix}')
            weight_by_mole_fraction.value = ProtocolPath('value', extract_volume.id)
            weight_by_mole_fraction.full_substance = full_substance_reference
            weight_by_mole_fraction.component = component_substance_reference

            conditional_group.add_protocols(weight_by_mole_fraction)

            value_source = ProtocolPath('weighted_value', conditional_group.id, weight_by_mole_fraction.id)

        if options.convergence_mode != WorkflowOptions.ConvergenceMode.NoChecks:

            # Make sure the convergence criteria is set to use the per component
            # uncertainty target.
            conditional_group.conditions[0].right_hand_value = ProtocolPath('per_component_uncertainty', 'global')

            if weight_by_mole_fraction:
                # Make sure the weighted uncertainty is being used in the conditional comparison.
                conditional_group.conditions[0].left_hand_value = ProtocolPath('weighted_value.uncertainty',
                                                                               conditional_group.id,
                                                                               weight_by_mole_fraction.id)

        # Set up the gradient calculations
        reweight_molar_volume_template = reweighting.ReweightStatistics('')
        reweight_molar_volume_template.statistics_type = ObservableType.Volume
        reweight_molar_volume_template.statistics_paths = [ProtocolPath('statistics_file_path',
                                                                        conditional_group.id,
                                                                        simulation_protocols.production_simulation.id)]

        coordinate_source = ProtocolPath('output_coordinate_file', simulation_protocols.equilibration_simulation.id)
        trajectory_source = ProtocolPath('trajectory_file_path', simulation_protocols.converge_uncertainty.id,
                                         simulation_protocols.production_simulation.id)
        statistics_source = ProtocolPath('statistics_file_path', simulation_protocols.converge_uncertainty.id,
                                         simulation_protocols.production_simulation.id)

        gradient_group, gradient_replicator, gradient_source = \
            generate_gradient_protocol_group(reweight_molar_volume_template,
                                             [ProtocolPath('force_field_path', 'global')],
                                             ProtocolPath('force_field_path', 'global'),
                                             coordinate_source,
                                             trajectory_source,
                                             statistics_source,
                                             replicator_id=gradient_replicator_id,
                                             substance_source=built_substance,
                                             id_suffix=id_suffix)

        # Remove the group id from the path.
        gradient_source.pop_next_in_path()

        if weight_by_mole_fraction:
            # The component workflows need an extra step to multiply their gradients by their
            # relative mole fraction.
            weight_gradient = miscellaneous.WeightByMoleFraction(f'weight_gradient_by_mole_fraction{id_suffix}')
            weight_gradient.value = gradient_source
            weight_gradient.full_substance = full_substance_reference
            weight_gradient.component = component_substance_reference

            gradient_group.add_protocols(weight_gradient)
            gradient_source = ProtocolPath('weighted_value', weight_gradient.id)

        scale_gradient = miscellaneous.DivideValue(f'scale_gradient{id_suffix}')
        scale_gradient.value = gradient_source
        scale_gradient.divisor = ProtocolPath('result', number_of_molar_molecules.id)

        gradient_group.add_protocols(scale_gradient)
        gradient_source = ProtocolPath('result', gradient_group.id, scale_gradient.id)

        return (simulation_protocols, number_of_molar_molecules, value_source, output_to_store,
                gradient_group, gradient_replicator, gradient_source)

    @staticmethod
    def _get_reweighting_protocols(id_suffix, gradient_replicator_id, data_replicator_id, replicator_id=None,
                                   weight_by_mole_fraction=False, substance_reference=None, options=None):

        """Returns the set of protocols which when combined in a workflow
        will yield the molar volume of a substance by reweighting cached data.

        Parameters
        ----------
        id_suffix: str
            A suffix to append to the id of each of the returned protocols.
        gradient_replicator_id: str
            The id of the replicator which will clone those protocols which will
            estimate the gradient of the molar volume with respect to a given parameter.
        data_replicator_id: str
            The id of the replicator which will be used to clone these protocols
            for each cached simulation data.
        replicator_id: str, optional
            The optional id of the replicator which will be used to clone these
            protocols, e.g. for each component in the system.
        weight_by_mole_fraction: bool
            If true, an extra protocol will be added to weight the calculated
            molar volume by the mole fraction of the component.
        substance_reference: ProtocolPath or PlaceholderInput, optional
            An optional protocol path (or replicator reference) to the substance
            whose molar volume is being estimated.
        options: WorkflowOptions
            The options to use when setting up the workflows.

        Returns
        -------
        BaseReweightingProtocols
            The protocols used to estimate the molar volume of a substance.
        ProtocolPath
            A reference to the estimated molar volume.
        ProtocolReplicator
            The replicator which will replicate each protocol for each
            cached simulation datum.
        ProtocolGroup
            The group of protocols which will calculate the gradient of the reduced potential
            with respect to a given property.
        ProtocolPath
            A reference to the value of the gradient.
        """

        if replicator_id is not None:
            id_suffix = f'{id_suffix}_$({replicator_id})'

        full_id_suffix = id_suffix

        if data_replicator_id is not None:
            full_id_suffix = f'{id_suffix}_$({data_replicator_id})'

        if substance_reference is None:
            substance_reference = ProtocolPath('substance', 'global')

        extract_volume = analysis.ExtractAverageStatistic(f'extract_volume{full_id_suffix}')
        extract_volume.statistics_type = ObservableType.Volume
        reweight_volume = reweighting.ReweightStatistics(f'reweight_volume{id_suffix}')
        reweight_volume.statistics_type = ObservableType.Volume

        (protocols,
         data_replicator) = generate_base_reweighting_protocols(analysis_protocol=extract_volume,
                                                                mbar_protocol=reweight_volume,
                                                                workflow_options=options,
                                                                replicator_id=data_replicator_id,
                                                                id_suffix=id_suffix)

        # Make sure to use the correct substance.
        protocols.build_target_system.substance = substance_reference

        value_source = ProtocolPath('value', protocols.mbar_protocol.id)

        # Set up the protocols which will be responsible for adding together
        # the component molar volumes, and subtracting these from the full system volume.
        weight_volume = None

        if weight_by_mole_fraction is True:
            weight_volume = miscellaneous.WeightByMoleFraction(f'weight_volume{id_suffix}')
            weight_volume.value = ProtocolPath('value', protocols.mbar_protocol.id)
            weight_volume.full_substance = ProtocolPath('substance', 'global')
            weight_volume.component = substance_reference

            value_source = ProtocolPath('weighted_value', weight_volume.id)

        # Divide by the component molar volumes by the number of molecules in the system
        number_of_molecules = ProtocolPath('total_number_of_molecules', protocols.
                                           unpack_stored_data.id.replace(f'$({data_replicator_id})', '0'))

        number_of_molar_molecules = miscellaneous.MultiplyValue(f'number_of_molar_molecules{id_suffix}')
        number_of_molar_molecules.value = EstimatedQuantity((1.0 / unit.avogadro_number).to(unit.mole),
                                                            (0.0 / unit.avogadro_number).to(unit.mole), '')
        number_of_molar_molecules.multiplier = number_of_molecules

        divide_by_molecules = miscellaneous.DivideValue(f'divide_by_molecules{id_suffix}')
        divide_by_molecules.value = value_source
        divide_by_molecules.divisor = ProtocolPath('result.value', number_of_molar_molecules.id)

        value_source = ProtocolPath('result', divide_by_molecules.id)

        # Set up the gradient calculations.
        reweight_volume_template = reweighting.ReweightStatistics('')
        reweight_volume_template.statistics_type = ObservableType.Volume
        reweight_volume_template.statistics_paths = ProtocolPath('output_statistics_path',
                                                                 protocols.decorrelate_statistics.id)

        coordinate_path = ProtocolPath('output_coordinate_path', protocols.concatenate_trajectories.id)
        trajectory_path = ProtocolPath('output_trajectory_path', protocols.concatenate_trajectories.id)

        gradient_group, _, gradient_source = \
            generate_gradient_protocol_group(reweight_volume_template,
                                             ProtocolPath('force_field_path', protocols.unpack_stored_data.id),
                                             ProtocolPath('force_field_path', 'global'),
                                             coordinate_path,
                                             trajectory_path,
                                             replicator_id=gradient_replicator_id,
                                             id_suffix=id_suffix,
                                             substance_source=substance_reference,
                                             use_subset_of_force_field=False,
                                             effective_sample_indices=ProtocolPath('effective_sample_indices',
                                                                                   protocols.mbar_protocol.id))

        # Remove the group id from the path.
        gradient_source.pop_next_in_path()

        if weight_by_mole_fraction is True:
            # The component workflows need an extra step to multiply their gradients by their
            # relative mole fraction.
            weight_gradient = miscellaneous.WeightByMoleFraction(f'weight_gradient_$({gradient_replicator_id})_'
                                                                 f'by_mole_fraction{id_suffix}')
            weight_gradient.value = gradient_source
            weight_gradient.full_substance = ProtocolPath('substance', 'global')
            weight_gradient.component = substance_reference

            gradient_group.add_protocols(weight_gradient)
            gradient_source = ProtocolPath('weighted_value', weight_gradient.id)

        scale_gradient = miscellaneous.DivideValue(f'scale_gradient_$({gradient_replicator_id}){id_suffix}')
        scale_gradient.value = gradient_source
        scale_gradient.divisor = ProtocolPath('result.value', number_of_molar_molecules.id)

        gradient_group.add_protocols(scale_gradient)
        gradient_source = ProtocolPath('result', gradient_group.id, scale_gradient.id)

        all_protocols = (*protocols, number_of_molar_molecules, divide_by_molecules)

        if weight_volume is not None:
            all_protocols = (*all_protocols, weight_volume)

        return all_protocols, value_source, data_replicator, gradient_group, gradient_source

    @staticmethod
    def get_default_simulation_workflow_schema(options=None):
        """Returns the default workflow to use when estimating this property
        from direct simulations.

        Parameters
        ----------
        options: WorkflowOptions
            The default options to use when setting up the estimation workflow.

        Returns
        -------
        WorkflowSchema
            The schema to follow when estimating this property.
        """

        # Define the id of the replicator which will clone the gradient protocols
        # for each gradient key to be estimated.
        gradient_replicator_id = 'gradient_replicator'

        # Set up a workflow to calculate the molar volume of the full, mixed system.
        (full_system_protocols,
         full_system_molar_molecules,
         full_system_volume,
         full_output,
         full_system_gradient_group,
         full_system_gradient_replicator,
         full_system_gradient) = ExcessMolarVolume._get_simulation_protocols('_full',
                                                                             gradient_replicator_id,
                                                                             options=options)

        # Set up a general workflow for calculating the molar volume of one of the system components.
        component_replicator_id = 'component_replicator'
        component_substance = ReplicatorValue(component_replicator_id)

        # Make sure to weight by the mole fractions of the actual full system as these may be slightly
        # different to the mole fractions of the measure property due to rounding.
        full_substance = ProtocolPath('output_substance', full_system_protocols.build_coordinates.id)

        (component_protocols,
         component_molar_molecules,
         component_volumes,
         component_output,
         component_gradient_group,
         component_gradient_replicator,
         component_gradient) = ExcessMolarVolume._get_simulation_protocols('_component',
                                                                           gradient_replicator_id,
                                                                           replicator_id=component_replicator_id,
                                                                           weight_by_mole_fraction=True,
                                                                           component_substance_reference=
                                                                                   component_substance,
                                                                           full_substance_reference=full_substance,
                                                                           options=options)

        # Finally, set up the protocols which will be responsible for adding together
        # the component molar volumes, and subtracting these from the mixed system molar volume.
        add_component_molar_volumes = miscellaneous.AddValues('add_component_molar_volumes')
        add_component_molar_volumes.values = component_volumes

        calculate_excess_volume = miscellaneous.SubtractValues('calculate_excess_volume')
        calculate_excess_volume.value_b = full_system_volume
        calculate_excess_volume.value_a = ProtocolPath('result', add_component_molar_volumes.id)

        # Create the replicator object which defines how the pure component
        # molar volume estimation protocols will be replicated for each component.
        component_replicator = ProtocolReplicator(replicator_id=component_replicator_id)
        component_replicator.template_values = ProtocolPath('components', 'global')

        # Combine the gradients.
        add_component_gradients = miscellaneous.AddValues(f'add_component_gradients'
                                                          f'_$({gradient_replicator_id})')
        add_component_gradients.values = component_gradient

        combine_gradients = miscellaneous.SubtractValues(f'combine_gradients_$({gradient_replicator_id})')
        combine_gradients.value_b = full_system_gradient
        combine_gradients.value_a = ProtocolPath('result', add_component_gradients.id)

        # Combine the gradient replicators.
        gradient_replicator = ProtocolReplicator(replicator_id=gradient_replicator_id)
        gradient_replicator.template_values = ProtocolPath('parameter_gradient_keys', 'global')

        # Build the final workflow schema
        schema = WorkflowSchema(property_type=ExcessMolarVolume.__name__)
        schema.id = '{}{}'.format(ExcessMolarVolume.__name__, 'Schema')

        schema.protocols = {
            component_protocols.build_coordinates.id: component_protocols.build_coordinates.schema,
            component_protocols.assign_parameters.id: component_protocols.assign_parameters.schema,
            component_protocols.energy_minimisation.id: component_protocols.energy_minimisation.schema,
            component_protocols.equilibration_simulation.id: component_protocols.equilibration_simulation.schema,
            component_protocols.converge_uncertainty.id: component_protocols.converge_uncertainty.schema,
            component_molar_molecules.id: component_molar_molecules.schema,

            full_system_protocols.build_coordinates.id: full_system_protocols.build_coordinates.schema,
            full_system_protocols.assign_parameters.id: full_system_protocols.assign_parameters.schema,
            full_system_protocols.energy_minimisation.id: full_system_protocols.energy_minimisation.schema,
            full_system_protocols.equilibration_simulation.id: full_system_protocols.equilibration_simulation.schema,
            full_system_protocols.converge_uncertainty.id: full_system_protocols.converge_uncertainty.schema,
            full_system_molar_molecules.id: full_system_molar_molecules.schema,

            component_protocols.extract_uncorrelated_trajectory.id:
                component_protocols.extract_uncorrelated_trajectory.schema,
            component_protocols.extract_uncorrelated_statistics.id:
                component_protocols.extract_uncorrelated_statistics.schema,

            full_system_protocols.extract_uncorrelated_trajectory.id:
                full_system_protocols.extract_uncorrelated_trajectory.schema,
            full_system_protocols.extract_uncorrelated_statistics.id:
                full_system_protocols.extract_uncorrelated_statistics.schema,

            add_component_molar_volumes.id: add_component_molar_volumes.schema,
            calculate_excess_volume.id: calculate_excess_volume.schema,

            component_gradient_group.id: component_gradient_group.schema,
            full_system_gradient_group.id: full_system_gradient_group.schema,
            add_component_gradients.id: add_component_gradients.schema,
            combine_gradients.id: combine_gradients.schema
        }

        schema.replicators = [gradient_replicator, component_replicator]

        # Finally, tell the schemas where to look for its final values.
        schema.gradients_sources = [ProtocolPath('result', combine_gradients.id)]
        schema.final_value_source = ProtocolPath('result', calculate_excess_volume.id)

        schema.outputs_to_store = {
            'full_system': full_output,
            f'component_$({component_replicator_id})': component_output
        }

        return schema

    @staticmethod
    def get_default_reweighting_workflow_schema(options=None):
        """Returns the default workflow to use when estimating this property
        by reweighting existing data.

        Parameters
        ----------
        options: WorkflowOptions
            The default options to use when setting up the estimation workflow.

        Returns
        -------
        WorkflowSchema
            The schema to follow when estimating this property.
        """

        # Set up a replicator that will re-run the component reweighting workflow for each
        # component in the system.
        component_replicator = ProtocolReplicator(replicator_id='component_replicator')
        component_replicator.template_values = ProtocolPath('components', 'global')

        gradient_replicator = ProtocolReplicator('gradient')
        gradient_replicator.template_values = ProtocolPath('parameter_gradient_keys', 'global')

        # Set up the protocols which will reweight data for the full system.
        full_data_replicator_id = 'full_data_replicator'

        (full_protocols,
         full_volume,
         full_data_replicator,
         full_gradient_group,
         full_gradient_source) = ExcessMolarVolume._get_reweighting_protocols('_full',
                                                                              gradient_replicator.id,
                                                                              full_data_replicator_id,
                                                                              options=options)

        # Set up the protocols which will reweight data for each component.
        component_data_replicator_id = f'component_{component_replicator.placeholder_id}_data_replicator'

        (component_protocols,
         component_volumes,
         component_data_replicator,
         component_gradient_group,
         component_gradient_source) = ExcessMolarVolume._get_reweighting_protocols('_component',
                                                                                   gradient_replicator.id,
                                                                                   component_data_replicator_id,
                                                                                   replicator_id=component_replicator.id,
                                                                                   weight_by_mole_fraction=True,
                                                                                   substance_reference=ReplicatorValue(
                                                                                       component_replicator.id),
                                                                                   options=options)

        # Make sure the replicator is only replicating over component data.
        component_data_replicator.template_values = ProtocolPath(f'component_data[$({component_replicator.id})]',
                                                                 'global')

        add_component_molar_volumes = miscellaneous.AddValues('add_component_molar_volumes')
        add_component_molar_volumes.values = component_volumes

        calculate_excess_volume = miscellaneous.SubtractValues('calculate_excess_potential')
        calculate_excess_volume.value_b = full_volume
        calculate_excess_volume.value_a = ProtocolPath('result', add_component_molar_volumes.id)

        # Combine the gradients.
        add_component_gradients = miscellaneous.AddValues(f'add_component_gradients'
                                                          f'_{gradient_replicator.placeholder_id}')
        add_component_gradients.values = component_gradient_source

        combine_gradients = miscellaneous.SubtractValues(f'combine_gradients_{gradient_replicator.placeholder_id}')
        combine_gradients.value_b = full_gradient_source
        combine_gradients.value_a = ProtocolPath('result', add_component_gradients.id)

        # Build the final workflow schema.
        schema = WorkflowSchema(property_type=ExcessMolarVolume.__name__)
        schema.id = '{}{}'.format(ExcessMolarVolume.__name__, 'Schema')

        schema.protocols = dict()

        schema.protocols.update({protocol.id: protocol.schema for protocol in full_protocols})
        schema.protocols.update({protocol.id: protocol.schema for protocol in component_protocols})

        schema.protocols[add_component_molar_volumes.id] = add_component_molar_volumes.schema
        schema.protocols[calculate_excess_volume.id] = calculate_excess_volume.schema

        schema.protocols[full_gradient_group.id] = full_gradient_group.schema
        schema.protocols[component_gradient_group.id] = component_gradient_group.schema
        schema.protocols[add_component_gradients.id] = add_component_gradients.schema
        schema.protocols[combine_gradients.id] = combine_gradients.schema

        schema.replicators = [
            full_data_replicator,
            component_replicator,
            component_data_replicator,
            gradient_replicator
        ]

        schema.gradients_sources = [ProtocolPath('result', combine_gradients.id)]
        schema.final_value_source = ProtocolPath('result', calculate_excess_volume.id)

        return schema
