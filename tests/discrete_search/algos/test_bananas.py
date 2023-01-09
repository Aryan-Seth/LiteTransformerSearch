import pytest
from overrides import overrides
import os
import numpy as np

from archai.discrete_search.api.predictor import Predictor, MeanVar
from archai.discrete_search.algos.bananas import MoBananasSearch


@pytest.fixture(scope="session")
def output_dir(tmp_path_factory):
    return tmp_path_factory.mktemp('out_bananas')


def test_bananas(output_dir, search_space, search_objectives, surrogate_model):
    algo = MoBananasSearch(
        search_space=search_space, search_objectives=search_objectives, dataset_provider=None, 
        output_dir=output_dir, surrogate_model=surrogate_model,
        num_iters=30, init_num_models=20, mutations_per_parent=10,
        num_parents=10, num_mutations=15
    )
    
    search_results = algo.search()
    assert len(os.listdir(output_dir)) > 0
    
    df = search_results.get_search_state_df()
    assert all(0 <= x <= 0.4 for x in df['Random1'].tolist())
    
    all_models = [m for iter_r in search_results.results for m in iter_r['models']]

    # Checks if all registered models satisfy constraints
    _, valid_models = search_objectives.eval_constraints(all_models, None)
    assert len(valid_models) == len(all_models)
