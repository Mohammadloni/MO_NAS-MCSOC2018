import operator
from copy import deepcopy, copy
from threading import Thread
import _pickle as pickle, os
import numpy as np
import warnings
from keras.models import model_from_json

from tqdm import tqdm
import logging
import sys
#import matplotlib.pyplot as plt


# based on https://github.com/sg-nm/cgp-cnn/blob/master/
# but for deeper details I can recommend  http://www.cartesiangp.co.uk/
# especially the free chapter 1 and 2 from the CGP book

class Gen:
    """
    Base Class for Gen
    """

    def __init__(self, max_inputs):
        """
        Parameters
        ----------
        max_inputs: int
            max number of function parameters in the cgp grid

        """
        self.is_output = True
        self.inputs = np.zeros(max_inputs, dtype=int)
        self.num_inputs = 1


class OutputGen(Gen):
    """
    Representation of node which is an output gen
    """

    def __init__(self, max_inputs):
        """
        Parameters
        ----------
        max_inputs: int
            max number of function parameters in the cgp grid

        """
        super(OutputGen, self).__init__(max_inputs)


class FunctionGen(Gen):
    """
    Representation of a node containing a function gen
    """

    def __init__(self, fnc_idx, max_inputs, num_inputs):
        """
        Parameters
        ----------
        fnc_idx: int
            position in function set

        max_inputs: int
            max number of function parameters in the cgp grid

        num_inputs: int
            number of function parameters for the function at fnc_idx
        """
        super(FunctionGen, self).__init__(max_inputs)

        self.fnc_idx = fnc_idx
        self.inputs = np.zeros(max_inputs, dtype=int)
        self.num_inputs = num_inputs
        self.is_output = False

    def __str__(self):
        return "fnc_idx: %s with %s inputs" % (self.fnc_idx, self.inputs)


class CgpConfig:
    """
    configuration for a CGP problem
    """

    def __init__(self, rows=5, cols=10, level_back=5, functions=[], function_inputs=[],
                 constraints=None, mutation_rate=0.1):
        """
        Parameters
        ----------
        rows: int(5)
            number of rows of a cgp grid
        cols: int(10)
            number of cols of a cgp grid
        level_back: int(5)
            max level back
        functions: list(None)
            list of available functions for a function node
        function_inputs: list(None)
            list containing number of inputs for each available function
        constraints:
            not used at the moment
        mutation_rate: float(0.1)
            percentage value of how many genes will be mutating
        """
        if not isinstance(functions, list) or not isinstance(function_inputs, list):
            raise TypeError("functions and function_inputs must be a list")

        if not len(functions) == len(function_inputs):
            raise ValueError("function list must have the same dimension like function_inputs")

        self.rows = rows
        self.cols = cols
        self.level_back = level_back

        self.num_input = 1
        self.num_output = 1
        self.num_nodes = rows * cols
        self.functions = functions
        self.function_inputs = function_inputs
        self.num_func_genes = len(functions)
        self.max_inputs = np.max(function_inputs)
        self.contraints = constraints

        self.mutation_rate = mutation_rate


