 #!/usr/bin/python
from __future__ import division
from copy import copy

import numpy as np
import numpy.lib.recfunctions as rec

from collections import OrderedDict, defaultdict

import pandas as pd
import pymc as pm
import warnings

import kabuki
from copy import copy, deepcopy


# (defaultdict) param_name -> (defaultdict) column_names -> elems
#self.create_model(depends={'v':('col1')})

# defaultdict(lambda: defaultdict(lambda: ()))

class Knode(object):
    def __init__(self, pymc_node, name, depends=(), col_name='', subj=False, **kwargs):
        self.pymc_node = pymc_node
        self.name = name
        self.kwargs = kwargs
        self.subj = subj
        self.col_name = col_name
        self.nodes = OrderedDict()

        #create self.parents
        self.parents = {}
        for (name, value) in self.kwargs.iteritems():
            if isinstance(value, Knode):
                self.parents[name] = value

        # Create depends set and update based on parents' depends
        depends = set(depends)
        if self.subj:
            depends.add('subj_idx')
        depends.update(self.get_parent_depends())

        self.depends = sorted(list(depends))

        self.observed = 'observed' in kwargs

    def set_data(self, data):
        self.data = data

    def get_parent_depends(self):
        """returns the depends of the parents"""
        union_parent_depends = set()
        for name, parent in self.parents.iteritems():
            union_parent_depends.update(set(parent.depends))
        return union_parent_depends

    def init_nodes_db(self):
        data_col_names = list(self.data.columns)
        node_descriptors = ['knode_name', 'stochastic', 'observed', 'subj', 'node']
        stats = ['mean', 'std', '2.5q', '25q', '50q', '75q', '97.5q', 'mc err']

        columns = node_descriptors + data_col_names + stats

        # create central dataframe
        self.nodes_db = pd.DataFrame(columns=columns)

    def append_node_to_db(self, node, uniq_elem):
        #create db entry for knode
        line = {}
        line['knode_name'] = self.name
        line['observed'] = self.observed
        line['stochastic'] = isinstance(node, pm.Stochastic)
        line['subj'] = self.subj
        line['node'] = node

        line = pd.DataFrame(data=[line], columns=self.nodes_db.columns, index=[node.__name__])

        for dep, elem in zip(self.depends, uniq_elem):
            line[dep] = elem

        self.nodes_db = self.nodes_db.append(line)

    def create(self):
        """create the pymc nodes"""

        self.init_nodes_db()

        #group data
        if len(self.depends) == 0:
            grouped = [((), self.data)]
        else:
            grouped = self.data.groupby(self.depends)

        #create all the knodes
        for uniq_elem, grouped_data in grouped:

            if not isinstance(uniq_elem, tuple):
                uniq_elem = (uniq_elem,)

            # create new kwargs to pass to the new pymc node
            kwargs = self.kwargs.copy()

            # update kwarg with the right parent
            for name, parent in self.parents.iteritems():
                kwargs[name] = parent.get_node(self.depends, uniq_elem)

            #get node name
            node_name = self.create_node_name(self.depends, uniq_elem)

            #get value for observed node
            if self.observed:
                kwargs['value'] = grouped_data[self.col_name].values

            # Deterministic nodes require a parent argument that is a
            # dict mapping parent names to parent nodes. Knode wraps
            # this; so here we have to fish out the parent nodes from
            # kwargs, put them into a parent dict and put that back
            # into kwargs, which will make pm.Determinstic() get a
            # parent dict as an argument.
            if self.pymc_node is pm.Deterministic:
                parents_dict = {}
                for name, parent in self.parents.iteritems():
                    parents_dict[name] = parent.get_node(self.depends, uniq_elem)
                    kwargs.pop(name)
                kwargs['parents'] = parents_dict


            #actually create the node
            node = self.pymc_node(name=node_name, **kwargs)

            self.nodes[uniq_elem] = node

            self.append_node_to_db(node, uniq_elem)


    def create_node_name(self, cols, uniq_elem):
        cols = np.asarray(cols)
        uniq_elem = np.asarray(uniq_elem)

        if 'subj_idx' in cols:
            uniq_elem_wo_subj = uniq_elem[cols != 'subj_idx']
            elems_str = '.'.join([str(elem) for elem in uniq_elem_wo_subj])
            subj_idx = uniq_elem[cols == 'subj_idx'][0]
            return "{name}({elems}).{subj_idx}".format(name=self.name, elems=elems_str, subj_idx=subj_idx)
        else:
            elems_str = '.'.join([str(elem) for elem in uniq_elem])
            return "{name}({elems})".format(name=self.name, elems=elems_str)



    def get_node(self, cols, elems):
        """Return the node that depends on the same elements.

        Called by the child to receive specific parent node.

        :Arguments:
            col_to_elem : dict
                Maps column names to elements.
                e.g. {'col1': 'elem1', 'col2': 'elem2', 'col3': 'elem3'}
        """

        col_to_elem = {}
        for col, elem in zip(cols, elems):
            col_to_elem[col] = elem

        # Find the column names that overlap with the ones we have
        overlapping_cols = intersect(cols, self.depends)

        # Create new tag for the specific elements we are looking for (that overlap)
        deps_on_elems = tuple([col_to_elem[col] for col in overlapping_cols])

        return self.nodes[deps_on_elems]



