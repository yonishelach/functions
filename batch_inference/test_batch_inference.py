# Copyright 2019 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import json
import os

import mlrun
import mlrun.common.schemas
import numpy as np
import pandas as pd
import pytest
from mlrun.frameworks.sklearn import apply_mlrun
from sklearn.datasets import make_classification
from sklearn.tree import DecisionTreeClassifier

REQUIRED_ENV_VARS = [
    "MLRUN_DBPATH",
    "V3IO_USERNAME",
    "V3IO_API",
    "V3IO_ACCESS_KEY",
]


def _validate_environment_variables() -> bool:
    """
    Checks that all required Environment variables are set.
    """
    environment_keys = os.environ.keys()
    return all(key in environment_keys for key in REQUIRED_ENV_VARS)


@mlrun.handler(outputs=["training_set", "prediction_set"])
def generate_data(n_samples: int = 5000, n_features: int = 20, n_classes: int = 2):
    # Generate a classification data:
    x, y = make_classification(n_samples=n_samples, n_features=n_features, n_classes=2)

    # Split the data into a training set and a prediction set:
    x_train, x_prediction = x[: n_samples // 2], x[n_samples // 2 :]
    y_train = y[: n_samples // 2]

    # Randomly drift some features:
    x_prediction += np.random.uniform(
        low=2, high=4, size=x_train.shape
    ) * np.random.randint(low=0, high=2, size=x_train.shape[1], dtype=int)

    # Initialize dataframes:
    features = [f"feature_{i}" for i in range(n_features)]
    training_set = pd.DataFrame(data=x_train, columns=features)
    training_set.insert(
        loc=n_features, column="target_label", value=y_train, allow_duplicates=True
    )
    prediction_set = pd.DataFrame(data=x_prediction, columns=features)

    return training_set, prediction_set


@mlrun.handler()
def train(training_set: pd.DataFrame):
    # Get the data into x, y:
    labels = pd.DataFrame(training_set["target_label"])
    training_set.drop(columns=["target_label"], inplace=True)

    # Initialize a model:
    model = DecisionTreeClassifier()

    # Apply MLRun:
    apply_mlrun(model=model, model_name="model")

    # Train:
    model.fit(training_set, labels)


@pytest.mark.skipif(
    condition=not _validate_environment_variables(),
    reason="Project's environment variables are not set",
)
def test_batch_predict():

    project = mlrun.get_or_create_project(
        "batch-infer-v9-test", context="./", user_project=True
    )

    # Configure test:
    n_samples = 5000
    n_features = 20

    # Create the function and run:
    test_function = mlrun.code_to_function(filename=__file__, kind="job")
    generate_data_run = test_function.run(
        handler="generate_data",
        params={"n_samples": n_samples, "n_features": n_features},
        local=True,
    )
    train_run = test_function.run(
        handler="train",
        inputs={"training_set": generate_data_run.outputs["training_set"]},
        local=True,
    )

    batch_predict_function = mlrun.import_function("function.yaml")
    batch_predict_run = batch_predict_function.run(
        handler="infer",
        inputs={"dataset": generate_data_run.outputs["prediction_set"]},
        params={
            "model": train_run.outputs["model"],
            "result_set_name": "result_set",
        },
    )

    # Check the result set:
    result_set = batch_predict_run.artifact("result_set").as_df()
    assert result_set.shape == (n_samples // 2, n_features + 1)
    assert "target_label" in result_set.columns
    assert "batch_id" in batch_predict_run.status.results

    # Check drift table artifact url
    assert (
        batch_predict_run.artifact("drift_table_plot").artifact_url
        == batch_predict_run.outputs["drift_table_plot"]
    )

    # Check the features drift results json:
    drift_results_file = batch_predict_run.artifact("features_drift_results").local()
    with open(drift_results_file, "r") as json_file:
        drift_results = json.load(json_file)
    assert len(drift_results) == n_features + 1

    # Check the final analysis logged results:
    assert "drift_status" in batch_predict_run.status.results
    assert "drift_metric" in batch_predict_run.status.results
