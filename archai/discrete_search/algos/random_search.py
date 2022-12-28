# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from overrides.overrides import overrides
from pathlib import Path
import random
from typing import List, Optional
from tqdm import tqdm

from archai.common.utils import create_logger
from archai.discrete_search import (
    ArchaiModel, DatasetProvider, Searcher, SearchObjectives, SearchResults
)

from archai.discrete_search.api.search_space import DiscreteSearchSpace


class RandomSearch(Searcher):
    def __init__(self, search_space: DiscreteSearchSpace, 
                 search_objectives: SearchObjectives, 
                 dataset_provider: DatasetProvider,
                 output_dir: str, num_iters: int = 10,
                 samples_per_iter: int = 10, 
                 seed: int = 1):
        
        assert isinstance(search_space, DiscreteSearchSpace), \
            f'{str(search_space.__class__)} is not compatible with {str(self.__class__)}'
        
        self.iter_num = 0
        self.search_space = search_space
        self.so = search_objectives
        self.dataset_provider = dataset_provider
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True, parents=True)

        # Algorithm settings
        self.num_iters = num_iters
        self.samples_per_iter = samples_per_iter

        # Utils
        self.search_state = SearchResults(search_space, self.so)
        self.seed = seed
        self.rng = random.Random(seed)
        self.evaluated_architectures = set()
        self.num_sampled_archs = 0
        self.logger = create_logger(str(self.output_dir / 'log.log'), enable_stdout=True)

        assert self.samples_per_iter > 0 
        assert self.num_iters > 0

    def sample_random_models(self, num_models: int) -> List[ArchaiModel]:
        return [self.search_space.random_sample() for _ in range(num_models)]

    @overrides
    def search(self) -> SearchResults:
        # sample the initial population
        self.iter_num = 0
        self.logger.info(
            f'Using {self.samples_per_iter} random architectures as the initial population'
        )
        unseen_pop = self.sample_random_models(self.samples_per_iter)

        self.all_pop = unseen_pop
        for i in range(self.num_iters):
            self.iter_num = i + 1
            self.logger.info(f'starting random search iter {i}')

            # Calculates objectives
            self.logger.info(
                f'iter {i}: calculating search objectives {str(self.objectives)} for'
                f' {len(unseen_pop)} models'
            )

            results = evaluate_models(unseen_pop, self.objectives, self.dataset_provider)
            self.search_state.add_iteration_results(
                unseen_pop, results,
            )

            # Records evaluated archs to avoid computing the same architecture twice
            self.evaluated_architectures.update([m.archid for m in unseen_pop])

            # update the pareto frontier
            self.logger.info(f'iter {i}: updating the pareto')
            pareto = self.search_state.get_pareto_frontier()['models']
            self.logger.info(f'iter {i}: found {len(pareto)} members')

            # Saves search iteration results
            self.search_state.save_search_state(
                str(self.output_dir / f'search_state_{self.iter_num}.csv')
            )

            self.search_state.save_pareto_frontier_models(
                str(self.output_dir / f'pareto_models_iter_{self.iter_num}')
            )

            self.search_state.save_all_2d_pareto_evolution_plots(str(self.output_dir))
            unseen_pop = self.sample_random_models(self.samples_per_iter)

            # update the set of architectures ever visited
            self.all_pop.extend(unseen_pop)

        return self.search_state
