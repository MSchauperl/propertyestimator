"""
Defines the core workflow object and execution graph.
"""
import abc
import copy
import json
import logging
import math
import time
import traceback
import uuid
from enum import Enum
from math import sqrt
from os import path, makedirs

from propertyestimator import unit
from propertyestimator.forcefield import SmirnoffForceFieldSource, ForceFieldSource
from propertyestimator.storage.dataclasses import BaseStoredData, StoredSimulationData, StoredDataCollection
from propertyestimator.utils import graph
from propertyestimator.utils.exceptions import PropertyEstimatorException
from propertyestimator.utils.serialization import TypedJSONEncoder, TypedJSONDecoder
from propertyestimator.utils.string import extract_variable_index_and_name, sanitize_smiles_file_name
from propertyestimator.utils.utils import SubhookedABCMeta, get_nested_attribute
from propertyestimator.workflow.protocols import BaseProtocol
from propertyestimator.workflow.schemas import WorkflowSchema, ProtocolReplicator, WorkflowSimulationDataToStore, \
    WorkflowDataCollectionToStore
from propertyestimator.workflow.utils import ProtocolPath, ReplicatorValue


class IWorkflowProperty(SubhookedABCMeta):
    """Defines the interface a property must implement to be estimable
    by a workflow.
    """

    @staticmethod
    @abc.abstractmethod
    def get_default_workflow_schema(calculation_layer, options): pass


class WorkflowOptions:
    """A set of convenience options used when creating
    estimation workflows.
    """

    class ConvergenceMode(Enum):
        """The available options for deciding when a workflow has converged.
        For now, these options include running until the computed uncertainty
        of a property is within a relative fraction of the measured uncertainty
        (`ConvergenceMode.RelativeUncertainty`) or is less than some absolute
        value (`ConvergenceMode.AbsoluteUncertainty`)."""

        NoChecks = 'NoChecks'
        RelativeUncertainty = 'RelativeUncertainty'
        AbsoluteUncertainty = 'AbsoluteUncertainty'

    def __init__(self,
                 convergence_mode=ConvergenceMode.RelativeUncertainty,
                 relative_uncertainty_fraction=1.0, absolute_uncertainty=None,
                 protocol_replacements=None):
        """Constructs a new WorkflowOptions object.

        Parameters
        ----------
        convergence_mode: WorkflowOptions.ConvergenceMode
            The mode which governs how workflows should decide when they have
            reached convergence.
        relative_uncertainty_fraction: float, optional
            If the convergence mode is set to `RelativeUncertainty`, then workflows
            will by default run simulations until the estimated uncertainty is less
            than

            `relative_uncertainty_fraction` * property_to_estimate.uncertainty
        absolute_uncertainty: propertyestimator.unit.Quantity, optional
            If the convergence mode is set to `AbsoluteUncertainty`, then workflows
            will by default run simulations until the estimated uncertainty is less
            than the `absolute_uncertainty`
        protocol_replacements: dict of str and str, optional
            A dictionary with keys of the types of protocols which should be replaced
            with those protocols named by the values.
        """

        self.convergence_mode = convergence_mode

        self.absolute_uncertainty = absolute_uncertainty
        self.relative_uncertainty_fraction = relative_uncertainty_fraction

        if (self.convergence_mode is self.ConvergenceMode.RelativeUncertainty and
            self.relative_uncertainty_fraction is None):

            raise ValueError('The relative uncertainty fraction must be set when the convergence '
                             'mode is set to RelativeUncertainty.')

        if (self.convergence_mode is self.ConvergenceMode.AbsoluteUncertainty and
            self.absolute_uncertainty is None):

            raise ValueError('The absolute uncertainty must be set when the convergence '
                             'mode is set to AbsoluteUncertainty.')

        self.protocol_replacements = protocol_replacements if protocol_replacements is not None else {}

    def __getstate__(self):

        return {
            'convergence_mode': self.convergence_mode,

            'absolute_uncertainty': self.absolute_uncertainty,
            'relative_uncertainty_fraction': self.relative_uncertainty_fraction,

            'protocol_replacements': self.protocol_replacements
        }

    def __setstate__(self, state):

        self.convergence_mode = state['convergence_mode']

        self.absolute_uncertainty = state['absolute_uncertainty']
        self.relative_uncertainty_fraction = state['relative_uncertainty_fraction']

        self.protocol_replacements = state['protocol_replacements']


