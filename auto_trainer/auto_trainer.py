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
from typing import List, Dict, Optional, Union, Tuple, Any
from pathlib import Path

import mlrun
import pandas as pd
from sklearn.model_selection import train_test_split

from mlrun.execution import MLClientCtx
from mlrun.datastore import DataItem
from mlrun.frameworks.auto_mlrun import AutoMLRun
from mlrun import feature_store as fs
from mlrun.api.schemas import ObjectKind
from mlrun.utils.helpers import create_class, create_function

PathType = Union[str, Path]


class KWArgsPrefixes:
    MODEL_CLASS = "CLASS_"
    FIT = "FIT_"
    TRAIN = "TRAIN_"
    PREDICT = "PREDICT_"


def _get_sub_dict_by_prefix(src: Dict, prefix_key: str) -> Dict[str, Any]:
    """
    Collect all the keys from the given dict that starts with the given prefix and creates a new dictionary with these
    keys.

    :param src:         The source dict to extract the values from.
    :param prefix_key:  Only keys with this prefix will be returned. The keys in the result dict will be without this
                        prefix.
    """
    return {
        key.replace(prefix_key, ""): val
        for key, val in src.items()
        if key.startswith(prefix_key)
    }


def _get_dataframe(
    context: MLClientCtx,
    dataset: DataItem,
    label_columns: Optional[Union[str, List[str]]] = None,
    drop_columns: Union[str, List[str], int, List[int]] = None,
) -> Tuple[pd.DataFrame, Optional[Union[str, List[str]]]]:
    """
    Getting the DataFrame of the dataset and drop the columns accordingly.

    :param context:         MLRun context.
    :param dataset:         The dataset to train the model on.
                            Can be either a list of lists, dict, URI or a FeatureVector.
    :param label_columns:   The target label(s) of the column(s) in the dataset. for Regression or
                            Classification tasks.
    :param drop_columns:    str/int or a list of strings/ints that represent the column names/indices to drop.
    """
    if isinstance(dataset, (list, dict)):
        dataset = pd.DataFrame(dataset)
        # Checking if drop_columns provided by integer type:
        if drop_columns:
            if isinstance(drop_columns, str) or (
                isinstance(drop_columns, list)
                and any(isinstance(col, str) for col in drop_columns)
            ):
                context.logger.error(
                    "drop_columns must be an integer/list of integers if not provided with a URI/FeatureVector dataset"
                )
                raise ValueError
            dataset.drop(drop_columns, axis=1, inplace=True)

        return dataset, label_columns

    if dataset.meta and dataset.meta.kind == ObjectKind.feature_vector:
        # feature-vector case:
        label_columns = label_columns or dataset.meta.status.label_column
        dataset = fs.get_offline_features(
            dataset.meta.uri, drop_columns=drop_columns
        ).to_dataframe()

        context.logger.info(f"label columns: {label_columns}")
    else:
        # simple URL case:
        dataset = dataset.as_df()
        if drop_columns:
            if all(col in dataset for col in drop_columns):
                dataset = dataset.drop(drop_columns, axis=1)
            else:
                context.logger.info(
                    "not all of the columns to drop in the dataset, drop columns process skipped"
                )
    return dataset, label_columns


