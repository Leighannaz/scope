#!/usr/bin/env python
import fire
import numpy as np
import pandas as pd
import pathlib
from penquins import Kowalski
from typing import List
import yaml
import os
import time
import h5py
from scope.utils import write_parquet, impute_features
from datetime import datetime
import pyarrow.dataset as ds

BASE_DIR = os.path.dirname(__file__)
JUST = 50


config_path = pathlib.Path(__file__).parent.parent.absolute() / "config.yaml"
with open(config_path) as config_yaml:
    config = yaml.load(config_yaml, Loader=yaml.FullLoader)

# Access datatypes in config file
all_feature_names_config = config["features"]["ontological"]
dtype_dict = {
    key: all_feature_names_config[key]['dtype'] for key in all_feature_names_config
}

# Only features listed in config (regardless of include:) will be downloaded
projection_dict = {key: 1 for key in all_feature_names_config}

period_suffix = config['features']['info']['period_suffix']
# Rename periodic feature columns if suffix provided in config (features: info: period_suffix:)
if not ((period_suffix is None) | (period_suffix == 'None')):
    all_feature_names = [x for x in all_feature_names_config.keys()]
    periodic_bool = [all_feature_names_config[x]['periodic'] for x in all_feature_names]
    for j, name in enumerate(all_feature_names):
        if periodic_bool[j]:
            all_feature_names[j] = f'{name}_{period_suffix}'

    dtype_values = [x for x in dtype_dict.values()]
    projection_values = [x for x in projection_dict.values()]

    dtype_dict = {
        all_feature_names[i]: dtype_values[i] for i in range(len(dtype_values))
    }
    projection_dict = {
        all_feature_names[i]: projection_values[i]
        for i in range(len(projection_values))
    }

# use tokens specified as env vars (if exist)
kowalski_token_env = os.environ.get("KOWALSKI_INSTANCE_TOKEN")
gloria_token_env = os.environ.get("GLORIA_INSTANCE_TOKEN")
melman_token_env = os.environ.get("MELMAN_INSTANCE_TOKEN")

# Set up Kowalski instance connection
if kowalski_token_env is not None:
    config["kowalski"]["hosts"]["kowalski"]["token"] = kowalski_token_env
if gloria_token_env is not None:
    config["kowalski"]["hosts"]["gloria"]["token"] = gloria_token_env
if melman_token_env is not None:
    config["kowalski"]["hosts"]["melman"]["token"] = melman_token_env

timeout = config['kowalski']['timeout']

hosts = [
    x
    for x in config['kowalski']['hosts']
    if config['kowalski']['hosts'][x]['token'] is not None
]
instances = {
    host: {
        'protocol': config['kowalski']['protocol'],
        'port': config['kowalski']['port'],
        'host': f'{host}.caltech.edu',
        'token': config['kowalski']['hosts'][host]['token'],
    }
    for host in hosts
}

kowalski_instances = Kowalski(timeout=timeout, instances=instances)


def get_features_loop(
    func,
    source_ids: List[int],
    features_catalog: str = "ZTF_source_features_DR5",
    verbose: bool = False,
    whole_field: bool = True,
    field: int = 291,
    ccd: int = 1,
    quad: int = 1,
    limit_per_query: int = 1000,
    max_sources: int = 100000,
    impute_missing_features: bool = False,
    self_impute: bool = True,
    restart: bool = True,
    write_csv: bool = False,
    projection: dict = {},
    suffix: str = None,
    save: bool = True,
):
    '''
    Loop over get_features.py to save at specified checkpoints.
    '''

    if not whole_field:
        outfile = (
            os.path.dirname(__file__)
            + "/../features/field_"
            + str(field)
            + "/ccd_"
            + str(ccd).zfill(2)
            + "_quad_"
            + str(quad)
        )

    else:
        outfile = (
            os.path.dirname(__file__)
            + "/../features/field_"
            + str(field)
            + "/"
            + "field_"
            + str(field)
        )

    if suffix is not None:
        outfile += f'_{suffix}'
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    DS = ds.dataset(os.path.dirname(outfile), format='parquet')
    indiv_files = DS.files
    files_exist = len(indiv_files) > 0
    existing_ids = []

    # Set source_ids
    if (not restart) & (files_exist):
        generator = DS.to_batches(columns=['_id'])
        for batch in generator:
            existing_ids += batch['_id'].to_pylist()
        # Remove existing source_ids from list
        todo_source_ids = list(set(source_ids) - set(existing_ids))
        if len(todo_source_ids) == 0:
            print('Dataset is already complete.')
            return

    n_sources = len(source_ids)
    if n_sources % max_sources != 0:
        n_iterations = n_sources // max_sources + 1
    else:
        n_iterations = n_sources // max_sources
    start_iteration = len(existing_ids) // max_sources

    for i in range(start_iteration, n_iterations):
        print(f"Iteration {i+1} of {n_iterations}...")
        select_source_ids = source_ids[
            i * max_sources : min(n_sources, (i + 1) * max_sources)
        ]

        df, _ = func(
            source_ids=select_source_ids,
            features_catalog=features_catalog,
            verbose=verbose,
            limit_per_query=limit_per_query,
            impute_missing_features=impute_missing_features,
            self_impute=self_impute,
            projection=projection,
        )

        if save:
            write_parquet(df, f'{outfile}_iter_{i}.parquet')
            files_exist = True
            if write_csv:
                df.to_csv(f'{outfile}_iter_{i}.csv', index=False)

    return df, outfile


