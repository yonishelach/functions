# Generated by nuclio.export.NuclioExporter

import skmultiflow.drift_detection
import numpy as np
import pandas as pd
import os
import json
import v3io.dataplane
import v3io_frames as v3f
import requests
from cloudpickle import load

import random


def split_path(mntpath=""):
    if mntpath[0] == "/":
        mntpath = mntpath[1:]
    paths = mntpath.split("/")
    container = paths[0]
    subpath = ""
    if len(paths) > 1:
        subpath = mntpath[len(container) :]
    return container, subpath


def create_stream(context, path, shards=1):
    container, stream_path = split_path(path)
    context.logger.info(
        f"Creating stream in Container: {container} & Path {stream_path}"
    )
    response = context.v3io_client.create_stream(
        container=container,
        path=stream_path,
        shard_count=shards,
        raise_for_status=v3io.dataplane.RaiseForStatus.never,
    )
    response.raise_for_status([409, 204])


def push_to_stream(context, stream_path, data):
    records = [{"data": json.dumps(rec)} for rec in data]
    container, stream_path = split_path(stream_path)
    response = context.v3io_client.put_records(
        container=container, path=stream_path, records=records
    )


def construct_record(record):
    label_col = os.getenv("label_col", "label")
    prediction_col = os.getenv("prediction_col", "prediction")
    res = dict([(k, record[k]) for k in ["when", "class", "model", "resp", "request"]])
    res["feature_vector"] = res.pop("request")["instances"][0]
    res["timestamp"] = res.pop("when")
    res[prediction_col] = res["resp"][0]
    return res


def init_context(context):
    v3io_client = v3io.dataplane.Client()
    setattr(context, "v3io_client", v3io_client)

    v3f_client = v3f.Client("framesd:8081", container="bigdata")
    setattr(context, "v3f", v3f_client)
    window = []
    setattr(context, "window", window)
    setattr(context, "window_size", int(os.getenv("window_size", 10)))
    setattr(context, "tsdb_table", os.getenv("tsdb_table", "concept_drift_tsdb_1"))
    try:
        context.v3f.create("tsdb", context.tsdb_table, rate="1/s", if_exists=1)
    except Exception as e:
        context.logger.info(f"Creating context with rate= faile for {e}")
        context.v3f.create(
            "tsdb", context.tsdb_table, attrs={"rate": "1/s"}, if_exists=1
        )

    callbacks = [callback.strip() for callback in os.getenv("callbacks", "").split(",")]
    setattr(context, "callbacks", callbacks)

    setattr(context, "drift_stream", os.getenv("drift_stream", "/bigdata/drift_stream"))
    try:
        create_stream(
            context, context.drift_stream, int(os.getenv("drift_stream_shards", 1))
        )
    except:
        context.logger.info(f"{context.drift_stream} already exists")

    models = {}
    model_types = ["pagehinkely", "ddm", "eddm"]
    path_suffix = "_model_path"
    for model in model_types:
        model_env = f"{model}{path_suffix}"
        if model_env in os.environ:
            with open(os.environ[model_env], "rb") as f:
                models[model] = load(f)
    setattr(context, "models", models)

    setattr(context, "label_col", os.getenv("label_col", "label"))
    setattr(context, "prediction_col", os.getenv("prediction_col", "prediction"))


def handler(context, event):
    context.logger.info(f"event: {event.body}")
    full_event = json.loads(event.body)
    record = construct_record(full_event)

    is_error = record[context.label_col] != record[context.prediction_col]
    context.logger.info(f"Adding {is_error}")

    for name, model in context.models.items():
        results = {"timestamp": record["timestamp"]}
        results["algorithm"] = name
        model.add_element(is_error)

        if hasattr(model, "detected_warning_zone") and model.detected_warning_zone():
            context.logger.info(f"{name}\tWarning zone detected")
            results["warning_zone"] = 1
            full_event[f"{name}_warning_zone"] = 1
        else:
            results["warning_zone"] = 0
            full_event[f"{name}_warning_zone"] = 0

        if model.detected_change():
            context.logger.info("Change Detected")
            results["change_detected"] = 1
            full_event[f"{name}_drift"] = 1
        else:
            results["change_detected"] = 0
            full_event[f"{name}_drift"] = 0
        context.window.append(results)

    push_to_stream(context, context.drift_stream, [full_event])

    if context.callbacks != [""]:
        for callback in context.callbacks:
            requests.post(url=callback, json=full_event)

    if (len(context.window) / len(context.models)) >= context.window_size:
        df = pd.DataFrame(context.window)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index(["timestamp", "algorithm"])
        context.v3f.write("tsdb", context.tsdb_table, df)
        context.window = []
