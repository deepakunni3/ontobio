from ontobio.sim.api.interfaces import SimApi, InformationContentStore, FilteredSearchable
from ontobio.config import get_config
from ontobio.vocabulary.upper import HpoUpperLevel
from ontobio.ontol_factory import OntologyFactory
from ontobio.model.similarity import IcStatistic, SimResult, Node, SimMatch, SimQuery, PairwiseMatch, ICNode
from ontobio.vocabulary.similarity import SimAlgorithm
from ontobio.util.scigraph_util import get_nodes_from_ids, get_id_type_map

from typing import List, Optional, Dict, Tuple, Union
from json.decoder import JSONDecodeError
import logging
from cachier import cachier
import datetime
import requests


class OwlSim2Api(SimApi, InformationContentStore, FilteredSearchable):
    """
    Owlsim2 is part of the owltools package and uses a modified
    version of the phenodigm algorithm to compute semantic similarity,
    using IC instead of the geometric mean of IC and jaccard similarities

    Resnik, jaccard, and SimGIC are also available alongside the phenodigm
    score

    This service drives phenogrid, annotation sufficiency, and analyze phenotypes
    on the monarch web application

    refs:
      code: https://github.com/owlcollab/owltools/tree/master/OWLTools-Sim
      phendigm: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3649640/
      simGIC:  https://bmcbioinformatics.biomedcentral.com/articles/
               10.1186/1471-2105-9-S5-S4

    The default monarch instance computes similarity over phenotype profiles,
    using upheno as the backing ontology and hpoa for disease-phenotype
    annotations, model organism databases for gene-phenotype, and case
    data from the UDP
    """

    """
    Dictionary that contains taxon to namespace mappings
    for owlsim2 which requires namespace for filtering
    """
    TAX_TO_NS = {
        10090: {
            'gene': 'MGI'
        },
        9606: {
            'disease': 'MONDO',
            'case': 'MONARCH',
            'gene': 'HGNC'
        },
        7227: {
            'gene': 'FlyBase'
        },
        6239: {
            'gene': 'WormBase'
        },
        7955: {
            'gene': 'ZFIN'
        }
    }

    human_tax = Node(
        id='NCBITaxon:9606',
        label='Homo sapiens'
    )

    # This can be replaced if taxon becomes a node property
    NS_TO_TAX = {
        'MGI': Node(
            id='NCBITaxon:10090',
            label='Mus musculus'
        ),
        'MONDO': human_tax,
        'OMIM': human_tax,
        'MONARCH': human_tax,
        'HGNC': human_tax,
        'FlyBase': Node(
            id='NCBITaxon:7227',
            label='Drosophila melanogaster'
        ),
        'WormBase': Node(
            id='NCBITaxon:6239',
            label='Caenorhabditis elegans'
        ),
        'ZFIN': Node(
            id='NCBITaxon:7955',
            label='Danio rerio'
        )
    }

    method2key = {
        SimAlgorithm.PHENODIGM: 'combinedScore',
        SimAlgorithm.JACCARD: 'simJ',
        SimAlgorithm.SIM_GIC: 'simGIC',
        SimAlgorithm.RESNIK: 'bmaAsymIC',
        SimAlgorithm.SYMMETRIC_RESNIK: 'bmaSymIC',
    }

    def __init__(self, url: Optional[str]=None, timeout: Optional[int]=None):
        self.url = url if url is not None else get_config().owlsim2.url
        self.timeout = timeout if timeout is not None else get_config().owlsim2.timeout

        # Init ic stats
        stats = self._get_owlsim_stats()
        self._statistics = stats[0]
        self._category_statistics = stats[1]

    @property
    def statistics(self) -> IcStatistic:
        return self._statistics

    @statistics.setter
    def statistics(self, value: IcStatistic):
        self._statistics = value

    @property
    def category_statistics(self):
        return self._category_statistics

    @category_statistics.setter
    def category_statistics(self, value: Dict[str, IcStatistic]):
        self._category_statistics = value

    def search(
            self,
            id_list: List,
            negated_classes: List,
            method: Optional[SimAlgorithm] = SimAlgorithm.PHENODIGM) -> SimResult:
        """
        Owlsim2 search, calls search_by_attribute_set, and converts to SimResult object

        :raises JSONDecodeError: If the owlsim response is not valid json.
        """

        return self.filtered_search(
            id_list = id_list,
            negated_classes = negated_classes,
            taxon_filter = None,
            category_filter = None,
            method = method
        )

    def compare(self,
                reference_classes: List,
                query_classes: List,
                method: Optional[SimAlgorithm] = SimAlgorithm.PHENODIGM) -> SimResult:
        """
        Owlsim2 compare, calls compare_attribute_sets, and converts to SimResult object
        :return: SimResult object
        """
        owlsim_results = self.compare_attribute_sets(reference_classes, query_classes)
        return OwlSim2Api._simsearch_to_simresult(owlsim_results, method, 'compare')

    def filtered_search(
            self,
            id_list: List,
            negated_classes: List,
            taxon_filter: Optional[int],
            category_filter: Optional[str],
            method: Optional[SimAlgorithm] = SimAlgorithm.PHENODIGM) -> SimResult:
        """
        Owlsim2 filtered search, resolves taxon and category to a namespace,
        calls search_by_attribute_set, and converts to SimResult object
        """
        if len(negated_classes) > 0:
            logging.warning("Owlsim2 does not support negation, ignoring neg classes")

        namespace_filter = self._get_namespace_filter(taxon_filter, category_filter)
        owlsim_results = self.search_by_attribute_set(id_list, namespace_filter)
        return OwlSim2Api._simsearch_to_simresult(owlsim_results, method, 'search')

    @staticmethod
    def matchers() -> List[SimAlgorithm]:
        """
        Matchers in owlsim2
        """
        return [
            SimAlgorithm.PHENODIGM,
            SimAlgorithm.JACCARD,
            SimAlgorithm.SIM_GIC,
            SimAlgorithm.RESNIK,
            SimAlgorithm.SYMMETRIC_RESNIK
        ]

    def get_profile_ic(self, profile: List) -> Dict:
        """
        Given a list of individuals, return their information content
        """
        sim_response = self.get_attribute_information_profile(profile)

        profile_ic = {}
        try:
            for cls in sim_response['input']:
                profile_ic[cls['id']] = cls['IC']
        except JSONDecodeError as exc_msg:
            raise JSONDecodeError("Cannot parse owlsim2 response: {}".format(exc_msg))

        return profile_ic

    def search_by_attribute_set(
            self,
            profile: List[str],
            namespace_filter: Optional[str]=None) -> Dict:
        """
        Given a list of phenotypes, returns a ranked list of individuals
        individuals can be filtered by namespace, eg MONDO, MGI, HGNC
        :returns Dict with the structure: {
          'unresolved' : [...]
          'query_IRIs' : [...]
          'results': {...}
        }
        :raises JSONDecodeError: If the response body does not contain valid json.
        """
        owlsim_url = self.url + 'searchByAttributeSet'

        params = {
            'a': profile,
            'target': namespace_filter
        }
        return requests.get(owlsim_url, params=params, timeout=self.timeout).json()

    def compare_attribute_sets(
            self,
            profile_a: List[str],
            profile_b: List[str]) -> Dict:
        """
        Given two phenotype profiles, returns their similarity
        :returns Dict with the structure: {
          'unresolved' : [...]
          'query_IRIs' : [...]
          'target_IRIs': [...]
          'results': {...}
        }
        """
        owlsim_url = self.url + 'compareAttributeSets'

        params = {
            'a': profile_a,
            'b': profile_b,
        }
        return requests.get(owlsim_url, params=params, timeout=self.timeout).json()

    def get_attribute_information_profile(
            self,
            profile: Optional[List[str]]=None,
            categories: Optional[List[str]]=None) -> Dict:
        """
        Get the information content for a list of phenotypes
        and the annotation sufficiency simple and
        and categorical scores if categories are provied

        Ref: https://zenodo.org/record/834091#.W8ZnCxhlCV4
        Note that the simple score varies slightly from the pub in that
        it uses max_max_ic instead of mean_max_ic

        If no arguments are passed this function returns the
        system (loaded cohort) stats
        :raises JSONDecodeError: If the response body does not contain valid json.
        """
        owlsim_url = self.url + 'getAttributeInformationProfile'

        params = {
            'a': profile,
            'r': categories
        }
        return requests.get(owlsim_url, params=params, timeout=self.timeout).json()

    @staticmethod
    def _simsearch_to_simresult(
            sim_resp: Dict,
            method: SimAlgorithm,
            mode: str) -> SimResult:
        """
        Convert owlsim json to SimResult object

        :param sim_resp: owlsim response from search_by_attribute_set()
                        or compare_attribute_sets()
        :param method: SimAlgorithm
        :param mode: str, search or compare
        :return: SimResult object
        """

        sim_ids = get_nodes_from_ids(sim_resp['query_IRIs'])
        sim_resp['results'] = OwlSim2Api._rank_results(sim_resp['results'], method)

        # get id type map:
        ids = [result['j']['id'] for result in sim_resp['results']]
        id_type_map = get_id_type_map(ids)

        matches = []

        for result in sim_resp['results']:
            ns = result['j']['id'].split(":")[0]
            if ns in OwlSim2Api.NS_TO_TAX:
                taxon = OwlSim2Api.NS_TO_TAX[ns]
            else:
                taxon = {}
            matches.append(
                SimMatch(
                    id=result['j']['id'],
                    label=result['j']['label'],
                    rank=result['rank'] if mode == 'search' else "NaN",
                    score=result[OwlSim2Api.method2key[method]],
                    type=id_type_map[result['j']['id']][0],
                    taxon=taxon,
                    significance="NaN",
                    pairwise_match=OwlSim2Api._make_pairwise_matches(result)
                )
            )

        if mode == "compare":
            target_ids = [get_nodes_from_ids(sim_resp['target_IRIs'])]
        else:
            target_ids = [[]]

        return SimResult(
            query=SimQuery(
                ids=sim_ids,
                unresolved_ids=sim_resp['unresolved'],
                target_ids=target_ids
            ),
            matches=matches
        )

    @staticmethod
    def _make_pairwise_matches(result: Dict) -> List[PairwiseMatch]:
        """
        Make a list of match object from owlsim results
        :param result: Single owlsim result
        :return: List of SimMatch objects
        """
        pairwise_matches = []
        for pairwise_match in result['matches']:
            pairwise_matches.append(
                PairwiseMatch(
                    query=ICNode(**pairwise_match['a']),
                    match=ICNode(**pairwise_match['b']),
                    lcs=ICNode(**pairwise_match['lcs'])
                )
            )

        return pairwise_matches

    @staticmethod
    def _rank_results(results: List[Dict], method: SimAlgorithm) -> List[Dict]:
        """
        Ranks results - for phenodigm results are ranks but ties need to accounted for
                        for other methods, results need to be reranked

        :param results: Results from search_by_attribute_set()['results'] or
                                     compare_attribute_sets()['results']
        :param method: sim method used to rank results
        :return: Sorted results list
        """
        # https://stackoverflow.com/a/73050
        sorted_results = sorted(
            results, reverse=True, key=lambda k: k[OwlSim2Api.method2key[method]]
        )
        rank = 1
        previous_score = sorted_results[0][OwlSim2Api.method2key[method]]
        for result in sorted_results:
            if previous_score > result[OwlSim2Api.method2key[method]]:
                rank += 1
            result['rank'] = rank
            previous_score = result[OwlSim2Api.method2key[method]]

        return sorted_results

    @staticmethod
    def _get_namespace_filter(
            taxon_filter: Optional[int]=None,
            category_filter: Optional[str]=None) -> Union[None, str]:
        """
        Given either a taxon and/or category, return the correct namespace
        :raises ValueError: If category is provided without a taxon
        """
        namespace_filter = None
        taxon_category_default = {
            10090: 'gene',
            9606:  'disease',
            7227:  'gene',
            6239:  'gene',
            7955:  'gene'
        }
        if category_filter is not None and taxon_filter is None:
            raise ValueError("Must provide taxon filter along with category")
        elif category_filter is None and taxon_filter is not None:
            category_filter = taxon_category_default[taxon_filter]
        else:
            return namespace_filter

        return OwlSim2Api.TAX_TO_NS[taxon_filter][category_filter.lower()]

    @cachier(datetime.timedelta(days=30))
    def _get_owlsim_stats(self) -> Tuple[IcStatistic, Dict[str, IcStatistic]]:
        """
        :return Tuple[IcStatistic, Dict[str, IcStatistic]]
        :raises JSONDecodeError: If the response body does not contain valid json
        """
        scigraph = OntologyFactory().create('scigraph:ontology')
        category_stats = {}
        categories = [enum.value for enum in HpoUpperLevel]
        sim_response = self.get_attribute_information_profile(categories=categories)

        try:
            global_stats = IcStatistic(
                mean_mean_ic=float(  sim_response['system_stats']['meanMeanIC']),
                mean_sum_ic= float(  sim_response['system_stats']['meanSumIC']),
                mean_cls=    float(  sim_response['system_stats']['meanN']),
                max_max_ic=  float(  sim_response['system_stats']['maxMaxIC']),
                max_sum_ic=  float(  sim_response['system_stats']['maxSumIC']),
                individual_count=int(sim_response['system_stats']['individuals']),
                mean_max_ic= float(  sim_response['system_stats']['meanMaxIC'])
            )
            for cat_stat in sim_response['categorical_scores']:
                category_stats[cat_stat['id']] = IcStatistic(
                    mean_mean_ic=float(  cat_stat['system_stats']['meanMeanIC']),
                    mean_sum_ic= float(  cat_stat['system_stats']['meanSumIC']),
                    mean_cls=    float(  cat_stat['system_stats']['meanN']),
                    max_max_ic=  float(  cat_stat['system_stats']['maxMaxIC']),
                    max_sum_ic=  float(  cat_stat['system_stats']['maxSumIC']),
                    individual_count=int(cat_stat['system_stats']['individuals']),
                    mean_max_ic= float(  cat_stat['system_stats']['meanMaxIC']),
                    descendants=scigraph.descendants(cat_stat['id'], relations=["subClassOf"])
                )

        except JSONDecodeError as exc_msg:
            raise JSONDecodeError("Cannot parse owlsim2 response: {}".format(exc_msg))

        return global_stats, category_stats

    def __str__(self):
        return "owlsim2 api: {}".format(self.url)