def get_features(
    source_ids: List[int],
    features_catalog: str = "ZTF_source_features_DR5",
    verbose: bool = False,
    limit_per_query: int = 1000,
    impute_missing_features: bool = False,
    self_impute: bool = True,
    dtypes: dict = dtype_dict,
    projection: dict = projection_dict,
):
    '''
    Get features of all ids present in the field in one file.
    '''

    id = 0
    df_collection = []
    dmdt_temp = []
    dmdt_collection = []

    while True:
        query = {
            "query_type": "find",
            "query": {
                "catalog": features_catalog,
                "filter": {
                    "_id": {
                        "$in": source_ids[
                            id * limit_per_query : (id + 1) * limit_per_query
                        ]
                    }
                },
                "projection": projection,
            },
        }
        responses = kowalski_instances.query(query=query)

        for name in responses.keys():
            if len(responses[name]) > 0:
                response = responses[name]
                if response.get("status", "error") == "success":
                    source_data = response.get("data")
                    if source_data is None:
                        print(response)
                        raise ValueError(f"No data found for source ids {source_ids}")

        df_temp = pd.DataFrame(source_data)
        if (projection == {}) | ("dmdt" in projection):
            df_temp = df_temp.astype(dtype=dtypes)
        df_collection += [df_temp]
        try:
            dmdt_temp = np.expand_dims(
                np.array([d for d in df_temp['dmdt'].values]), axis=-1
            )
        except Exception as e:
            # Print dmdt error if using the default projection or user requests the feature
            if (projection == {}) | ("dmdt" in projection):
                print("Error", e)
                print(df_temp)
        dmdt_collection += [dmdt_temp]

        if ((id + 1) * limit_per_query) >= len(source_ids):
            print(f'{len(source_ids)} done')
            break
        id += 1
        if (id * limit_per_query) % limit_per_query == 0:
            print(id * limit_per_query, "done")

    df = pd.concat(df_collection, axis=0)
    df.reset_index(drop=True, inplace=True)
    dmdt = np.vstack(dmdt_collection)

    if impute_missing_features:
        df = impute_features(df, self_impute=self_impute)

    # Add metadata
    utcnow = datetime.utcnow()
    start_dt = utcnow.strftime("%Y-%m-%d %H:%M:%S")
    features_ztf_dr = features_catalog.split('_')[-1]
    df.attrs['features_download_dateTime_utc'] = start_dt
    df.attrs['features_ztf_dataRelease'] = features_ztf_dr
    df.attrs['features_imputed'] = impute_missing_features

    if verbose:
        print("Features dataframe: ", df)
        print("dmdt shape: ", dmdt.shape)

    return df, dmdt