class Workflow:
    """Encapsulates and prepares a workflow which is able to estimate
    a physical property.
    """

    @property
    def schema(self):
        return self._get_schema()

    @schema.setter
    def schema(self, value):
        self._set_schema(value)

    def __init__(self, physical_property, global_metadata, workflow_uuid=None):
        """
        Constructs a new Workflow object.

        Parameters
        ----------
        physical_property: PhysicalProperty
            The property which this workflow aims to calculate.
        global_metadata: dict of str and Any
            A dictionary of the global metadata available to each
            of the workflow properties.
        workflow_uuid: str, optional
            An optional uuid to assign to this workflow. If none is provided,
            one will be chosen at random.
        """
        assert physical_property is not None and global_metadata is not None

        self.physical_property = physical_property
        self.global_metadata = global_metadata

        self.uuid = workflow_uuid if workflow_uuid is not None else str(uuid.uuid4())

        self.protocols = {}

        self.starting_protocols = []
        self.dependants_graph = {}

        self.final_value_source = None
        self.gradients_sources = []

        self.outputs_to_store = {}

    def _get_schema(self):
        """Returns the schema that describes this workflow.

        Returns
        -------
        WorkflowSchema
            The schema that describes this workflow.
        """
        schema = WorkflowSchema()

        schema.id = self.uuid
        schema.property_type = type(self.physical_property).__name__

        schema.protocols = {}

        for protocol_id, protocol in self.protocols.items():
            schema.protocols[protocol_id] = protocol.schema

        if self.final_value_source is not None:
            schema.final_value_source = ProtocolPath.from_string(self.final_value_source.full_path)

        schema.gradients_sources = [ProtocolPath.from_string(source.full_path) for source in self.gradients_sources]

        schema.outputs_to_store = {}

        for substance_identifier in self.outputs_to_store:

            schema.outputs_to_store[substance_identifier] = \
                copy.deepcopy(self.outputs_to_store[substance_identifier])

        return schema

    def _set_schema(self, value):
        """Sets this workflows properties from a `WorkflowSchema`.

        Parameters
        ----------
        value: WorkflowSchema
            The schema which outlines this steps in this workflow.
        """
        schema = WorkflowSchema.parse_json(value.json())

        if schema.final_value_source is not None:

            self.final_value_source = ProtocolPath.from_string(schema.final_value_source.full_path)
            self.final_value_source.append_uuid(self.uuid)

        self._build_protocols(schema)
        self._build_dependants_graph()

        self.gradients_sources = []

        for gradient_source in schema.gradients_sources:

            copied_source = ProtocolPath.from_string(gradient_source.full_path)
            copied_source.append_uuid(self.uuid)

            self.gradients_sources.append(copied_source)

        self.outputs_to_store = {}

        for label in schema.outputs_to_store:
            self._append_uuid_to_output_to_store(schema.outputs_to_store[label])
            self.outputs_to_store[label] = self._build_output_to_store(schema.outputs_to_store[label])

    def _append_uuid_to_output_to_store(self, output_to_store):
        """Appends this workflows uuid to all of the protocol paths
        within an output to store, and all of its child outputs.

        Parameters
        ----------
        output_to_store: WorkflowOutputToStore
            The output to store to append the uuid to.
        """

        for attribute_key in output_to_store.__getstate__():

            attribute_value = getattr(output_to_store, attribute_key)

            if not isinstance(attribute_value, ProtocolPath):
                continue

            attribute_value.append_uuid(self.uuid)

        if isinstance(output_to_store, WorkflowDataCollectionToStore):

            for inner_data in output_to_store.data.values():
                self._append_uuid_to_output_to_store(inner_data)

    def _build_output_to_store(self, output_to_store_schema):
        """Builds a WorkflowOutputToStore object from the
        an entry defined in the schema.

        Parameters
        ----------
        output_to_store_schema: WorkflowOutputToStore
            The entry defined in the workflow schema.

        Returns
        -------
        WorkflowOutputToStore
            The built object with all of its inputs correctly set.
        """

        output_to_store = copy.deepcopy(output_to_store_schema)

        for attribute_key in output_to_store.__getstate__():

            attribute_value = getattr(output_to_store, attribute_key)

            if not isinstance(attribute_value, ProtocolPath) or not attribute_value.is_global:
                continue

            attribute_value = get_nested_attribute(self.global_metadata, attribute_value.property_name)
            setattr(output_to_store, attribute_key, attribute_value)

        # Make sure to also up any child data objects.
        if isinstance(output_to_store, WorkflowDataCollectionToStore):

            for child_data_label in output_to_store.data:

                child_data = self._build_output_to_store(output_to_store.data[child_data_label])
                output_to_store.data[child_data_label] = child_data

        return output_to_store

    def _build_protocols(self, schema):
        """Creates a set of protocols based on a WorkflowSchema.

        Parameters
        ----------
        schema: WorkflowSchema
            The schema to use when creating the protocols
        """
        from propertyestimator.workflow.plugins import available_protocols

        self._apply_replicators(schema)

        for protocol_name in schema.protocols:

            protocol_schema = schema.protocols[protocol_name]

            protocol = available_protocols[protocol_schema.type](protocol_schema.id)
            protocol.schema = protocol_schema

            # Try to set global properties on each of the protocols
            for input_path in protocol.required_inputs:

                value_references = protocol.get_value_references(input_path)

                for source_path, value_reference in value_references.items():

                    if not value_reference.is_global:
                        continue

                    value = get_nested_attribute(self.global_metadata, value_reference.property_name)
                    protocol.set_value(source_path, value)

            protocol.set_uuid(self.uuid)
            self.protocols[protocol.id] = protocol

    def _get_template_values(self, replicator):
        """Returns the values which which will be passed to the replicated
        protocols, evaluating any protocol paths to retrieve the referenced
        values.

        Parameters
        ----------
        replicator: ProtocolReplicator
            The replictor which is replicating the protocols.

        Returns
        -------
        Any
            The template values.
        """

        invalid_value_error = ValueError(f'Template values must either be a constant or come '
                                         f'from the global scope (and not from {replicator.template_values})')

        # Get the list of values which will be passed to the newly created protocols.
        if isinstance(replicator.template_values, ProtocolPath):

            if not replicator.template_values.is_global:
                raise invalid_value_error

            return get_nested_attribute(self.global_metadata, replicator.template_values.property_name)

        elif not isinstance(replicator.template_values, list):
            raise NotImplementedError()

        evaluated_template_values = []

        for template_value in replicator.template_values:

            if not isinstance(template_value, ProtocolPath):

                evaluated_template_values.append(template_value)
                continue

            if not template_value.is_global:
                raise invalid_value_error

            evaluated_template_values.append(get_nested_attribute(self.global_metadata,
                                                                  template_value.property_name))

        return evaluated_template_values

    def _apply_replicators(self, schema):
        """Applies each of the protocol replicators in turn to the schema.

        Parameters
        ----------
        schema: WorkflowSchema
            The schema to apply the replicators to.
        """

        while len(schema.replicators) > 0:

            replicator = schema.replicators.pop(0)

            # Apply this replicator
            self._apply_replicator(schema, replicator)

            if schema.json().find(replicator.placeholder_id) >= 0:
                raise RuntimeError(f'The {replicator.id} replicator was not fully applied.')

    def _apply_replicator(self, schema, replicator):
        """A method to create a set of protocol schemas based on a ProtocolReplicator,
        and add them to the list of existing schemas.

        Parameters
        ----------
        schema: WorkflowSchema
            The schema which contains the protocol definitions
        replicator: `ProtocolReplicator`
            The replicator which describes which new protocols should
            be created.
        """

        from propertyestimator.workflow.plugins import available_protocols

        # Get the list of values which will be passed to the newly created protocols.
        template_values = self._get_template_values(replicator)

        # Replicate the protocols.
        protocols = {}

        for protocol_id, protocol_schema in schema.protocols.items():

            protocol = available_protocols[protocol_schema.type](schema.id)
            protocol.schema = protocol_schema
            protocols[protocol_id] = protocol

        replicated_protocols, replication_map = replicator.apply(protocols, template_values)
        replicator.update_references(replicated_protocols, replication_map, template_values)

        # Update the schema with the replicated protocols.
        schema.protocols = {}

        for protocol_id in replicated_protocols:
            schema.protocols[protocol_id] = replicated_protocols[protocol_id].schema

        # Make sure to correctly replicate gradient sources.
        replicated_gradient_sources = []

        for gradient_source in schema.gradients_sources:

            if replicator.placeholder_id not in gradient_source.full_path:

                replicated_gradient_sources.append(gradient_source)
                continue

            for index, template_value in enumerate(template_values):

                replicated_source = ProtocolPath.from_string(
                    gradient_source.full_path.replace(replicator.placeholder_id, str(index)))

                replicated_gradient_sources.append(replicated_source)

        schema.gradients_sources = replicated_gradient_sources

        # Replicate any outputs.
        self._apply_replicator_to_outputs(replicator, schema, template_values)
        # Replicate any replicators.
        self._apply_replicator_to_replicators(replicator, schema, template_values)

    def _apply_replicator_to_outputs(self, replicator, schema, template_values):
        """Applies a replicator to a schema outputs to store.

        Parameters
        ----------
        replicator: ProtocolReplicator
            The replicator to apply.
        schema: WorkflowSchema
            The schema which defines the outputs to store.
        template_values: List of Any
            The values being applied by the replicator.
        """

        outputs_to_replicate = []

        for output_label in schema.outputs_to_store:

            if output_label.find(replicator.id) < 0:
                continue

            if isinstance(schema.outputs_to_store[output_label], WorkflowDataCollectionToStore):
                raise NotImplementedError('`WorkflowDataCollectionToStore` cannot currently be replicated.')

            outputs_to_replicate.append(output_label)

        # Check to see if there are any outputs to store pointing to
        # protocols which are being replicated.
        for output_label in outputs_to_replicate:

            output_to_replicate = schema.outputs_to_store.pop(output_label)

            for index, template_value in enumerate(template_values):

                replicated_label = output_label.replace(replicator.placeholder_id, str(index))
                replicated_output = copy.deepcopy(output_to_replicate)

                for attribute_key in replicated_output.__getstate__():

                    attribute_value = getattr(replicated_output, attribute_key)

                    if isinstance(attribute_value, ProtocolPath):

                        attribute_value = ProtocolPath.from_string(
                            attribute_value.full_path.replace(replicator.placeholder_id, str(index)))

                    elif isinstance(attribute_value, ReplicatorValue):

                        if attribute_value.replicator_id != replicator.id:

                            # Make sure to handle nested dependent replicators.
                            attribute_value.replicator_id = attribute_value.replicator_id.replace(
                                replicator.placeholder_id, str(index))

                            continue

                        attribute_value = template_value

                    setattr(replicated_output, attribute_key, attribute_value)

                schema.outputs_to_store[replicated_label] = replicated_output

    @staticmethod
    def _apply_replicator_to_replicators(replicator, schema, template_values):
        """Applies a replicator to any replicators which depend upon
        it (e.g. replicators with ids similar to `other_id_$(replicator.id)`).

        Parameters
        ----------
        replicator: ProtocolReplicator
            The replicator being applied.
        schema: WorkflowSchema
            The workflow schema to which the replicator belongs.
        template_values: List of Any
            The values which the replicator is applying.
        """

        # Look over all of the replicators left to apply and update them
        # to point to the newly replicated protocols where appropriate.
        new_indices = [str(index) for index in range(len(template_values))]

        replicators = []

        for original_replicator in schema.replicators:

            # Check whether this replicator will be replicated.
            if replicator.placeholder_id not in original_replicator.id:

                replicators.append(original_replicator)
                continue

            # Create the replicated replicators
            for template_index in new_indices:

                replicator_id = original_replicator.id.replace(replicator.placeholder_id, template_index)

                new_replicator = ProtocolReplicator(replicator_id)
                new_replicator.template_values = original_replicator.template_values

                # Make sure to replace any reference to the applied replicator
                # with the actual index.
                if isinstance(new_replicator.template_values, ProtocolPath):

                    updated_path = new_replicator.template_values.full_path.replace(replicator.placeholder_id,
                                                                                    template_index)

                    new_replicator.template_values = ProtocolPath.from_string(updated_path)

                elif isinstance(new_replicator.template_values, list):

                    updated_values = []

                    for template_value in new_replicator.template_values:

                        if not isinstance(template_value, ProtocolPath):

                            updated_values.append(template_value)
                            continue

                        updated_path = template_value.full_path.replace(replicator.placeholder_id, template_index)
                        updated_values.append(ProtocolPath.from_string(updated_path))

                    new_replicator.template_values = updated_values

                replicators.append(new_replicator)

        schema.replicators = replicators

    def _build_dependants_graph(self):
        """Builds a dictionary of key value pairs where each key represents the id of a
        protocol to be executed in this workflow, and each value a list ids of protocols
        which must be ran after the protocol identified by the key.
        """

        for protocol_name in self.protocols:
            self.dependants_graph[protocol_name] = []

        for dependant_protocol_name in self.protocols:

            dependant_protocol = self.protocols[dependant_protocol_name]

            for dependency in dependant_protocol.dependencies:

                if dependency.is_global:
                    # Global inputs are outside the scope of the
                    # schema dependency graph.
                    continue

                if dependency.start_protocol == dependant_protocol_name and dependency.start_protocol:
                    # Don't add self to the dependency list.
                    continue

                # Only add a dependency on the protocol at the head of the path,
                # dependencies on the rest of protocols in the path is then implied.
                if dependant_protocol.id in self.dependants_graph[dependency.start_protocol]:
                    continue

                self.dependants_graph[dependency.start_protocol].append(dependant_protocol.id)

        self.starting_protocols = graph.find_root_nodes(self.dependants_graph)

    def replace_protocol(self, old_protocol, new_protocol):
        """Replaces an existing protocol with a new one, while
        updating all input and local references to point to the
        new protocol.

        The main use of this method is when merging multiple protocols
        into one.

        Parameters
        ----------
        old_protocol : protocols.BaseProtocol or str
            The protocol (or its id) to replace.
        new_protocol : protocols.BaseProtocol or str
            The new protocol (or its id) to use.
        """

        old_protocol_id = old_protocol
        new_protocol_id = new_protocol

        if isinstance(old_protocol, BaseProtocol):
            old_protocol_id = old_protocol.id
        if isinstance(new_protocol, BaseProtocol):
            new_protocol_id = new_protocol.id

        if new_protocol_id in self.protocols:
            raise ValueError('A protocol with the same id already exists in this workflow.')

        for protocol_id in self.protocols:
            protocol = self.protocols[protocol_id]
            protocol.replace_protocol(old_protocol_id, new_protocol_id)

        if old_protocol_id in self.protocols and isinstance(new_protocol, BaseProtocol):
            self.protocols.pop(old_protocol_id)
            self.protocols[new_protocol_id] = new_protocol

        for index, starting_id in enumerate(self.starting_protocols):

            if starting_id == old_protocol_id:
                starting_id = new_protocol_id

            self.starting_protocols[index] = starting_id

        for protocol_id in self.dependants_graph:

            for index, dependant_id in enumerate(self.dependants_graph[protocol_id]):

                if dependant_id == old_protocol_id:
                    dependant_id = new_protocol_id

                self.dependants_graph[protocol_id][index] = dependant_id

        if old_protocol_id in self.dependants_graph:
            self.dependants_graph[new_protocol_id] = self.dependants_graph.pop(old_protocol_id)

        if self.final_value_source is not None:
            self.final_value_source.replace_protocol(old_protocol_id, new_protocol_id)

        for gradient_source in self.gradients_sources:
            gradient_source.replace_protocol(old_protocol_id, new_protocol_id)

        for output_label in self.outputs_to_store:

            output_to_store = self.outputs_to_store[output_label]

            for attribute_key in output_to_store.__getstate__():

                attribute_value = getattr(output_to_store, attribute_key)

                if not isinstance(attribute_value, ProtocolPath):
                    continue

                attribute_value.replace_protocol(old_protocol_id,
                                                 new_protocol_id)

            if not isinstance(output_to_store, WorkflowDataCollectionToStore):
                continue

            for inner_data in output_to_store.data.values():

                for attribute_key in inner_data.__getstate__():

                    attribute_value = getattr(inner_data, attribute_key)

                    if not isinstance(attribute_value, ProtocolPath):
                        continue

                    attribute_value.replace_protocol(old_protocol_id,
                                                     new_protocol_id)

    @staticmethod
    def _find_relevant_gradient_keys(substance, force_field_path, parameter_gradient_keys):
        """Extract only those keys which may be applied to the
        given substance.

        Parameters
        ----------
        substance: Substance
            The substance to compare against.
        force_field_path: str
            The path to the force field which contains the parameters.
        parameter_gradient_keys: list of ParameterGradientKey
            The original list of parameter gradient keys.

        Returns
        -------
        list of ParameterGradientKey
            The filtered list of parameter gradient keys.
        """
        from openforcefield.topology import Molecule, Topology

        # noinspection PyTypeChecker
        if parameter_gradient_keys is None or len(parameter_gradient_keys) == 0:
            return []

        with open(force_field_path) as file:
            force_field_source = ForceFieldSource.parse_json(file.read())

        if not isinstance(force_field_source, SmirnoffForceFieldSource):
            return []

        force_field = force_field_source.to_force_field()

        all_molecules = []

        for component in substance.components:
            all_molecules.append(Molecule.from_smiles(component.smiles))

        topology = Topology.from_molecules(all_molecules)
        labelled_molecules = force_field.label_molecules(topology)

        reduced_parameter_keys = []

        for labelled_molecule in labelled_molecules:

            for parameter_key in parameter_gradient_keys:

                if parameter_key.tag not in labelled_molecule or parameter_key in reduced_parameter_keys:
                    continue

                contains_parameter = False

                for parameter in labelled_molecule[parameter_key.tag].store.values():

                    if parameter.smirks != parameter_key.smirks:
                        continue

                    contains_parameter = True
                    break

                if not contains_parameter:
                    continue

                reduced_parameter_keys.append(parameter_key)

        return reduced_parameter_keys

    @staticmethod
    def generate_default_metadata(physical_property, force_field_path,
                                  parameter_gradient_keys=None, workflow_options=None):
        """Generates a default global metadata dictionary.
        
        Parameters
        ----------
        physical_property: PhysicalProperty
            The physical property whose arguments are available in the
            global scope.
        force_field_path: str
            The path to the force field parameters to use in the workflow.
        parameter_gradient_keys: list of ParameterGradientKey
                A list of references to all of the parameters which all observables
                should be differentiated with respect to.
        workflow_options: WorkflowOptions, optional
            The options provided when an estimate request was submitted.

        Returns
        -------
        dict of str, Any

            The metadata dictionary, with the following
            keys / types:

            - thermodynamic_state: `ThermodynamicState` - The state (T,p) at which the
                                                          property is being computed
            - substance: `Substance` - The composition of the system of interest.
            - components: list of `Substance` - The components present in the system for
                                              which the property is being estimated.
            - target_uncertainty: propertyestimator.unit.Quantity - The target uncertainty with which
                                                        properties should be estimated.
            - per_component_uncertainty: propertyestimator.unit.Quantity - The target uncertainty divided
                                                               by the sqrt of the number of
                                                               components in the system + 1
            - force_field_path: str - A path to the force field parameters with which the
                                      property should be evaluated with.
            - parameter_gradient_keys: list of ParameterGradientKey - A list of references to all of the
                                                                      parameters which all observables
                                                                      should be differentiated with respect to.
        """
        from propertyestimator.substances import Substance

        components = []

        for component in physical_property.substance.components:

            component_substance = Substance()
            component_substance.add_component(component, Substance.MoleFraction())

            components.append(component_substance)

        if workflow_options is None:
            workflow_options = WorkflowOptions()

        if workflow_options.convergence_mode == WorkflowOptions.ConvergenceMode.RelativeUncertainty:
            target_uncertainty = physical_property.uncertainty * workflow_options.relative_uncertainty_fraction
        elif workflow_options.convergence_mode == WorkflowOptions.ConvergenceMode.AbsoluteUncertainty:
            target_uncertainty = workflow_options.absolute_uncertainty
        elif workflow_options.convergence_mode == WorkflowOptions.ConvergenceMode.NoChecks:
            target_uncertainty = math.inf
        else:
            raise ValueError('The convergence mode {} is not supported.'.format(workflow_options.convergence_mode))

        if (isinstance(physical_property.uncertainty, unit.Quantity) and not
            isinstance(target_uncertainty, unit.Quantity)):

            target_uncertainty = target_uncertainty * physical_property.uncertainty.units

        # +1 comes from inclusion of the full mixture as a possible component.
        per_component_uncertainty = target_uncertainty / sqrt(physical_property.substance.number_of_components + 1)

        # Find only those gradient keys which will actually be relevant to the
        # property of interest
        relevant_gradient_keys = Workflow._find_relevant_gradient_keys(physical_property.substance,
                                                                       force_field_path,
                                                                       parameter_gradient_keys)

        # Define a dictionary of accessible 'global' properties.
        global_metadata = {
            "thermodynamic_state": physical_property.thermodynamic_state,
            "substance": physical_property.substance,
            "components": components,
            "target_uncertainty": target_uncertainty,
            "per_component_uncertainty": per_component_uncertainty,
            "force_field_path": force_field_path,
            "parameter_gradient_keys": relevant_gradient_keys
        }

        # Include the properties metadata
        global_metadata.update(physical_property.metadata)

        return global_metadata


