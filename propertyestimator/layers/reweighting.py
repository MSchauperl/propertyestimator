"""
The simulation reweighting estimation layer.
"""
import abc
import json
import logging
import os

from propertyestimator.layers import register_calculation_layer, PropertyCalculationLayer
from propertyestimator.substances import Substance
from propertyestimator.utils.serialization import TypedJSONEncoder
from propertyestimator.utils.utils import SubhookedABCMeta
from propertyestimator.workflow import WorkflowGraph, Workflow
from propertyestimator.workflow.workflow import IWorkflowProperty


class IReweightable(SubhookedABCMeta):

    @property
    @abc.abstractmethod
    def multi_component_property(self):
        """bool: Returns whether this property is dependant on properties of the
        full mixed substance, or whether it is also dependant on the properties
        of the individual components also.
        """
        pass

    @property
    @abc.abstractmethod
    def required_data_class(self):
        """subclass of BaseStoredData: The data class required to reweight this
        property (e.g. `StoredSimulationData`).
        """
        pass

    @property
    @abc.abstractmethod
    def required_data_class(self): pass


@register_calculation_layer()
class ReweightingLayer(PropertyCalculationLayer):
    """A calculation layer which aims to calculate physical properties by
    reweighting the results of previous calculations.

    .. warning :: This class is still heavily under development and is subject to
                 rapid changes.
    """

    @staticmethod
    def schedule_calculation(calculation_backend, storage_backend, layer_directory,
                             data_model, callback, synchronous=False):

        # Make a local copy of the target force field.
        target_force_field = storage_backend.retrieve_force_field(data_model.force_field_id)
        target_force_field_path = os.path.join(layer_directory, data_model.force_field_id)

        target_force_field.to_file(target_force_field_path, io_format='XML',
                                   discard_cosmetic_attributes=False)

        stored_data_paths = ReweightingLayer._retrieve_stored_data(data_model.queued_properties,
                                                                   storage_backend, layer_directory)

        workflow_graph = ReweightingLayer._build_workflow_graph(layer_directory,
                                                                data_model.queued_properties,
                                                                target_force_field_path,
                                                                stored_data_paths,
                                                                data_model.parameter_gradient_keys,
                                                                data_model.options)

        reweighting_futures = workflow_graph.submit(calculation_backend)

        PropertyCalculationLayer._await_results(calculation_backend, storage_backend, layer_directory,
                                                data_model, callback, reweighting_futures, synchronous)

    @staticmethod
    def _retrieve_stored_data(physical_properties, storage_backend, layer_directory):
        """Extract all of the stored data from the backend which may be
        used in reweighting

        Parameters
        ----------
        physical_properties: list of PhysicalProperty
            The physical properties to attempt to estimate.
        storage_backend: PropertyEstimatorStorage
            The storage backend to retrieve the data from.
        layer_directory: str
            The directory in which to store the retrieved data.

        Returns
        -------
        dict of str and dict of str and tuple(str, str, str)
            A dictionary partitioned by substance identifiers and the type,
            of data class, whose values are a tuple of a path to a stored
            simulation data object, it's ancillary data directory, and its
            corresponding force field path.
        """

        data_paths = {}

        for physical_property in physical_properties:

            if not isinstance(physical_property, IReweightable):
                # Only properties which implement the IReweightable
                # interface can be reweighted
                continue

            existing_data = storage_backend.retrieve_simulation_data(physical_property.substance,
                                                                     physical_property.multi_component_property,
                                                                     physical_property.required_data_class)

            if len(existing_data) == 0:
                continue

            # Take data from the storage backend and save it in the working directory.
            for substance_id in existing_data:

                # Register the substance id with the return dictionary
                if substance_id not in data_paths:
                    data_paths[substance_id] = {}

                for data_object, data_directory in existing_data[substance_id]:

                    # Register this objects data class type with the
                    # return dictionary
                    if type(data_object) not in data_paths[substance_id]:
                        data_paths[substance_id][type(data_object)] = []

                    data_object_path = os.path.join(layer_directory, f'{os.path.basename(data_directory)}.json')

                    # Save a local copy of the data object file.
                    if not os.path.isfile(data_object_path):

                        with open(data_object_path, 'w') as file:
                            json.dump(data_object, file, cls=TypedJSONEncoder)

                    force_field_path = os.path.join(layer_directory, data_object.force_field_id)

                    path_tuple = (data_object_path, data_directory, force_field_path)

                    if path_tuple in data_paths[substance_id][type(data_object)]:
                        continue

                    # Save a local copy of the force field file if one
                    # does not already exist.
                    if not os.path.isfile(force_field_path):

                        existing_force_field = storage_backend.retrieve_force_field(data_object.force_field_id)

                        existing_force_field.to_file(force_field_path, io_format='XML',
                                                     discard_cosmetic_attributes=False)

                    data_paths[substance_id][type(data_object)].append(path_tuple)

        return data_paths

    @staticmethod
    def _build_workflow_graph(working_directory, properties, target_force_field_path,
                              stored_data_paths, parameter_gradient_keys, options):
        """Construct a workflow graph, containing all of the workflows which should
        be followed to estimate a set of properties by reweighting.

        Parameters
        ----------
        working_directory: str
            The local directory in which to store all local,
            temporary calculation data from this graph.
        properties : list of PhysicalProperty
            The properties to attempt to compute.
        target_force_field_path : str
            The path to the target force field parameters to use in the workflow.
        stored_data_paths: dict of str and tuple(str, str)
            A dictionary partitioned by substance identifiers, whose values
            are a tuple of a path to a stored simulation data object, and
            its corresponding force field path.
        parameter_gradient_keys: list of ParameterGradientKey
            A list of references to all of the parameters which all observables
            should be differentiated with respect to.
        options: PropertyEstimatorOptions
            The options to run the workflows with.
        """
        workflow_graph = WorkflowGraph(working_directory)

        for property_to_calculate in properties:

            if (not isinstance(property_to_calculate, IReweightable) or
                not isinstance(property_to_calculate, IWorkflowProperty)):
                # Only properties which implement the IReweightable and
                # IWorkflowProperty interfaces can be reweighted
                continue

            property_type = type(property_to_calculate).__name__

            if property_type not in options.workflow_schemas:

                logging.warning('The reweighting layer does not support {} '
                                'workflows.'.format(property_type))

                continue

            if ReweightingLayer.__name__ not in options.workflow_schemas[property_type]:
                continue

            schema = options.workflow_schemas[property_type][ReweightingLayer.__name__]
            workflow_options = options.workflow_options[property_type].get(ReweightingLayer.__name__)

            global_metadata = Workflow.generate_default_metadata(property_to_calculate,
                                                                 target_force_field_path,
                                                                 parameter_gradient_keys,
                                                                 workflow_options)

            substance_id = property_to_calculate.substance.identifier
            data_class_type = property_to_calculate.required_data_class

            if (substance_id not in stored_data_paths or
                data_class_type not in stored_data_paths[substance_id]):

                # We haven't found and cached data which is compatible with this property.
                continue

            global_metadata['full_system_data'] = stored_data_paths[substance_id][data_class_type]
            global_metadata['component_data'] = []

            if property_to_calculate.multi_component_property:

                has_data_for_property = True

                for component in property_to_calculate.substance.components:

                    temporary_substance = Substance()
                    temporary_substance.add_component(component, amount=Substance.MoleFraction())

                    if (temporary_substance.identifier not in stored_data_paths or
                        data_class_type not in stored_data_paths[temporary_substance.identifier]):

                        has_data_for_property = False
                        break

                    global_metadata['component_data'].append(
                        stored_data_paths[temporary_substance.identifier][data_class_type])

                if not has_data_for_property:
                    continue

            workflow = Workflow(property_to_calculate, global_metadata)
            workflow.schema = schema

            from propertyestimator.properties import CalculationSource
            workflow.physical_property.source = CalculationSource(fidelity=ReweightingLayer.__name__,
                                                                           provenance={})

            workflow_graph.add_workflow(workflow)

        return workflow_graph
