"""
The direct simulation estimation layer.
"""

import logging
from os import path

from propertyestimator.layers import register_calculation_layer, PropertyCalculationLayer
from propertyestimator.workflow import WorkflowGraph, Workflow


@register_calculation_layer()
class SimulationLayer(PropertyCalculationLayer):
    """A calculation layer which aims to calculate physical properties
    directly from molecular simulation.

    .. warning :: This class is experimental and should not be used in a production environment.
    """

    @staticmethod
    def _build_workflow_graph(working_directory, properties, force_field_path, parameter_gradient_keys, options):
        """ Construct a graph of the protocols needed to calculate a set of properties.

        Parameters
        ----------
        working_directory: str
            The local directory in which to store all local,
            temporary calculation data from this graph.
        properties : list of PhysicalProperty
            The properties to attempt to compute.
        force_field_path : str
            The path to the force field parameters to use in the workflow.
        parameter_gradient_keys: list of ParameterGradientKey
            A list of references to all of the parameters which all observables
            should be differentiated with respect to.
        options: PropertyEstimatorOptions
            The options to run the workflows with.
        """
        workflow_graph = WorkflowGraph(working_directory)

        for property_to_calculate in properties:

            property_type = type(property_to_calculate).__name__

            if property_type not in options.workflow_schemas:

                logging.warning('The property calculator does not support {} '
                                'workflows.'.format(property_type))

                continue

            if SimulationLayer.__name__ not in options.workflow_schemas[property_type]:
                continue

            schema = options.workflow_schemas[property_type][SimulationLayer.__name__]
            workflow_options = options.workflow_options[property_type].get(SimulationLayer.__name__)

            global_metadata = Workflow.generate_default_metadata(property_to_calculate,
                                                                 force_field_path,
                                                                 parameter_gradient_keys,
                                                                 workflow_options)

            workflow = Workflow(property_to_calculate, global_metadata)
            workflow.schema = schema

            from propertyestimator.properties import CalculationSource
            workflow.physical_property.source = CalculationSource(fidelity=SimulationLayer.__name__,
                                                                           provenance={})

            workflow_graph.add_workflow(workflow)

        return workflow_graph

    @staticmethod
    def schedule_calculation(calculation_backend, storage_backend, layer_directory,
                             data_model, callback, synchronous=False):

        # Store a temporary copy of the force field for protocols to easily access.
        force_field_source = storage_backend.retrieve_force_field(data_model.force_field_id)
        force_field_path = path.join(layer_directory, 'force_field_{}'.format(data_model.force_field_id))

        with open(force_field_path, 'w') as file:
            file.write(force_field_source.json())

        workflow_graph = SimulationLayer._build_workflow_graph(layer_directory,
                                                               data_model.queued_properties,
                                                               force_field_path,
                                                               data_model.parameter_gradient_keys,
                                                               data_model.options)

        simulation_futures = workflow_graph.submit(calculation_backend)

        PropertyCalculationLayer._await_results(calculation_backend, storage_backend, layer_directory,
                                                data_model, callback, simulation_futures, synchronous)