def train(
    context: MLClientCtx,
    dataset: DataItem,
    drop_columns: List[str] = None,
    model_class: str = None,
    model_name: str = "model",
    tag: str = "",
    label_columns: Optional[Union[str, List[str]]] = None,
    sample_set: DataItem = None,
    test_set: DataItem = None,
    train_test_split_size: float = None,
    random_state: int = None,
):
    """
    Training the given model on the given dataset.

    :param context:                 MLRun context
    :param dataset:                 The dataset to train the model on. Can be either a URI or a FeatureVector
    :param drop_columns:            str or a list of strings that represent the columns to drop
    :param model_class:             The class of the model, e.g. `sklearn.linear_model.LogisticRegression`
    :param model_name:              The model's name to use for storing the model artifact, default to 'model'
    :param tag:                     The model's tag to log with
    :param label_columns:           The target label(s) of the column(s) in the dataset. for Regression or
                                    Classification tasks
    :param sample_set:              A sample set of inputs for the model for logging its stats along the model in favour
                                    of model monitoring. Can be either a URI or a FeatureVector
    :param test_set:                The test set to train the model with
    :param train_test_split_size:   Should be between 0.0 and 1.0 and represent the proportion of the dataset to include
                                    in the test split. The size of the Training set is set to the complement of this
                                    value. Default = 0.2
    :param random_state:            Random state for `train_test_split`
    """
    # Validate inputs:
    # Check if exactly one of them is supplied:
    if test_set is None:
        if train_test_split_size is None:
            context.logger.info(
                "test_set or train_test_split_size are not provided, setting train_test_split_size to 0.2"
            )
            train_test_split_size = 0.2

    elif train_test_split_size:
        context.logger.info(
            "test_set provided, ignoring given train_test_split_size value"
        )
        train_test_split_size = None

    # Get DataFrame by URL or by FeatureVector:
    dataset, label_columns = _get_dataframe(
        context=context,
        dataset=dataset,
        label_columns=label_columns,
        drop_columns=drop_columns,
    )
    # TODO: Remove dropna after fixing steps in feature store
    #  This is a temporary change for fraud-prevention demo
    dataset.dropna(inplace=True)

    # Getting the sample set:
    if sample_set is None:
        context.logger.info(
            f"Sample set not given, using the whole training set as the sample set"
        )
        sample_set = dataset
    else:
        sample_set, _ = _get_dataframe(
            context=context,
            dataset=sample_set,
            label_columns=label_columns,
            drop_columns=drop_columns,
        )

    # Parsing kwargs:
    # TODO: Use in xgb or lgbm train function.
    train_kwargs = _get_sub_dict_by_prefix(
        src=context.parameters, prefix_key=KWArgsPrefixes.TRAIN
    )
    fit_kwargs = _get_sub_dict_by_prefix(
        src=context.parameters, prefix_key=KWArgsPrefixes.FIT
    )
    model_class_kwargs = _get_sub_dict_by_prefix(
        src=context.parameters, prefix_key=KWArgsPrefixes.MODEL_CLASS
    )

    # Check if model or function:
    if hasattr(model_class, "train"):
        # TODO: Need to call: model(), afterwards to start the train function.
        # model = create_function(f"{model_class}.train")
        raise NotImplementedError
    else:
        # Creating model instance:
        model = create_class(model_class)(**model_class_kwargs)

    x = dataset.drop(label_columns, axis=1)
    y = dataset[label_columns]
    if train_test_split_size:
        x_train, x_test, y_train, y_test = train_test_split(
            x, y, test_size=train_test_split_size, random_state=random_state
        )
    else:
        x_train, y_train = x, y

        test_set = test_set.as_df()
        if drop_columns:
            test_set = dataset.drop(drop_columns, axis=1)

        x_test, y_test = test_set.drop(label_columns, axis=1), test_set[label_columns]

    AutoMLRun.apply_mlrun(
        model=model,
        model_name=model_name,
        context=context,
        tag=tag,
        sample_set=sample_set,
        y_columns=label_columns,
        test_set=test_set,
        x_test=x_test,
        y_test=y_test,
        artifacts=context.artifacts,
    )
    context.logger.info(f"training '{model_name}'")
    model.fit(x_train, y_train, **fit_kwargs)


