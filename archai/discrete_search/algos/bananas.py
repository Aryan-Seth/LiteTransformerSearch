# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from overrides import overrides
from typing import List, Dict, Union, Optional
from pathlib import Path

import numpy as np

from archai.common.utils import create_logger
from archai.discrete_search import (
    ArchaiModel,  SearchObjectives,
    SearchResults, get_non_dominated_sorting, evaluate_models
)
from archai.discrete_search import BayesOptSearchSpace, EvolutionarySearchSpace
from archai.discrete_search.api.predictor import Predictor, MeanVar
from archai.discrete_search.predictors import PredictiveDNNEnsemble
from archai.discrete_search.api.dataset import DatasetProvider
from archai.discrete_search.api.searcher import Searcher


class MoBananasSearch(Searcher):
    def __init__(self, output_dir: str,
                 search_space: BayesOptSearchSpace, 
                 search_objectives: SearchObjectives, 
                 dataset_provider: DatasetProvider,
                 surrogate_model: Optional[Predictor] = None,
                 num_iters: int = 10, init_num_models: int = 10,
                 num_parents: int = 10, mutations_per_parent: int = 5,
                 num_mutations: int = 10, seed: int = 1):

        assert isinstance(search_space, BayesOptSearchSpace)
        assert isinstance(search_space, EvolutionarySearchSpace)
        
        if surrogate_model:
            assert isinstance(surrogate_model, Predictor)
        else:
            surrogate_model = PredictiveDNNEnsemble()

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

        self.search_space = search_space
        self.dataset_provider = dataset_provider
        self.surrogate_model = surrogate_model

        # Objectives
        self.so = search_objectives

        # Algorithm parameters
        self.num_iters = num_iters
        self.init_num_models = init_num_models
        self.num_parents = num_parents
        self.mutations_per_parent = mutations_per_parent
        self.num_mutations = num_mutations

        # Utils
        self.logger = create_logger(str(self.output_dir / 'log.log'), enable_stdout=True) 
        self.seen_archs = set()
        self.seed = seed
        self.rng = np.random.RandomState(self.seed)
        self.surrogate_dataset = []
        self.search_state = SearchResults(search_space, search_objectives)

        
    def get_surrogate_iter_dataset(self, all_pop: List[ArchaiModel]):
        encoded_archs = np.vstack([self.search_space.encode(m) for m in all_pop])
        target = np.array([
            self.search_state.all_evaluated_objs[obj] 
            for obj in self.so.exp_objs
        ]).T

        return encoded_archs, target
    
    def sample_models(self, num_models: int, patience: int = 30) -> List[ArchaiModel]:
        nb_tries, valid_sample = 0, []

        while len(valid_sample) < num_models and nb_tries < patience:
            sample = [self.search_space.random_sample() for _ in range(num_models)]

            _, valid_indices = self.so.eval_constraints(sample, self.dataset_provider)
            valid_sample += [sample[i] for i in valid_indices]

        return valid_sample[:num_models]

    def mutate_parents(self, parents: List[ArchaiModel],
                       mutations_per_parent: int = 1,
                       patience: int = 30) -> List[ArchaiModel]:
        mutations = {}

        for p in parents:
            candidates = {}
            nb_tries = 0

            while len(candidates) < mutations_per_parent and nb_tries < patience:
                mutated_model = self.search_space.mutate(p)
                mutated_model.metadata['parent'] = p.archid

                if not self.so.check_model_valid(mutated_model, self.dataset_provider):
                    continue

                if mutated_model.archid not in self.seen_archs:
                    candidates[mutated_model.archid] = mutated_model

                nb_tries += 1

            mutations.update(candidates)
        
        if len(mutations) == 0:
            self.logger.warning(
                f'No mutations found after {patience} tries for each one of the {len(parents)} parents.'
            )

        return list(mutations.values())

    def predict_expensive_objectives(self, archs: List[ArchaiModel]) -> Dict[str, MeanVar]:
        ''' Predicts expensive objectives for `archs` using surrogate model ''' 
        encoded_archs = np.vstack([self.search_space.encode(m) for m in archs])
        pred_results = self.surrogate_model.predict(encoded_archs)
        
        return {
            obj_name: MeanVar(pred_results.mean[:, i], pred_results.var[:, i])
            for i, obj_name in enumerate(self.so.exp_objs)
        }

    def thompson_sampling(self, archs: List[ArchaiModel], sample_size: int,
                          pred_expensive_objs: Dict[str, MeanVar],
                          cheap_objs: Dict[str, np.ndarray]) -> List[int]:
        ''' Returns the selected architecture list indices from Thompson Sampling  '''                           
        simulation_results = cheap_objs

        # Simulates results from surrogate model assuming N(pred_mean, pred_std)
        simulation_results.update({
            obj_name: self.rng.randn(len(archs)) * np.sqrt(pred.var) + pred.mean
            for obj_name, pred in pred_expensive_objs.items()
        })

        # Performs non-dominated sorting
        nds_frontiers = get_non_dominated_sorting(archs, simulation_results, self.so)
        
        # Shuffle elements inside each frontier to avoid giving advantage to a specific
        # part of the nds frontiers
        for frontier in nds_frontiers:
            self.rng.shuffle(frontier['indices'])

        return [
            idx for frontier in nds_frontiers
            for idx in frontier['indices']
        ][:sample_size]

    @overrides
    def search(self):
        all_pop, selected_indices, pred_expensive_objs = [], [], {}
        unseen_pop = self.sample_models(self.init_num_models)

        for i in range(self.num_iters):
            self.logger.info(f'Starting iteration {i}')
            all_pop.extend(unseen_pop)

            self.logger.info(f'Evaluating objectives for {len(unseen_pop)} architectures')
            iter_results = self.so.eval_all_objs(unseen_pop, self.dataset_provider, progress_bar=True)

            self.seen_archs.update([m.archid for m in unseen_pop])
            
            # Adds iteration results and predictions from the previous iteration for comparison
            extra_model_data = {
                f'Predicted {obj_name} {c}': getattr(obj_results, c)[selected_indices]
                for obj_name, obj_results in pred_expensive_objs.items()
                for c in ['mean', 'var']
            }
            self.search_state.add_iteration_results(unseen_pop, iter_results, extra_model_data)

            # Updates surrogate
            self.logger.info('Updating surrogate model...')
            X, y = self.get_surrogate_iter_dataset(all_pop)
            self.surrogate_model.fit(X, y)

            # Selects top-`num_parents` models from non-dominated sorted results
            nds_frontiers = get_non_dominated_sorting(
                all_pop, self.search_state.all_evaluated_objs, self.so
            )
            parents = [model for frontier in nds_frontiers for model in frontier['models']]
            parents = parents[:self.num_parents]

            # Mutates top models
            self.logger.info(f'Generating mutations for {len(parents)} parent architectures...')
            mutated = self.mutate_parents(parents, self.mutations_per_parent)
            self.logger.info(f'Found {len(mutated)} new architectures satisfying constraints.')

            if not mutated:
                self.logger.info('No new architectures found. Stopping search.')
                break

            # Predicts expensive objectives using surrogate model 
            # and calculates cheap objectives for mutated architectures
            self.logger.info(f'Predicting {str(self.so.exp_objs)} for new architectures using surrogate model')
            pred_expensive_objs = self.predict_expensive_objectives(mutated)

            self.logger.info(f'Calculating cheap objectives {str(self.so.cheap_objs)} for new architectures')
            cheap_objs = self.so.eval_cheap_objs(mutated, self.dataset_provider)

            # Selects `num_mutations`-archtiectures for next iteration using Thompson Sampling
            selected_indices = self.thompson_sampling(
                mutated, self.num_mutations,
                pred_expensive_objs, cheap_objs
            )
            unseen_pop = [mutated[i] for i in selected_indices]
            
            self.logger.info(f'Best {self.num_mutations} candidate architectures were selected for the next iteration')

            # Save plots and reports
            self.search_state.save_all_2d_pareto_evolution_plots(self.output_dir)
            self.search_state.save_search_state(self.output_dir / f'search_state_{i}.csv')

        return self.search_state
