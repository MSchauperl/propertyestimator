import json
import tempfile
import uuid
from os import path, makedirs

from propertyestimator.backends import DaskLocalCluster
from propertyestimator.client import PropertyEstimatorOptions
from propertyestimator.layers import register_calculation_layer, PropertyCalculationLayer
from propertyestimator.layers.layers import CalculationLayerResult
from propertyestimator.properties import Density
from propertyestimator.server import PropertyEstimatorServer
from propertyestimator.storage import LocalFileStorage, StoredSimulationData
from propertyestimator.tests.utils import create_dummy_property
from propertyestimator.utils.exceptions import PropertyEstimatorException
from propertyestimator.utils.serialization import TypedJSONEncoder, TypedJSONDecoder
from propertyestimator.utils.utils import temporarily_change_directory


@register_calculation_layer()
class DummyCalculationLayer(PropertyCalculationLayer):
    """A dummy calculation layer class to test out the base
    calculation layer methods.
    """

    @staticmethod
    def schedule_calculation(calculation_backend, storage_backend, layer_directory,
                             data_model, callback, synchronous=False):

        futures = [
            # Fake a success.
            calculation_backend.submit_task(DummyCalculationLayer.process_successful_property,
                                            data_model.queued_properties[0],
                                            layer_directory),
            # Fake a failure.
            calculation_backend.submit_task(DummyCalculationLayer.process_failed_property,
                                            data_model.queued_properties[1]),

            # Cause an exception.
            calculation_backend.submit_task(DummyCalculationLayer.return_bad_result,
                                            data_model.queued_properties[0],
                                            layer_directory)
        ]

        PropertyCalculationLayer._await_results(calculation_backend,
                                                storage_backend,
                                                layer_directory,
                                                data_model,
                                                callback,
                                                futures,
                                                synchronous)

    @staticmethod
    def process_successful_property(physical_property, layer_directory, **_):
        """Return a result as if the property had been successfully estimated.
        """

        dummy_data_directory = path.join(layer_directory, 'good_dummy_data')
        makedirs(dummy_data_directory, exist_ok=True)

        dummy_stored_object = StoredSimulationData()
        dummy_stored_object.substance = physical_property.substance

        dummy_stored_object_path = path.join(layer_directory, 'good_dummy_data.json')

        with open(dummy_stored_object_path, 'w') as file:
            json.dump(dummy_stored_object, file, cls=TypedJSONEncoder)

        return_object = CalculationLayerResult()
        return_object.property_id = physical_property.id

        return_object.calculated_property = physical_property
        return_object.data_to_store = [(dummy_stored_object_path, dummy_data_directory)]

        return return_object

    @staticmethod
    def process_failed_property(physical_property, **_):
        """Return a result as if the property could not be estimated.
        """

        return_object = CalculationLayerResult()
        return_object.property_id = physical_property.id

        return_object.exception = PropertyEstimatorException(directory='',
                                                             message='Failure Message')

        return return_object

    @staticmethod
    def return_bad_result(physical_property, layer_directory, **_):
        """Return a result which leads to an unhandled exception.
        """

        dummy_data_directory = path.join(layer_directory, 'bad_dummy_data')
        makedirs(dummy_data_directory, exist_ok=True)

        dummy_stored_object = StoredSimulationData()
        dummy_stored_object_path = path.join(layer_directory, 'bad_dummy_data.json')

        with open(dummy_stored_object_path, 'w') as file:
            json.dump(dummy_stored_object, file, cls=TypedJSONEncoder)

        return_object = CalculationLayerResult()
        return_object.property_id = physical_property.id

        return_object.calculated_property = physical_property
        return_object.data_to_store = [(dummy_stored_object_path, dummy_data_directory)]

        return return_object


def test_base_layer():

    properties_to_estimate = [
        create_dummy_property(Density),
        create_dummy_property(Density)
    ]

    dummy_options = PropertyEstimatorOptions()

    request = PropertyEstimatorServer.ServerEstimationRequest(estimation_id=str(uuid.uuid4()),
                                                              queued_properties=properties_to_estimate,
                                                              options=dummy_options,
                                                              force_field_id='')

    with tempfile.TemporaryDirectory() as temporary_directory:

        with temporarily_change_directory(temporary_directory):

            # Create a simple calculation backend to test with.
            test_backend = DaskLocalCluster()
            test_backend.start()

            # Create a simple storage backend to test with.
            test_storage = LocalFileStorage()

            layer_directory = 'dummy_layer'
            makedirs(layer_directory)

            def dummy_callback(returned_request):

                assert len(returned_request.estimated_properties) == 1
                assert len(returned_request.exceptions) == 2

            dummy_layer = DummyCalculationLayer()

            dummy_layer.schedule_calculation(test_backend,
                                             test_storage,
                                             layer_directory,
                                             request,
                                             dummy_callback,
                                             True)


def test_serialize_layer_result():
    """Tests that the `CalculationLayerResult` can be properly
    serialized and deserialized."""

    dummy_result = CalculationLayerResult()

    dummy_result.property_id = str(uuid.uuid4())

    dummy_result.calculated_property = create_dummy_property(Density)
    dummy_result.exception = PropertyEstimatorException()

    dummy_result.data_to_store = [('dummy_object_path', 'dummy_directory')]

    dummy_result_json = json.dumps(dummy_result, cls=TypedJSONEncoder)

    recreated_result = json.loads(dummy_result_json, cls=TypedJSONDecoder)
    recreated_result_json = json.dumps(recreated_result, cls=TypedJSONEncoder)

    assert recreated_result_json == dummy_result_json