# in Hierarchical: self.create_model(): for... knode.set_data(self.data); knode.create()

def intersect(t1, t2):
    # Preserves order, unlike set.
    return tuple([i for i in t2 if i in t1])

def test_subset_tuple():
    assert intersect(('a', 'b' , 'c'), ('a',)) == ('a',)
    assert intersect(('a', 'b' , 'c'), ('a', 'b')) == ('a', 'b')
    assert intersect(('a', 'b' , 'c'), ('a', 'c')) == ('a', 'c')
    assert intersect(('a', 'b' , 'c'), ('b', 'c')) == ('b', 'c')
    assert intersect(('c', 'b', 'a'), ('b', 'c')) == ('b', 'c')


class Hierarchical(object):
    """Creation of hierarchical Bayesian models in which each subject
    has a set of parameters that are constrained by a group distribution.

    :Arguments:
        data : numpy.recarray
            Input data with a row for each trial.
            Must contain the following columns:
              * 'rt': Reaction time of trial in seconds.
              * 'response': Binary response (e.g. 0->error, 1->correct)
            May contain:
              * 'subj_idx': A unique ID (int) of the subject.
              * Other user-defined columns that can be used in depends_on
                keyword.

    :Optional:
        include : tuple
            If the model has optional arguments, they
            can be included as a tuple of strings here.

        is_group_model : bool
            If True, this results in a hierarchical
            model with separate parameter distributions for each
            subject. The subject parameter distributions are
            themselves distributed according to a group parameter
            distribution.

        depends_on : dict
            Specifies which parameter depends on data
            of a column in data. For each unique element in that
            column, a separate set of parameter distributions will be
            created and applied. Multiple columns can be specified in
            a sequential container (e.g. list)

            :Example:

            >>> depends_on={'param1':['column1']}

            Suppose column1 has the elements 'element1' and
            'element2', then parameters 'param1('element1',)' and
            'param1('element2',)' will be created and the
            corresponding parameter distribution and data will be
            provided to the user-specified method get_liklihood().

        trace_subjs : bool
             Save trace for subjs (needed for many
             statistics so probably a good idea.)

        plot_var : bool
             Plot group variability parameters

        In addition, the variable self.params must be defined as a
        list of Paramater().

    """

    def __init__(self, data, is_group_model=None, depends_on=None, trace_subjs=True,
                 plot_subjs=False, plot_var=False, include=()):

        # Init
        self.include = set(include)

        self.mc = None

        self.data = pd.DataFrame(data)

        if not depends_on:
            depends_on = {}
        else:
            # Support for supplying columns as a single string
            # -> transform to list
            for key in depends_on:
                if isinstance(depends_on[key], str):
                    depends_on[key] = [depends_on[key]]
            # Check if column names exist in data
            for depend_on in depends_on.itervalues():
                for elem in depend_on:
                    if elem not in self.data.columns:
                        raise KeyError, "Column named %s not found in data." % elem


        self.depends = defaultdict(lambda: ())
        for key, value in depends_on.iteritems():
            self.depends[key] = value


        # Determine if group model
        if is_group_model is None:
            if 'subj_idx' in self.data.columns:
                if len(np.unique(data['subj_idx'])) != 1:
                    self.is_group_model = True
                else:
                    self.is_group_model = False
            else:
                self.is_group_model = False

        else:
            if is_group_model:
                if 'subj_idx' not in data.dtype.names:
                    raise ValueError("Group models require 'subj_idx' column in input data.")

            self.is_group_model = is_group_model

        # Should the model incorporate multiple subjects
        if self.is_group_model:
            self._subjs = np.unique(data['subj_idx'])
            self._num_subjs = self._subjs.shape[0]
        else:
            self._num_subjs = 1

        self.num_subjs = self._num_subjs

        # create knodes (does not build according pymc nodes)
        if self.is_group_model:
            self.knodes = self.create_knodes()
        else:
            self.knodes = self.create_knodes_single_subj()

        #add data to knodes
        for knode in self.knodes:
            knode.set_data(self.data)

        # constructs pymc nodes etc and connects them appropriately
        self.create_model()


    def create_knodes(self):
        raise NotImplementedError("create_knodes has to be overwritten")


    def create_knodes_single_subj(self):
        raise NotImplementedError("create_knodes_single_subj has to be overwritten")

    def create_model(self, max_retries=8):
        """Set group level distributions. One distribution for each
        parameter.

        :Arguments:
            retry : int
                How often to retry when model creation
                failed (due to bad starting values).
        """

        def _create():
            for knode in self.knodes:
                knode.create()

        for tries in range(max_retries):
            try:
                _create()
            except (pm.ZeroProbability, ValueError):
                continue
            break
        else:
            print "After %f retries, still no good fit found." %(tries)
            _create()

        # create node container
        self.create_nodes_db()


    def create_nodes_db(self):
        self.nodes_db = pd.concat([knode.nodes_db for knode in self.knodes])


    def map(self, runs=2, warn_crit=5, method='fmin_powell', **kwargs):
        """
        Find MAP and set optimized values to nodes.

        :Arguments:
            runs : int
                How many runs to make with different starting values
            warn_crit: float
                How far must the two best fitting values be apart in order to print a warning message

        :Returns:
            pymc.MAP object of model.

        :Note:
            Forwards additional keyword arguments to pymc.MAP().

        """

        from operator import attrgetter

        # I.S: when using MAP with Hierarchical model the subjects nodes should be
        # integrated out before the computation of the MAP (see Pinheiro JC, Bates DM., 1995, 2000).
        # since we are not integrating we get a point estimation for each
        # subject which is not what we want.
        if self.is_group_model:
            raise NotImplementedError("""Sorry, This method is not yet implemented for group models.
            you might consider using the subj_by_subj_map_init method""")


        maps = []

        for i in range(runs):
            # (re)create nodes to get new initival values.
            #nodes are not created for the first iteration if they already exist
            if i != 0:
                self.create_model()

            m = pm.MAP(self.nodes_db.node.values)
            m.fit(method, **kwargs)
            print m.logp
            maps.append(m)

        # We want to use values of the best fitting model
        sorted_maps = sorted(maps, key=attrgetter('logp'))
        max_map = sorted_maps[-1]

        # If maximum logp values are not in the same range, there
        # could be a problem with the model.
        if runs >= 2:
            abs_err = np.abs(sorted_maps[-1].logp - sorted_maps[-2].logp)
            if abs_err > warn_crit:
                print "Warning! Two best fitting MAP estimates are %f apart. Consider using more runs to avoid local minima." % abs_err

        # Set values of nodes
        for name, node in max_map._dict_container.iteritems():
            if isinstance(node, pm.ArrayContainer):
                for i,subj_node in enumerate(node):
                    if isinstance(node, pm.Node) and not subj_node.observed:
                        self.param_container.nodes[name][i].value = subj_node.value
            elif isinstance(node, pm.Node) and not node.observed:
                self.param_container.nodes[name].value = node.value

        return max_map


    def mcmc(self, assign_step_methods=True, *args, **kwargs):
        """
        Returns pymc.MCMC object of model.

        Input:
            assign_step_metheds <bool> : assign the step methods in params to the nodes

            The rest of the arguments are forwards to pymc.MCMC
        """

        self.mc = pm.MCMC(self.nodes_db.node.values, *args, **kwargs)

        if assign_step_methods:
            self.mcmc_step_methods()

        return self.mc


    def mcmc_step_methods(self):
        pass
        # TODO Imri


    def _assign_spx(self, param, loc, scale):
        """assign spx step method to param"""
        # TODO Imri
        # self.mc.use_step_method(kabuki.steps.SPXcentered,
        #                         loc=loc,
        #                         scale=scale,
        #                         loc_step_method=param.group_knode.step_method,
        #                         loc_step_method_args=param.group_knode.step_method_args,
        #                         scale_step_method=param.var_knode.step_method,
        #                         scale_step_method_args=param.var_knode.step_method_args,
        #                         beta_step_method=param.subj_knode.step_method,
        #                         beta_step_method_args=param.subj_knode.step_method_args)
        pass


    def sample(self, *args, **kwargs):
        """Sample from posterior.

        :Note:
            Forwards arguments to pymc.MCMC.sample().

        """

        # init mc if needed
        if self.mc == None:
            self.mcmc()

        # suppress annoying warnings
        if ('hdf5' in dir(pm.database)) and \
           isinstance(self.mc.db, pm.database.hdf5.Database):
            warnings.simplefilter('ignore', pm.database.hdf5.tables.NaturalNameWarning)

        # sample
        self.mc.sample(*args, **kwargs)

        return self.mc

    def dic_info(self):
        """returns information about the model DIC"""

        info = {}
        info['DIC'] = self.mc.dic
        info['deviance']  = np.mean(self.mc.db.trace('deviance')(), axis=0)
        info['pD'] = info['DIC'] - info['deviance']

        return info

    def _output_stats(self, stats_str, fname=None):
        """
        used by print_stats and print_group_stats to print the stats to the screen
        or to file
        """
        info = self.dic_info()
        if fname is None:
            print stats_str
            print "DIC: %f" % self.mc.dic
            print "deviance: %f" % info['deviance']
            print "pD: %f" % info['pD']
        else:
            with open(fname, 'w') as fd:
                fd.write(stats_str)
                fd.write("DIC: %f\n" % self.mc.dic)
                fd.write("deviance: %f\n" % info['deviance'])
                fd.write("pD: %f\n" % info['pD'])


    def print_stats(self, fname=None, **kwargs):
        """print statistics of all variables
        Input (optional)
            fname <string> - the output will be written to a file named fname
        """
        self.append_stats_to_nodes_db()

        sliced_db = self.nodes_db.copy()

        # only print stats of stochastic, non-observed nodes
        sliced_db = sliced_db[(sliced_db['stochastic'] == True) & (sliced_db['observed'] == False)]

        stat_cols  = ['mean', 'std', '2.5q', '25q', '50q', '75q', '97.5q', 'mc err']

        for node_property, value in kwargs.iteritems():
            sliced_db = sliced_db[sliced_db[node_property] == value]

        sliced_db = sliced_db[stat_cols]

        self._output_stats(sliced_db.to_string(), fname)


    def get_node(self, node_name, params):
        """Returns the node object with node_name from params if node
        is included in model, otherwise returns default value.

        """
        if node_name in self.include:
            return params[node_name]
        else:
            assert self.param_container.params_dict[node_name].default is not None, "Default value of not-included parameter not set."
            return self.param_container.params_dict[node_name].default


    def append_stats_to_nodes_db(self, *args, **kwargs):
        """
        smart call of MCMC.stats() for the model
        """
        try:
            nchains = self.mc.db.chains
        except AttributeError:
            raise ValueError("No model found.")

        #check which chain is going to be "stat"
        if 'chain' in kwargs:
            i_chain = kwargs['chain']
        else:
            i_chain = nchains

        #see if stats have been cached for this chain
        try:
            if self._stats_chain == i_chain:
                return
        except AttributeError:
            pass

        #update self._stats
        self._stats = self.mc.stats(*args, **kwargs)
        self._stats_chain = i_chain

        #add/overwrite stats to nodes_db
        for name, i_stats in self._stats.iteritems():
            self.nodes_db['mean'][name] = i_stats['mean']
            self.nodes_db['std'][name] = i_stats['standard deviation']
            self.nodes_db['2.5q'][name] = i_stats['quantiles'][2.5]
            self.nodes_db['25q'][name] = i_stats['quantiles'][25]
            self.nodes_db['50q'][name] = i_stats['quantiles'][50]
            self.nodes_db['75q'][name] = i_stats['quantiles'][75]
            self.nodes_db['97.5q'][name] = i_stats['quantiles'][97.5]
            self.nodes_db['mc err'][name] = i_stats['mc error']


    def load_db(self, dbname, verbose=0, db='sqlite'):
        """Load samples from a database created by an earlier model
        run (e.g. by calling .mcmc(dbname='test'))

        :Arguments:
            dbname : str
                File name of database
            verbose : int <default=0>
                Verbosity level
            db : str <default='sqlite'>
                Which database backend to use, can be
                sqlite, pickle, hdf5, txt.
        """


        if db == 'sqlite':
            db_loader = pm.database.sqlite.load
        elif db == 'pickle':
            db_loader = pm.database.pickle.load
        elif db == 'hdf5':
            db_loader = pm.database.hdf5.load
        elif db == 'txt':
            db_loader = pm.database.txt.load

        # Set up model
        if not self.param_container.nodes:
            self.create_nodes()

        # Ignore annoying sqlite warnings
        warnings.simplefilter('ignore', UserWarning)

        # Open database
        db = db_loader(dbname)

        # Create mcmc instance reading from the opened database
        self.mc = pm.MCMC(self.param_container.nodes, db=db, verbose=verbose)

        # Not sure if this does anything useful, but calling for good luck
        self.mc.restore_sampler_state()

        # Take the traces from the database and feed them into our
        # distribution variables (needed for _gen_stats())

        return self



    def init_from_existing_model(self, pre_model, assign_values=True, assign_step_methods=True,
                                 match=None, **mcmc_kwargs):
        """
        initialize the value and step methods of the model using an existing model
        Input:
            pre_model - existing mode

            assign_values (boolean) - should values of nodes from the existing model
                be assigned to the new model

            assign_step_method (boolean) - same as assign_values only for step methods

            match (dict) - dictionary which maps tags from the new model to tags from the
                existing model. match is a dictionary of dictionaries and it has
                the following structure:  match[name][new_tag] = pre_tag
                name is the parameter name. new_tag is the tag of the new model,
                and pre_tag is a single tag or list of tags from the exisiting model that will be map
                to the new_tag.
        """
        # TODO Imri
        raise NotImplementedError("TODO")
        if not self.mc:
            self.mcmc(assign_step_methods=False, **mcmc_kwargs)

        pre_d = pre_model.param_container.stoch_by_tuple
        assigned_s = 0; assigned_v = 0

        #set the new nodes
        for (key, node) in self.param_container.stoch_by_tuple.iteritems():
            name, h_type, tag, idx = key
            if name not in pre_model.param_container.params:
                continue

            #if the key was found then match_nodes assigns the old node value to the new node
            if pre_d.has_key(key):
                matched_nodes = [pre_d[key]]

            else: #match tags
                #get the matching pre_tags
                try:
                    pre_tags = match[name][tag]
                except TypeError, AttributeError:
                    raise ValueError('match argument does not have the coorect name or tag')

                if type(pre_tags) == str:
                    pre_tags = [pre_tags]

                #get matching nodes
                matched_nodes = [pre_d[(name, h_type, x, idx)] for x in pre_tags]

            #average matched_nodes values
            if assign_values:
                node.value = np.mean([x.value for x in matched_nodes])
                assigned_v += 1

            #assign step method
            if assign_step_methods:
                assigned_s += self._assign_step_methods_from_existing(node, pre_model, matched_nodes)

        print "assigned %d values (out of %d)." % (assigned_v, len(self.mc.stochastics))
        print "assigned %d step methods (out of %d)." % (assigned_s, len(self.mc.stochastics))


    def _assign_step_methods_from_existing(self, node, pre_model, matched_nodes):
        """
        private funciton used by init_from_existing_model to assign a node
        using matched_nodes from pre_model
        Output:
             assigned (boolean) - step method was assigned

        """

        if isinstance(matched_nodes, pm.Node):
            matched_node = [matched_nodes]

        #find the step methods
        steps = [pre_model.mc.step_method_dict[x][0] for x in matched_nodes]

        #only assign it if it's a Metropolis
        if isinstance(steps[0], pm.Metropolis):
            pre_sd = np.median([x.proposal_sd * x.adaptive_scale_factor for x in steps])
            self.mc.use_step_method(pm.Metropolis, node, proposal_sd = pre_sd)
            assigned = True
        else:
            assigned = False

        return assigned

    def plot_posteriors(self, parameters=None, plot_subjs=False, **kwargs):
        """
        plot the nodes posteriors
        Input:
            parameters (optional) - a list of parameters to plot.
            plot_subj (optional) - plot subjs nodes

        TODO: add attributes plot_subjs and plot_var to kabuki
        which will change the plot attribute in the relevant nodes
        """

        if parameters is None: #plot the model
            pm.Matplot.plot(self.mc, **kwargs)

        else: #plot only the given parameters

            if not isinstance(parameters, list):
                 parameters = [parameters]

            #get the nodes which will be plotted
            for param in parameters:
                nodes = tuple(np.unique(param.group_nodes.values() + param.var_nodes.values()))
                if plot_subjs:
                    for nodes_array in param.subj_nodes.values():
                        nodes += list(nodes_array)
            #this part does the ploting
            for node in nodes:
                plot_value = node.plot
                node.plot = True
                pm.Matplot.plot(node, **kwargs)
                node.plot = plot_value

    def subj_by_subj_map_init(self, runs=2, verbose=1, **map_kwargs):
        """
        initializing nodes by finding the MAP for each subject separately
        Input:
            runs - number of MAP runs for each subject
            map_kwargs - other arguments that will be passes on to the map function

        Note: This function should be run prior to the nodes creation, i.e.
        before running mcmc() or map()
        """

        #init
        subjless = {}
        subjs = self._subjs
        n_subjs = len(subjs)
        empty_s_model = deepcopy(self)
        empty_s_model.is_group_model = False
        del empty_s_model.num_subjs, empty_s_model._subjs, empty_s_model.data

        self.create_nodes()

        # loop over subjects
        for i_subj in range(n_subjs):
            # create and fit single subject
            if verbose > 0: print "*!*!* fitting subject %d *!*!*" % subjs[i_subj]
            t_data = self.data[self.data['subj_idx'] == subjs[i_subj]]
            s_model = deepcopy(empty_s_model)
            s_model.data = t_data
            s_model.map(method='fmin_powell', runs=runs, **map_kwargs)

            # copy to original model
            for (name, node) in s_model.param_container.iter_group_nodes():
                #try to assign the value of the node to the original model
                try:
                    self.param_container.subj_nodes[name][i_subj].value = node.value
                #if it fails it mean the param has no subj nodes
                except KeyError:
                    if subjless.has_key(name):
                        subjless[name].append(node.value)
                    else:
                        subjless[name] = [node.value]

        #set group and var nodes for params with subjs
        for (param_name, param) in self.param_container.iter_params():
            #if param has subj nodes than compute group and var nodes from them
            if param.has_subj_nodes:
                for (tag, nodes) in param.subj_nodes.iteritems():
                    subj_values = [x.value for x in nodes]
                    #set group node
                    if param.has_group_nodes:
                        param.group_nodes[tag].value = np.mean(subj_values)
                    #set var node
                    if param.has_var_nodes:
                        param.var_nodes[tag].value = param.var_func(subj_values)

        #set group nodes of subjless nodes
        for (name, values) in subjless.iteritems():
            self.param_container.group_nodes[name].value = np.mean(subjless[name])