def run(**kwargs):
    """
    Get the features of all sources in a field.

    Parameters
    ==========
    field: int
        Field number.
    ccd_range: int, list
        CCD range; single int or list of two ints between 1 and 16 (default range is [1,16])
    quad_range: int, list
        Quadrant range; single int or list of two ints between 1 and 4 (default range is [1,4])
    limit_per_query: int
        Number of sources to query at a time.
    max_sources: int
        Number of sources to save in single file.
    features_catalog: str
        Name of Kowalski collection to query for features
    whole_field: bool
        If True, get features of all sources in the field, else get features of a particular quad.
    start: int
        Start index of the sources to query. (to be used with whole_field)
    end: int
        End index of the sources to query. (to be used with whole_field)
    restart: bool
        if True, restart the querying of features even if file exists.
    write_results: bool
        if True, write results and make necessary directories.
    write_csv: bool
        if True, writes results as csv file in addition to parquet.
    column_list: list
        List of strings for each column to return from Kowalski collection.
    suffix: str
        Suffix to add to saved feature file.
    Returns
    =======
    Stores the features in a file at the following location:
        features/field_<field>/field_<field>.parquet
    or  features/field_<field>/field_<field>.csv
    """

    DEFAULT_FIELD = 291
    DEFAULT_CCD_RANGE = [1, 16]
    DEFAULT_QUAD_RANGE = [1, 4]
    DEFAULT_LIMIT = 1000
    DEFAULT_SAVE_BATCHSIZE = 100000
    features_catalog = config['kowalski']['collections']['features']
    DEFAULT_CATALOG = features_catalog

    field = kwargs.get("field", DEFAULT_FIELD)
    ccd_range = kwargs.get("ccd_range", DEFAULT_CCD_RANGE)
    quad_range = kwargs.get("quad_range", DEFAULT_QUAD_RANGE)
    limit_per_query = kwargs.get("limit_per_query", DEFAULT_LIMIT)
    max_sources = kwargs.get("max_sources", DEFAULT_SAVE_BATCHSIZE)
    features_catalog = kwargs.get("features_catalog", DEFAULT_CATALOG)
    whole_field = kwargs.get("whole_field", False)
    start = kwargs.get("start", None)
    end = kwargs.get("end", None)
    restart = kwargs.get("restart", False)
    write_results = kwargs.get("write_results", True)
    write_csv = kwargs.get("write_csv", False)
    column_list = kwargs.get("column_list", None)
    suffix = kwargs.get("suffix", None)
    impute_missing_features = kwargs.get("impute_missing_features", False)
    self_impute = kwargs.get("self_impute", True)

    projection = {}
    if column_list is not None:
        keys = [name for name in column_list]
        projection = {k: 1 for k in keys}

    if type(ccd_range) == int:
        ccd_range = [ccd_range, ccd_range]
    if type(quad_range) == int:
        quad_range = [quad_range, quad_range]

    iter_dct = {}

    if not whole_field:
        for ccd in range(ccd_range[0], ccd_range[1] + 1):
            for quad in range(quad_range[0], quad_range[1] + 1):
                default_file = (
                    "../ids/field_"
                    + str(field)
                    + "/data_ccd_"
                    + str(ccd).zfill(2)
                    + "_quad_"
                    + str(quad)
                    + ".h5"
                )
                iter_dct[(ccd, quad)] = default_file
    else:
        default_file = "../ids/field_" + str(field) + "/field_" + str(field) + ".h5"
        iter_dct[field] = default_file

    for k, v in iter_dct.items():
        if type(k) == tuple:
            ccd_quad = k
            print(f'Getting features for ccd {ccd_quad[0]} quad {ccd_quad[1]}...')
        else:
            ccd_quad = (0, 0)
            print(f'Getting features for field {field}...')
        default_file = v
        source_ids_filename = kwargs.get("source_ids_filename", default_file)

        tm = kwargs.get("time", False)
        filename = os.path.join(BASE_DIR, source_ids_filename)

        ts = time.time()
        source_ids = np.array([])
        with h5py.File(filename, "r") as f:
            ids = np.array(f[list(f.keys())[0]])
            source_ids = list(map(int, np.concatenate((source_ids, ids), axis=0)))
        te = time.time()
        if tm:
            print(
                "read source_ids from .h5".ljust(JUST)
                + "\t --> \t"
                + str(round(te - ts, 4))
                + " s"
            )

        verbose = kwargs.get("verbose", False)
        if verbose:
            print(f"{len(source_ids)} total source ids")

        if write_results:
            get_features_loop(
                get_features,
                source_ids=source_ids[start:end],
                features_catalog=features_catalog,
                verbose=verbose,
                whole_field=whole_field,
                field=field,
                ccd=ccd_quad[0],
                quad=ccd_quad[1],
                limit_per_query=limit_per_query,
                max_sources=max_sources,
                impute_missing_features=impute_missing_features,
                self_impute=self_impute,
                restart=restart,
                write_csv=write_csv,
                projection=projection,
                suffix=suffix,
                save=True,
            )

        else:
            # get raw features
            get_features(
                source_ids=source_ids[start:end],
                features_catalog=features_catalog,
                verbose=verbose,
                limit_per_query=limit_per_query,
                impute_missing_features=impute_missing_features,
                self_impute=self_impute,
                projection=projection,
            )


if __name__ == "__main__":
    fire.Fire(run)