class WorkflowGraph:
    """A hierarchical structure for storing and submitting the workflows
    which will estimate a set of physical properties..
    """

    def __init__(self, root_directory=''):
        """Constructs a new WorkflowGraph

        Parameters
        ----------
        root_directory: str
            The root directory in which to store all outputs from
            this graph.
        """
        self._protocols_by_id = {}

        self._root_protocol_ids = []
        self._root_directory = root_directory

        self._dependants_graph = {}

        self._workflows_to_execute = {}

    def _insert_protocol(self, protocol_name, workflow, parent_protocol_ids):
        """Inserts a protocol into the workflow graph.

        Parameters
        ----------
        protocol_name : str
            The name of the protocol to insert.
        workflow : Workflow
            The workflow being inserted.
        parent_protocol_ids : `list` of str
            The ids of the new parents of the node to be inserted. If None,
            the protocol will be added as a new parent node.
        """

        if protocol_name in self._protocols_by_id:

            raise RuntimeError('A protocol with id {} has already been '
                               'inserted into the graph.'.format(protocol_name))

        protocols = self._root_protocol_ids if len(parent_protocol_ids) == 0 else []

        for parent_protocol_id in parent_protocol_ids:
            protocols.extend(x for x in self._dependants_graph[parent_protocol_id] if x not in protocols)

        protocol_to_insert = workflow.protocols[protocol_name]
        existing_protocol = None

        # Start by checking to see if the starting protocol of the workflow graph is
        # already present in the full graph.
        for protocol_id in protocols:

            if protocol_id in workflow.protocols:
                continue

            protocol = self._protocols_by_id[protocol_id]

            if not protocol.can_merge(protocol_to_insert):
                continue

            existing_protocol = protocol
            break

        if existing_protocol is not None:

            # Make a note that the existing protocol should be used in place
            # of this workflows version.
            merged_ids = existing_protocol.merge(protocol_to_insert)
            workflow.replace_protocol(protocol_to_insert, existing_protocol)

            for old_id, new_id in merged_ids.items():
                workflow.replace_protocol(old_id, new_id)

        else:

            root_directory = self._root_directory

            if len(parent_protocol_ids) == 1:

                parent_protocol = self._protocols_by_id[parent_protocol_ids[0]]
                root_directory = parent_protocol.directory

            protocol_to_insert.directory = path.join(root_directory, protocol_to_insert.id)

            # Add the protocol as a new protocol in the graph.
            self._protocols_by_id[protocol_name] = protocol_to_insert

            existing_protocol = self._protocols_by_id[protocol_name]
            self._dependants_graph[protocol_name] = []

            if len(parent_protocol_ids) == 0:
                self._root_protocol_ids.append(protocol_name)

        if len(parent_protocol_ids) > 0:

            for protocol_id in workflow.dependants_graph:

                if (existing_protocol.id not in workflow.dependants_graph[protocol_id] or
                    existing_protocol.id in self._dependants_graph[protocol_id] or
                    protocol_id in self._dependants_graph[existing_protocol.id]):

                    continue

                self._dependants_graph[protocol_id].append(existing_protocol.id)

        return existing_protocol.id

    def add_workflow(self, workflow):
        """Insert a workflow into the workflow graph.

        Parameters
        ----------
        workflow : Workflow
            The workflow to insert.
        """

        if workflow.uuid in self._workflows_to_execute:

            raise ValueError('A workflow with the uuid ({}) is '
                             'already in the graph.'.format(workflow.uuid))

        self._workflows_to_execute[workflow.uuid] = workflow

        protocol_execution_order = graph.topological_sort(workflow.dependants_graph)

        reduced_protocol_dependants = copy.deepcopy(workflow.dependants_graph)
        graph.apply_transitive_reduction(reduced_protocol_dependants)

        parent_protocol_ids = {}

        for protocol_id in protocol_execution_order:

            parent_ids = parent_protocol_ids.get(protocol_id) or []
            inserted_id = self._insert_protocol(protocol_id, workflow, parent_ids)

            for dependant in reduced_protocol_dependants[protocol_id]:

                if dependant not in parent_protocol_ids:
                    parent_protocol_ids[dependant] = []

                parent_protocol_ids[dependant].append(inserted_id)

    def submit(self, backend, include_uncertainty_check=True):
        """Submits the protocol graph to the backend of choice.

        Parameters
        ----------
        backend: PropertyEstimatorBackend
            The backend to execute the graph on.
        include_uncertainty_check: bool
            If true, the uncertainty of each estimated property will be checked to
            ensure it is below the target threshold set in the workflow metadata.
            If an uncertainty is not included in the workflow metadata, then this
            parameter will be ignored.

        Returns
        -------
        list of Future:
            The futures of the submitted protocols.
        """
        submitted_futures = {}
        value_futures = []

        # Determine the ideal order in which to submit the
        # protocols.
        submission_order = graph.topological_sort(self._dependants_graph)

        # Build a dependency graph from the dependants graph so that
        # futures can be passed in the correct place.
        dependencies = graph.dependants_to_dependencies(self._dependants_graph)

        for node_id in submission_order:

            node = self._protocols_by_id[node_id]
            dependency_futures = []

            for dependency in dependencies[node_id]:
                dependency_futures.append(submitted_futures[dependency])

            submitted_futures[node_id] = backend.submit_task(WorkflowGraph._execute_protocol,
                                                             node.directory,
                                                             node.schema.json(),
                                                             *dependency_futures,
                                                             key=f'execute_{node_id}')

        for workflow_id in self._workflows_to_execute:

            workflow = self._workflows_to_execute[workflow_id]

            # TODO: Fill in any extra required provenance.
            provenance = {}

            for protocol_id in workflow.protocols:
                
                protocol = workflow.protocols[protocol_id]
                provenance[protocol_id] = protocol.schema

            workflow.physical_property.source.provenance = provenance

            final_futures = []

            if workflow.final_value_source is not None:

                value_node_id = workflow.final_value_source.start_protocol
                final_futures = [submitted_futures[value_node_id]]

            for gradient_source in workflow.gradients_sources:

                protocol_id = gradient_source.start_protocol
                final_futures.append(submitted_futures[protocol_id])

            for output_label in workflow.outputs_to_store:

                output_to_store = workflow.outputs_to_store[output_label]

                for attribute_key in output_to_store.__getstate__():

                    attribute_value = getattr(output_to_store, attribute_key)

                    if not isinstance(attribute_value, ProtocolPath):
                        continue

                    final_futures.append(submitted_futures[attribute_value.start_protocol])

                if not isinstance(output_to_store, WorkflowDataCollectionToStore):
                    continue

                for inner_data in output_to_store.data.values():

                    for attribute_key in inner_data.__getstate__():

                        attribute_value = getattr(inner_data, attribute_key)

                        if not isinstance(attribute_value, ProtocolPath):
                            continue

                        final_futures.append(submitted_futures[attribute_value.start_protocol])

            if len(final_futures) == 0:
                final_futures = [submitted_futures[key] for key in submitted_futures]

            target_uncertainty = None

            if include_uncertainty_check and 'target_uncertainty' in workflow.global_metadata:
                target_uncertainty = workflow.global_metadata['target_uncertainty'].to_tuple()

            # Gather the values and uncertainties of each property being calculated.
            value_futures.append(backend.submit_task(WorkflowGraph._gather_results,
                                                     self._root_directory,
                                                     workflow.physical_property,
                                                     workflow.final_value_source,
                                                     workflow.gradients_sources,
                                                     workflow.outputs_to_store,
                                                     target_uncertainty,
                                                     *final_futures))

        return value_futures

    @staticmethod
    def _save_protocol_output(file_path, output_dictionary):
        """Saves the results of executing a protocol (whether these be the true
        results or an exception) as a JSON file to disk.

        Parameters
        ----------
        file_path: str
            The path to save the output to.
        output_dictionary: dict of str and Any
            The results in the form of a dictionary which can be serialized
            by the `TypedJSONEncoder`
        """

        with open(file_path, 'w') as file:
            json.dump(output_dictionary, file, cls=TypedJSONEncoder)

    @staticmethod
    def _execute_protocol(directory, protocol_schema_json, *previous_output_paths, available_resources, **_):
        """Executes a protocol whose state is defined by the ``protocol_schema``.

        Parameters
        ----------
        protocol_schema_json: str
            The JSON schema defining the protocol to execute.
        previous_output_paths: tuple of str
            Paths to the results of previous protocol executions.

        Returns
        -------
        str
            The id of the executed protocol.
        dict of str and Any
            A dictionary which contains the outputs of the executed protocol.
        """

        from propertyestimator.workflow.plugins import available_protocols
        from propertyestimator.workflow import protocols

        protocol_schema = protocols.ProtocolSchema.parse_json(protocol_schema_json)

        # The path where the output of this protocol will be stored.
        output_dictionary_path = path.join(directory, '{}_output.json'.format(protocol_schema.id))
        makedirs(directory, exist_ok=True)

        # We need to make sure ALL exceptions are handled within this method,
        # or any function which will be executed on a calculation backend to
        # avoid accidentally killing the backend.
        try:

            # If the output file already exists, we can assume this protocol has already
            # been executed and we can return immediately without re-executing.
            if path.isfile(output_dictionary_path):
                return protocol_schema.id, output_dictionary_path

            # Store the results of the relevant previous protocols in a handy dictionary.
            # If one of the results is a failure, propagate it up the chain.
            previous_outputs_by_path = {}

            for parent_id, previous_output_path in previous_output_paths:

                try:

                    with open(previous_output_path, 'r') as file:
                        parent_output = json.load(file, cls=TypedJSONDecoder)

                except json.JSONDecodeError as e:

                    formatted_exception = traceback.format_exception(None, e, e.__traceback__)

                    exception = PropertyEstimatorException(directory,
                                                           f'Could not load the output dictionary of {parent_id} '
                                                           f'({previous_output_path}): {formatted_exception}')

                    WorkflowGraph._save_protocol_output(output_dictionary_path,
                                                        exception)

                    return protocol_schema.id, output_dictionary_path

                if isinstance(parent_output, PropertyEstimatorException):
                    return protocol_schema.id, previous_output_path

                for output_path, output_value in parent_output.items():

                    property_name, protocol_ids = ProtocolPath.to_components(output_path)

                    if len(protocol_ids) == 0 or (len(protocol_ids) > 0 and protocol_ids[0] != parent_id):
                        protocol_ids.insert(0, parent_id)

                    final_path = ProtocolPath(property_name, *protocol_ids)
                    previous_outputs_by_path[final_path] = output_value

            # Recreate the protocol on the backend to bypass the need for static methods
            # and awkward args and kwargs syntax.
            protocol = available_protocols[protocol_schema.type](protocol_schema.id)
            protocol.schema = protocol_schema

            # Pass the outputs of previously executed protocols as input to the
            # protocol to execute.
            for input_path in protocol.required_inputs:

                value_references = protocol.get_value_references(input_path)

                for source_path, target_path in value_references.items():

                    if (target_path.start_protocol == input_path.start_protocol or
                        target_path.start_protocol == protocol.id):

                        continue

                    property_name = target_path.property_name
                    property_index = None

                    nested_property_name = None

                    if property_name.find('.') > 0:

                        nested_property_name = '.'.join(property_name.split('.')[1:])
                        property_name = property_name.split('.')[0]

                    if property_name.find('[') >= 0 or property_name.find(']') >= 0:
                        property_name, property_index = extract_variable_index_and_name(property_name)

                    _, target_protocol_ids = ProtocolPath.to_components(target_path.full_path)

                    target_value = previous_outputs_by_path[ProtocolPath(property_name,
                                                                         *target_protocol_ids)]

                    if property_index is not None:
                        target_value = target_value[property_index]

                    if nested_property_name is not None:
                        target_value = get_nested_attribute(target_value, nested_property_name)

                    protocol.set_value(source_path, target_value)

            logging.info('Executing protocol: {}'.format(protocol.id))

            start_time = time.perf_counter()
            output_dictionary = protocol.execute(directory, available_resources)
            end_time = time.perf_counter()

            logging.info('Protocol finished executing ({} ms): {}'.format((end_time-start_time)*1000, protocol.id))

            try:

                WorkflowGraph._save_protocol_output(output_dictionary_path, output_dictionary)

            except TypeError as e:

                formatted_exception = traceback.format_exception(None, e, e.__traceback__)

                exception = PropertyEstimatorException(directory=directory,
                                                       message=f'Could not save the output dictionary of {protocol.id} '
                                                               f'({output_dictionary_path}): {formatted_exception}')

                WorkflowGraph._save_protocol_output(output_dictionary_path, exception)

            return protocol.id, output_dictionary_path

        except Exception as e:

            logging.info(f'Protocol failed to execute: {protocol_schema.id}')

            # Except the unexcepted...
            formatted_exception = traceback.format_exception(None, e, e.__traceback__)

            exception = PropertyEstimatorException(directory=directory,
                                                   message='An unhandled exception '
                                                           'occurred: {}'.format(formatted_exception))

            WorkflowGraph._save_protocol_output(output_dictionary_path, exception)
            return protocol_schema.id, output_dictionary_path

    @staticmethod
    def _gather_results(directory, property_to_return, value_reference, gradient_sources,
                        outputs_to_store, target_uncertainty, *protocol_result_paths, **_):
        """Gather the value and uncertainty calculated from the submission graph
        and store them in the property to return.

        Parameters
        ----------
        directory: str
            The directory to store any working files in.
        property_to_return: PhysicalProperty
            The property to which the value and uncertainty belong.
        value_reference: ProtocolPath, optional
            A reference to which property in the output dictionary is the actual value.
        gradient_sources: list of ProtocolPath
            A list of references to those entries in the output dictionaries which correspond
            to parameter gradients.
        outputs_to_store: dict of string and WorkflowOutputToStore
            A list of references to data which should be stored on the storage backend.
        target_uncertainty: unit.Quantity, optional
            The uncertainty within which this property should have been estimated. If this
            value is not `None` and the target has not been met, a `None` result will be returned
            indicating that this property could not be estimated by the workflow, but not because
            of an error.
        protocol_results: dict of string and str
            The result dictionary of the protocol which calculated the value of the property.

        Returns
        -------
        CalculationLayerResult, optional
            The result of attempting to estimate this property from a workflow graph. `None`
            will be returned if the target uncertainty is set but not met.
        """
        from propertyestimator.layers.layers import CalculationLayerResult

        if target_uncertainty is not None:
            target_uncertainty = unit.Quantity.from_tuple(target_uncertainty)

        return_object = CalculationLayerResult()
        return_object.property_id = property_to_return.id

        try:
            results_by_id = {}

            for protocol_id, protocol_result_path in protocol_result_paths:

                try:

                    with open(protocol_result_path, 'r') as file:
                        protocol_results = json.load(file, cls=TypedJSONDecoder)

                except json.JSONDecodeError as e:

                    formatted_exception = traceback.format_exception(None, e, e.__traceback__)

                    exception = PropertyEstimatorException(message=f'Could not load the output dictionary of '
                                                                   f'{protocol_id} ({protocol_result_path}): '
                                                                   f'{formatted_exception}')

                    return_object.exception = exception
                    return return_object

                # Make sure none of the protocols failed and we actually have a value
                # and uncertainty.
                if isinstance(protocol_results, PropertyEstimatorException):

                    return_object.exception = protocol_results
                    return return_object

                for output_path, output_value in protocol_results.items():

                    property_name, protocol_ids = ProtocolPath.to_components(output_path)

                    if len(protocol_ids) == 0 or (len(protocol_ids) > 0 and protocol_ids[0] != protocol_id):
                        protocol_ids.insert(0, protocol_id)

                    final_path = ProtocolPath(property_name, *protocol_ids)
                    results_by_id[final_path] = output_value

            if value_reference is not None:

                if (target_uncertainty is not None and
                    results_by_id[value_reference].uncertainty > target_uncertainty):

                    logging.info('The final uncertainty ({}) was not less than the target threshold ({}).'.format(
                        results_by_id[value_reference].uncertainty, target_uncertainty))

                    return None

                property_to_return.value = results_by_id[value_reference].value
                property_to_return.uncertainty = results_by_id[value_reference].uncertainty

            for gradient_source in gradient_sources:

                gradient = results_by_id[gradient_source]
                property_to_return.gradients.append(gradient)

            return_object.calculated_property = property_to_return
            return_object.data_to_store = []

            for output_to_store in outputs_to_store.values():

                if isinstance(output_to_store.substance, ProtocolPath):
                    substance_id = results_by_id[output_to_store.substance].identifier

                else:

                    substance_id = (property_to_return.substance.identifier if
                                    output_to_store.substance is None else
                                    output_to_store.substance.identifier)

                sanitized_id = sanitize_smiles_file_name(substance_id)

                data_object_path = path.join(directory, f'results_{property_to_return.id}_{sanitized_id}.json')
                data_directory = path.join(directory, f'results_{property_to_return.id}_{sanitized_id}')

                WorkflowGraph._store_output_data(data_object_path,
                                                 data_directory,
                                                 output_to_store,
                                                 property_to_return,
                                                 results_by_id)

                return_object.data_to_store.append((data_object_path, data_directory))

        except Exception as e:

            formatted_exception = traceback.format_exception(None, e, e.__traceback__)

            return_object.exception = PropertyEstimatorException(directory=directory,
                                                                 message=f'An unhandled exception '
                                                                         f'occurred: {formatted_exception}')

        return return_object

    @staticmethod
    def _store_output_data(data_object_path, data_directory, output_to_store,
                           physical_property, results_by_id):

        """Collects all of the simulation to store, and saves it into a directory
        whose path will be passed to the storage backend to process.

        Parameters
        ----------
        data_object_path: str
            The file path to serialize the data object to.
        data_directory: str
            The path of the directory to store ancillary data in.
        output_to_store: WorkflowOutputToStore
            An object which contains `ProtocolPath`s pointing to the
            data to store.
        physical_property: PhysicalProperty
            The property which was estimated while generating the
            data to store.
        results_by_id: dict of ProtocolPath and any
            The results of the protocols which formed the property
            estimation workflow.
        """

        makedirs(data_directory, exist_ok=True)

        stored_object = BaseStoredData()

        if type(output_to_store) == WorkflowSimulationDataToStore:
            stored_object = StoredSimulationData()
        elif type(output_to_store) == WorkflowDataCollectionToStore:
            stored_object = StoredDataCollection()

        if output_to_store.substance is None:
            stored_object.substance = physical_property.substance
        elif isinstance(output_to_store.substance, ProtocolPath):
            stored_object.substance = results_by_id[output_to_store.substance]
        else:
            stored_object.substance = output_to_store.substance

        stored_object.thermodynamic_state = physical_property.thermodynamic_state
        stored_object.provenance = physical_property.source
        stored_object.source_calculation_id = physical_property.id

        if isinstance(output_to_store, WorkflowSimulationDataToStore):

            WorkflowGraph._store_simulation_data(stored_object,
                                                 data_directory,
                                                 output_to_store,
                                                 results_by_id)

        elif isinstance(output_to_store, WorkflowDataCollectionToStore):

            for data_key in output_to_store.data:

                inner_data_object = StoredSimulationData()
                inner_data_object.substance = stored_object.substance
                inner_data_object.thermodynamic_state = stored_object.thermodynamic_state
                inner_data_object.source_calculation_id = stored_object.source_calculation_id

                inner_data_directory = path.join(data_directory, data_key)

                makedirs(inner_data_directory, exist_ok=True)

                WorkflowGraph._store_simulation_data(inner_data_object,
                                                     inner_data_directory,
                                                     output_to_store.data[data_key],
                                                     results_by_id)

                stored_object.data[data_key] = inner_data_object

        with open(data_object_path, 'w') as file:
            json.dump(stored_object, file, cls=TypedJSONEncoder)

    @staticmethod
    def _store_simulation_data(data_object, data_directory, output_to_store, results_by_id):
        """Collects all of the simulation to store, and saves it into a directory
        whose path will be passed to the storage backend to process.

        Parameters
        ----------
        data_object: StoredSimulationData
            The data object which is to be stored.
        data_directory: str
            The path of the directory to store ancillary data in.
        output_to_store: WorkflowSimulationDataToStore
            An object which contains `ProtocolPath`s pointing to the
            data to store.
        results_by_id: dict of ProtocolPath and any
            The results of the protocols which formed the property
            estimation workflow.
        """
        from shutil import copy as file_copy

        data_object.total_number_of_molecules = results_by_id[output_to_store.total_number_of_molecules]

        # Copy the files into the directory to store.
        _, coordinate_file_name = path.split(results_by_id[output_to_store.coordinate_file_path])
        _, trajectory_file_name = path.split(results_by_id[output_to_store.trajectory_file_path])

        _, statistics_file_name = path.split(results_by_id[output_to_store.statistics_file_path])

        file_copy(results_by_id[output_to_store.coordinate_file_path], data_directory)
        file_copy(results_by_id[output_to_store.trajectory_file_path], data_directory)

        file_copy(results_by_id[output_to_store.statistics_file_path], data_directory)

        data_object.coordinate_file_name = coordinate_file_name
        data_object.trajectory_file_name = trajectory_file_name

        data_object.statistics_file_name = statistics_file_name

        data_object.statistical_inefficiency = results_by_id[output_to_store.statistical_inefficiency]