class Individual:
    """
    individual class containing genes which can be mutated
    basically in a evolution strategy it is called a parent and/or child
    """

    def __init__(self, config):
        """
        Parameters
        ----------
        config
        """
        if not isinstance(config, CgpConfig):
            raise ValueError("invalid value for config")

        self.config = config
        self.score = None
        self.model = None
        self.total_params = None
        self.genes = []
        self.active = None
        self.accuracy = None

    @classmethod
    def spawn(cls, config):
        """
        creates an instance of an individual and generates random gene connections

        Parameters
        ----------
        config: CgpConfig
            CGP configuration

        Returns
        -------
            an individual with random gene connections
        """
        individual = cls(config)
        individual.init_genes()
        return individual

    def init_genes(self):
        """
        creates random gene connections in the configured cartesian grid
        Returns
        -------

        """
        # init genes with a random function
        for node in range(self.config.num_nodes + self.config.num_output):
            if node < self.config.num_nodes:
                fnc_idx = np.random.randint(self.config.num_func_genes)
                gene = FunctionGen(fnc_idx,
                                   self.config.max_inputs,
                                   self.config.function_inputs[fnc_idx])
            else:
                gene = OutputGen(self.config.max_inputs)

            col = min(node // self.config.rows, self.config.cols)
            max_con_id = col * self.config.rows + self.config.num_input
            min_con_id = 0

            if col - self.config.level_back >= 0:
                min_con_id = (col - self.config.level_back) * self.config.rows + self.config.num_input

            # randomly connect genes
            for i in range(len(gene.inputs)):
                n = min_con_id + np.random.randint(max_con_id - min_con_id)
                gene.inputs[i] = n

            self.genes.append(gene)

        # lookup table for active nodes
        self.active = np.empty(len(self.genes), dtype=bool)
        self.check_active()

    def clone(self):
        """
        clones an individual
        Returns
        -------

        """
        instance = Individual(self.config)
        instance.active = self.active.copy()
        instance.genes = deepcopy(self.genes)
        instance.score = self.score
        instance.model = self.model
        instance.total_params = self.total_params
        instance.accuracy=self.accuracy

        return instance

    def __walk_to_out(self, node):
        if not self.active[node]:
            self.active[node] = True

            for i in range(self.genes[node].num_inputs):
                if self.genes[node].inputs[i] >= self.config.num_input:
                    self.__walk_to_out(self.genes[node].inputs[i] - self.config.num_input)

    def __mutate_function_gene(self, gene_idx):
        # if not self.active[gene_idx]:
        fnc_idx = np.random.randint(self.config.num_func_genes)
        while fnc_idx == self.genes[gene_idx].fnc_idx:
            fnc_idx = np.random.randint(self.config.num_func_genes)

        self.genes[gene_idx].fnc_idx = fnc_idx
        self.genes[gene_idx].num_inputs = self.config.function_inputs[fnc_idx]

        return True

    def __mutate_connection_gene(self, gene_idx):
        col = min(gene_idx // self.config.rows, self.config.cols)
        max_con_id = col * self.config.rows + self.config.num_input
        min_con_id = 0

        if col - self.config.level_back >= 0:
            min_con_id = (col - self.config.level_back) * self.config.rows + self.config.num_input

        for i in range(self.genes[gene_idx].num_inputs):
            if np.random.randint(0, 2) and max_con_id - min_con_id > 1:
                n = min_con_id + np.random.randint(max_con_id - min_con_id)
                while n == self.genes[gene_idx].inputs[i]:
                    n = min_con_id + np.random.randint(max_con_id - min_con_id)
                self.genes[gene_idx].inputs[i] = n

                return True

        return False

    def mutate(self, force=True):
        """
        mutates an individual based on the configured mutation rate

        Parameters
        ----------
        force: bool(True)
            forces to mutate. if nothing changed it tries to mutate until something changed

        Returns
        -------
        None
        """

        if not self.is_spawned():
            self.init_genes()

        if force:
            old_net = self.active.copy()

        cnt = 0
        num_genes = len(self.genes)
        while cnt <= int(num_genes * self.config.mutation_rate):
            idx = np.random.randint(0, num_genes)

            if idx < self.config.num_nodes:
                cnt += self.__mutate_function_gene(idx)

            # mutate connections
            cnt += self.__mutate_connection_gene(idx)

        self.check_active()

        if force:
            if np.array_equal(self.active, old_net):
                self.mutate(force)

    def check_active(self):
        """
        checks which node in the cartesian grid is active
        Returns
        -------

        """
        self.active[:] = False
        for node in range(self.config.num_output):
            self.__walk_to_out(self.config.num_nodes + node)

    def is_spawned(self):
        """
        checks whether a individual contains genes
        Returns
        -------
        bool
            True if an individual contains genes
        """
        return self.active is not None

    def num_active_nodes(self):
        """
        counts number of active genes in the cartesian grid

        Returns
        -------
        int
        """
        return len(np.where(self.active)[0])

    def active_net(self):
        """
        creates a decoded net structure

        Returns
        -------

        """
        net = [["input %d" % i, i, i] for i in range(self.config.num_input)]


        active_cnt = np.arange(self.config.num_nodes + self.config.num_output + self.config.num_input)
        active_cnt[self.config.num_input:] = np.cumsum(self.active)
        out_idx = 0
        for node, active in enumerate(self.active):
            if not active:
                continue

            con = [active_cnt[self.genes[node].inputs[i]] for i in range(self.genes[node].num_inputs)]
            if isinstance(self.genes[node], FunctionGen):
                fnc = self.config.functions[self.genes[node].fnc_idx]
                if hasattr(fnc, 'name'):
                    name = fnc.name
                else:
                    name = fnc.__name__
                net.append([name + "_id_%d" % len(net)] + con)
            else:
                net.append(['output-%d' % out_idx] + con)
                out_idx += 1

        return net


class CGP:
    """
    apply Cartesian Genetic Programming to your problem
    """

    def __init__(self, config, children=2, parent=None):
        """
        Parameters
        ----------
        config: CgpConfig
            configuration for cgp instance
        children: int(2)
            number of children for evolution strategy
        parent: str
            path to a parent which will be loaded as evolution start point
        """
        if not isinstance(config, CgpConfig):
            raise TypeError("config must be an instance of CgpConfig!")

        # create 1 parent + N childrens generations
        self.config = config
        self.num_children = children
        self.parent = self.load_parent(parent)

    def fast_non_dominated_sort(self, non_sort_genomes):
        S = [[] for i in range(0, len(non_sort_genomes))]
        front = [[]]
        n = [0 for i in range(0, len(non_sort_genomes))]
        rank = [0 for i in range(0, len(non_sort_genomes))]

        for p in range(0, len(non_sort_genomes)):
            S[p] = []
            n[p] = 0
            for q in range(0, len(non_sort_genomes)):
                if (non_sort_genomes[p].score > non_sort_genomes[q].score and non_sort_genomes[p].total_params <
                    non_sort_genomes[q].total_params) or (
                        non_sort_genomes[p].score >= non_sort_genomes[q].score and non_sort_genomes[p].total_params <
                        non_sort_genomes[q].total_params) or (
                        non_sort_genomes[p].score > non_sort_genomes[q].score and non_sort_genomes[p].total_params <=
                        non_sort_genomes[q].total_params):
                    if non_sort_genomes[q] not in S[p]:
                        S[p].append(non_sort_genomes[q])
                elif (non_sort_genomes[q].score > non_sort_genomes[p].score and non_sort_genomes[q].total_params <
                      non_sort_genomes[p].total_params) or (
                        non_sort_genomes[q].score >= non_sort_genomes[p].score and non_sort_genomes[q].total_params <
                        non_sort_genomes[p].total_params) or (
                        non_sort_genomes[q].score > non_sort_genomes[p].score and non_sort_genomes[q].total_params <=
                        non_sort_genomes[p].total_params):
                    n[p] = n[p] + 1
            if n[p] == 0:
                rank[p] = 0
                if non_sort_genomes[p] not in front[0]:
                    front[0].append(non_sort_genomes[p])

        i = 0
        while (front[i] != []):
            Q = []
            for p in front[i]:
                # for q in S[p]:
                for q in range(0, len(S)):
                    n[q] = n[q] - 1
                    if (n[q] == 0):
                        rank[q] = i + 1
                        if non_sort_genomes[q] not in Q:
                            Q.append(non_sort_genomes[q])
            i = i + 1
            front.append(Q)

        del front[len(front) - 1]
        return front

    def __evaluator_wrapper(self, evaluator, child, index, epoch):
        score, total_params, model1, accuracy1 = evaluator(child, index, epoch)
        child.score = score
        child.total_params = total_params
        child.model=model1
        child.accuracy=accuracy1

    def load_parent(self, filename):
        """
        loads an individual form which the evolution starts

        Parameters
        ----------
        filename: str
            path to the file which will be loaded

        Returns
        -------
        Individual
            loaded instance of an Individual
        """
        if filename is None or not os.path.exists(filename):
            return None

        with open(filename, 'rb') as f:
            instance = pickle.load(f)
            print("loaded file %s with score %.4f" % (filename, instance.score))

            if not isinstance(instance, Individual):
                warnings.warn('parent is not a valid Individual instance')
                return None

            return instance

    def run(self, evaluator, max_epochs=10, force_mutate=True, save_best=None, verbose=1):
        """
        starts evaluation and searching process for a problem

        Parameters
        ----------
        evaluator: function(child, child_index)
            function that will be called for each individual to rate it
            arguments are individual and an index
            the function must return the score of an passed individual
        max_epochs: int(10)
            max number of iterations to search for the best individual
        force_mutate: bool(true)
            forces to mutate. if true and nothing changed it tries to mutate until something changed
        save_best: str(None)
            if save_best is an path, the best child will be saved to it
        verbose: int(1)
            if 1 then a message will be printed after an evaluation


        Returns
        -------

        """

        if not callable(evaluator):
            raise TypeError("evaluator must be callable")

        if self.parent is None:

            if verbose > 0:
                print('%s\nevaluate parent\n%s' % ('#' * 100, '#' * 100))

            self.parent = Individual.spawn(self.config)

            self.__evaluator_wrapper(evaluator, self.parent, 0, 0)
        current_epoch = 0



        threads = [None] * self.num_children

        children = [None] * self.num_children

        while current_epoch < max_epochs:

            if verbose > 0:
                print('%s' % ('#' * 100))

                print("CGP Epoch %d of %d" % (current_epoch + 1, max_epochs))

            for i in range(self.num_children):
                mutated = self.parent.clone()

                mutated.mutate(force_mutate)

                #threads[i] = Thread(target=self.__evaluator_wrapper,args=(evaluator, mutated, i + 1, current_epoch + 1))
                self.__evaluator_wrapper(evaluator, mutated, i + 1, current_epoch + 1)
                children[i] = mutated

            #for t in threads:
             #   t.start()

            #for t in threads:
              #  t.join()

            #delete unsuitable children which do not present a network
            #children = self.childeren_delete(children)




            # Print Pareto in file
            error_i = []
            neurons_i = []
            for idx, child in enumerate(children):
                if (child.accuracy> 0):
                    neurons_i.append(child.total_params)
                    print(" Network Parameters: %d , accurracy : %.4f, , score : %.4f" % ((child.total_params), (child.accuracy), (child.score)))
                    error_i.append(1 - child.accuracy)
                    f = open(str(current_epoch) + 'mocga.txt', 'a')
                    f.write('\n' + '%d  :  %.4f ' % (child.total_params, (1 - child.accuracy)))
                    f.close()



            for idx, child in enumerate(children):
                if (child.score > 0):
                    if evaluator.trainer.comp( self.parent.score , child.score ):

                        if verbose:
                            print("child-%d %.2f has a better score than parent %.2f" % (idx, child.score, self.parent.score))

                            print('%s' % ('#' * 100))

                        self.parent = child

                        model_json = self.parent.model.to_json()
                        with open("model.json", "w") as json_file:
                            json_file.write(model_json)

                        evaluator.improved(idx + 1, child.score)

                        if save_best is not None:

                            p = os.path.abspath(os.path.dirname(save_best))

                            if not os.path.exists(p):
                                os.mkdir(p)

                            #with open(save_best, 'wb') as f:

                            #pickle.dump(self.parent, f)

                current_epoch += 1