def evaluate(
    context: MLClientCtx,
    model: str,
    dataset: mlrun.DataItem,
    drop_columns: List[str] = None,
    label_columns: Optional[Union[str, List[str]]] = None,
):
    """
    Evaluating a model. Artifacts generated by the MLHandler.

    :param context:                 MLRun context.
    :param model:                   The model Store path.
    :param dataset:                 The dataset to evaluate the model on. Can be either a URI or a FeatureVector.
    :param drop_columns:            str or a list of strings that represent the columns to drop.
    :param label_columns:           The target label(s) of the column(s) in the dataset. for Regression or
                                    Classification tasks.
    """
    # Get dataset by URL or by FeatureVector:
    dataset, label_columns = _get_dataframe(
        context=context,
        dataset=dataset,
        label_columns=label_columns,
        drop_columns=drop_columns,
    )

    # Parsing label_columns:
    parsed_label_columns = []
    if label_columns:
        label_columns = label_columns if isinstance(label_columns, list) else [label_columns]
        for lc in label_columns:
            if fs.common.feature_separator in lc:
                feature_set_name, label_name, alias = fs.common.parse_feature_string(lc)
                parsed_label_columns.append(alias or label_name)
        if parsed_label_columns:
            label_columns = parsed_label_columns

    # Parsing kwargs:
    predict_kwargs = _get_sub_dict_by_prefix(
        src=context.parameters, prefix_key=KWArgsPrefixes.PREDICT
    )

    x = dataset.drop(label_columns, axis=1)
    y = dataset[label_columns]

    # Loading the model and predicting:
    model_handler = AutoMLRun.load_model(model_path=model, context=context)
    AutoMLRun.apply_mlrun(model_handler.model, y_test=y, model_path=model)

    context.logger.info(f"evaluating '{model_handler.model_name}'")
    model_handler.model.predict(x, **predict_kwargs)


def predict(
    context: MLClientCtx,
    model: str,
    dataset: mlrun.DataItem,
    drop_columns: Union[str, List[str], int, List[int]] = None,
    label_columns: Optional[Union[str, List[str]]] = None,
    result_set: Optional[str] = None,
):
    """
    Predicting dataset by a model.

    :param context:                 MLRun context.
    :param model:                   The model Store path.
    :param dataset:                 The dataset to predict the model on. Can be either a URI, a FeatureVector or a
                                    sample in a shape of a list/dict.
                                    When passing a sample, pass the dataset as a field in `params` instead of `inputs`.
    :param drop_columns:            str/int or a list of strings/ints that represent the column names/indices to drop.
                                    When the dataset is a list/dict this parameter should be represented by integers.
    :param label_columns:           The target label(s) of the column(s) in the dataset. for Regression or
                                    Classification tasks.
    :param result_set:              The db key to set name of the prediction result and the filename.
                                    Default to 'prediction'.
    """
    # Get dataset by URL or by FeatureVector:
    dataset, label_columns = _get_dataframe(
        context=context,
        dataset=dataset,
        label_columns=label_columns,
        drop_columns=drop_columns,
    )

    # Parsing kwargs:
    predict_kwargs = _get_sub_dict_by_prefix(
        src=context.parameters, prefix_key=KWArgsPrefixes.PREDICT
    )

    # loading the model, and getting the model handler:
    model_handler = AutoMLRun.load_model(model_path=model, context=context)

    # Dropping label columns if necessary:
    if not label_columns:
        label_columns = []
    elif isinstance(label_columns, str):
        label_columns = [label_columns]

    # Predicting:
    context.logger.info(f"making prediction by '{model_handler.model_name}'")
    y_pred = model_handler.model.predict(dataset, **predict_kwargs)

    # Preparing and validating label columns for the dataframe of the prediction result:
    num_predicted = 1 if len(y_pred.shape) == 1 else y_pred.shape[1]

    if num_predicted > len(label_columns):
        if num_predicted == 1:
            label_columns = ["predicted labels"]
        else:
            label_columns.extend(
                [
                    f"predicted_label_{i + 1 + len(label_columns)}"
                    for i in range(num_predicted - len(label_columns))
                ]
            )
    elif num_predicted < len(label_columns):
        context.logger.error(
            f"number of predicted labels: {num_predicted} is smaller than number of label columns: {len(label_columns)}"
        )
        raise ValueError

    artifact_name = result_set or 'prediction'
    labels_inside_df = set(label_columns) & set(dataset.columns.tolist())
    if labels_inside_df:
        context.logger.error(f"The labels: {labels_inside_df} are already existed in the dataframe")
        raise ValueError
    pred_df = pd.concat([dataset, pd.DataFrame(y_pred, columns=label_columns)], axis=1)
    context.log_dataset(artifact_name, pred_df, db_key=result_set)
