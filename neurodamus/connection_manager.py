"""
Main module for handling and instantiating synaptical connections and gap-junctions
"""
from __future__ import absolute_import
import hashlib
import logging
import numpy
from abc import abstractmethod
from collections import defaultdict
from itertools import chain
from os import path as ospath

from .core import NeurodamusCore as Nd
from .core import ProgressBarRank0 as ProgressBar, MPI
from .core.configuration import GlobalConfig, SimConfig, ConfigurationError
from .connection import Connection, SynapseMode, ReplayMode
from .io.cell_readers import TargetSpec
from .io.synapse_reader import SynapseReader
from .utils import compat, bin_search, dict_filter_map
from .utils.logging import log_verbose, log_all
from .utils.timeit import timeit


class ConnectionSet(object):
    """
    A dataset of connections.
    Several populations may exist with different seeds
    """
    def __init__(self, src_id, dst_id, conn_factory=Connection):
        # Connections indexed by post-gid, then ordered by pre-gid
        self.src_id = src_id
        self.dst_id = dst_id
        self.src_name = None
        self.dst_name = None
        self._conn_factory = conn_factory
        self._connections_map = defaultdict(list)
        self._conn_count = 0

    def __contains__(self, item):
        return item in self._connections_map

    def __getitem__(self, item):
        return self._connections_map[item]

    def get(self, item):
        return self._connections_map.get(item)

    def items(self):
        """Iterate over the population as tuples (dst_gid, [connections])"""
        return self._connections_map.items()

    def target_gids(self):
        """Get the list of all targets gids in this Population"""
        return self._connections_map.keys()

    def all_connections(self):
        """Get an iterator over all the connections."""
        return chain.from_iterable(self._connections_map.values())

    def _find_connection(self, sgid, tgid, exact=True):
        """Finds a connection, given its source and destination gids.

        Returns:
            tuple: connection list and index.
                If the element doesnt exist, index depends on 'exact':
                None if exact=True, otherwise the insertion index.
        """
        cell_conns = self._connections_map[tgid]
        pos = 0
        if cell_conns:
            pos = bin_search(cell_conns, sgid, lambda x: x.sgid)
        if exact and (pos == len(cell_conns) or cell_conns[pos].sgid != sgid):
            # Not found
            return cell_conns, None
        return cell_conns, pos

    # -
    def get_connection(self, sgid, tgid):
        """Retrieves a connection from the pre and post gids.

        Returns:
            Connection: A connection object if it exists. None otherwise
        """
        conn_lst, idx = self._find_connection(sgid, tgid)
        return None if idx is None else conn_lst[idx]

    # -
    def store_connection(self, conn):
        """When we have created a new connection (sgid->tgid), store it
        in order in our structure for faster retrieval later

        Args:
            conn: The connection object to be stored
        """
        cell_conns, pos = self._find_connection(conn.sgid, conn.tgid, exact=False)
        if cell_conns and pos < len(cell_conns) and cell_conns[pos].sgid == conn.sgid:
            logging.error("Attempt to store existing connection: %d->%d",
                          conn.sgid, conn.tgid)
            return
        self._conn_count += 1
        cell_conns.insert(pos, conn)

    # -
    def get_or_create_connection(self, sgid, tgid, **kwargs):
        """Returns a connection by pre-post gid, creating if required."""
        conns = self._connections_map[tgid]
        pos = 0
        if conns:
            # optimize for ordered insertion, and handle when sgid is not used
            last_conn = conns[-1]
            if last_conn.sgid in (sgid, None):
                return last_conn
            if last_conn.sgid < sgid:
                pos = len(conns)
            else:
                pos = bin_search(conns, sgid, lambda x: x.sgid)
                if conns[pos].sgid == sgid:
                    return conns[pos]
        # Not found. Create & insert
        cur_conn = self._conn_factory(sgid, tgid, self.src_id, self.dst_id, **kwargs)
        conns.insert(pos, cur_conn)
        self._conn_count += 1
        return cur_conn

    # -
    def get_connections(self, post_gids, pre_gids=None):
        """Get all connections between groups of gids."""
        if isinstance(post_gids, int):
            if pre_gids is None:
                return self._connections_map[post_gids]
            elif isinstance(pre_gids, int):
                elem = self.get_connection(pre_gids, post_gids)
                return (elem,) if elem is not None else ()

        post_gid_conn_lists = (
            self._connections_map.values() if post_gids is None
            else (self._connections_map[post_gids],) if isinstance(post_gids, int)
            else (self._connections_map[tgid] for tgid in post_gids)
        )
        if pre_gids is None:
            return chain.from_iterable(post_gid_conn_lists)
        elif isinstance(pre_gids, int):
            # Return a generator which is employing bin search
            return (conns[posi] for conns in post_gid_conn_lists
                    for posi in (bin_search(conns, pre_gids, lambda x: x.sgid),)
                    if posi < len(conns) and conns[posi].sgid == pre_gids)
        else:
            # Generic case. Looks through all conns in selected tgids
            pre_gids = set(pre_gids)
            return (c for conns in post_gid_conn_lists
                    for c in conns
                    if c.sgid in pre_gids)

    def get_synapse_params_gid(self, target_gid):
        """Get an iterator over all the synapse parameters of a target
        cell connections.
        """
        conns = self._connections_map[target_gid]
        return chain.from_iterable(c.synapse_params for c in conns)

    def delete(self, sgid, tgid):
        """Removes a given connection from the population."""
        conn_lst, idx = self._find_connection(sgid, tgid)
        if idx is None:
            logging.error("Non-existing connection to delete: %d->%d", sgid, tgid)
            return
        self._conn_count -= 1
        del conn_lst[idx]

    def delete_group(self, post_gids, pre_gids=None):
        """Removes a set of connections from the population."""
        for conns, indices in self._find_connections(post_gids, pre_gids):
            conns[:] = numpy.delete(conns, indices, axis=0).tolist()
            self._conn_count -= len(indices)

    def count(self):
        return self._conn_count

    # -
    def _find_connections(self, post_gids, pre_gids=None):
        """Get the indices of the connections between groups of gids"""
        post_gid_conn_lists = (
            self._connections_map.values() if post_gids is None
            else (self._connections_map[post_gids],) if isinstance(post_gids, int)
            else (self._connections_map[tgid] for tgid in post_gids)
        )

        if pre_gids is None:
            return ((conns, range(len(conns))) for conns in post_gid_conn_lists)

        sgids_interest = [pre_gids] if isinstance(pre_gids, int) else pre_gids
        return (
            (conns, numpy.searchsorted(
                numpy.fromiter((c.sgid for c in conns), dtype="int64", count=len(conns)),
                sgids_interest))
            for conns in post_gid_conn_lists
        )

    def ids_match(self, population_ids, dst_second=None):
        """Whereas a given population_id selector matches population
        """
        if isinstance(population_ids, tuple):
            expr_src, expr_dst = population_ids
        else:
            expr_src, expr_dst = (population_ids, dst_second)
        return ((expr_src is None or expr_src == self.src_id) and
                (expr_dst is None or expr_dst == self.dst_id))

    def is_default(self):
        return self.src_id == 0 and self.dst_id == 0

    def __str__(self):
        if self.is_default():
            return "<ConnectionSet: Default>"
        return "<ConnectionSet: SrcID: %d (%s->%s)>" % (self.src_id, self.src_name, self.dst_name)


class ConnectionManagerBase(object):
    """
    An abstract base class common to Synapse and GapJunction connections
    """

    CIRCUIT_FILENAMES = None
    """The possible circuit filenames specificed in search order"""
    CONNECTIONS_TYPE = None
    """The type of connections subclasses handle"""

    # Set depending Classes, customizable
    ConnectionSet = ConnectionSet
    SynapseReader = SynapseReader
    conn_factory = Connection

    # We declare class variables which might be used in subclass methods
    _circuit_target = None  # gaj junctions require the target obj to validate src cells
    _synapse_mode = SynapseMode.default

    cell_manager = property(lambda self: self._cell_manager)
    is_file_open = property(lambda self: bool(self._synapse_reader))

    # -
    def __init__(self, circuit_conf, target_manager, cell_manager, base_manager=None):
        """Base class c-tor for connections (Synapses & Gap-Junctions)

        Args:
            circuit_conf: A circuit config object where to get the synapse source from
                or None if the ConnecionManager is to be constructed empty
            target_manager: A target manager, where to query target cells
            cell_distributor: Query the local gids
            base_manager: In other engines, this is the base engine cell manager
        """
        self._target_manager = target_manager
        self._cell_manager = cell_manager

        self._base_population = None  # the node population name that base connectivity targets
        self._populations = {}  # Multiple edge populations support. key is a tuple (src, dst)
        self._populations_by_src_nodes = defaultdict(list)  # find edge populations by src nodes
        self._cur_population = self.get_population(0, 0)  # Instantiate a default pop
        self._disabled_conns = defaultdict(list)

        self._synapse_reader = None
        self._local_gids = cell_manager.local_gids
        self._total_connections = 0
        self.circuit_conf = circuit_conf
        self.base_manager = base_manager
        self.has_syn_indexes = False

        if GlobalConfig.debug_conn:
            logging.info("Debugging activated for cell/conn %s", GlobalConfig.debug_conn)

    # -
    def init_synapse_location(self, nrn_path, circuit_conf, load_offsets=False):
        """Initializes the reader from compat nrn_path values and sets base population
        """
        logging.info("Initialize cell manager from nrnPath=%s", nrn_path)
        n_synapse_files = 1
        nrn_path, *pop = nrn_path.split(":")  # fspath:population

        if ospath.isdir(nrn_path):
            # legacy nrnreader may require manual NumSynapseFiles
            # But with a wrapper nrn.h5 dont change from 1
            if not ospath.isfile(ospath.join(nrn_path, "nrn.h5")):
                n_synapse_files = circuit_conf.get("NumSynapseFiles", 1)

            nrn_path = self._find_circuit_file(nrn_path)
        assert ospath.isfile(nrn_path), "nrnPath doesnt contain valid Edge files"

        if pop:
            src_pop, dst_pop = self._edge_to_node_population_names(pop[0])
            assert src_pop == dst_pop, "Base connectivity must be within the same node population"
            self._base_population = dst_pop

        synapse_source = ":".join([nrn_path] + pop)
        self.open_synapse_file(synapse_source, n_synapse_files, 0)
        self._populations_by_src_nodes[None].append(self._cur_population)  # Default population

        self.has_syn_indexes = load_offsets and self._synapse_reader.has_property("synapse_index")
        logging.info("Enabled reading Synapse offsets: %s", self.has_syn_indexes)
        return nrn_path

    @staticmethod
    def _edge_to_node_population_names(edge_pop_name):
        if edge_pop_name is None:
            log_verbose("No edge population name. Matching all unprefixed targets")
            return None, None
        pop_infos = edge_pop_name.split("__")
        if len(pop_infos) == 1:
            # Dont raise exception yet since this is a new feature and we can keep on
            logging.error("Bad format of edge populaton name. "
                          "Requires two or three groups separated by '__'")
        src_pop = pop_infos[0]
        dst_pop = pop_infos[1] if len(pop_infos) > 2 else src_pop
        return src_pop, dst_pop

    # -
    def open_synapse_file(self, synapse_source, n_files=1, src_pop_id=None):
        """Initializes a reader for Synapses"""
        synapse_file, *pop_name = synapse_source.split(":")  # fspath:population
        pop_name = pop_name[0] if pop_name else None

        self._synapse_reader = self._open_synapse_file(synapse_file, pop_name, n_files)
        self._init_conn_population(pop_name, src_pop_id)
        self._unlock_all_connections()  # Allow appending synapses from new sources

    # -
    def _open_synapse_file(self, synapse_file, pop_name, n_nrn_files=None):
        logging.info("Opening Synapse file %s, population: %s", synapse_file, pop_name)
        return self.SynapseReader.create(
            synapse_file, self.CONNECTIONS_TYPE, pop_name,
            n_nrn_files, self._local_gids,  # Used eventually by NRN reader
            extracellular_calcium=SimConfig.extracellular_calcium
        )

    def _init_conn_population(self, pop_name, src_pop_id):
        # For base connectivity src_pop_id=0 and so we immediately have pop (0,0)
        dst_pop_id = 0
        if src_pop_id is None:
            src_pop_id, dst_pop_id = self._compute_pop_ids(pop_name)

        self.select_populations(src_pop_id, dst_pop_id)
        cur_pop = self._cur_population

        pop_names = self._edge_to_node_population_names(pop_name)
        if pop_names[0] is not None or src_pop_id > 0:
            cur_pop.src_name, cur_pop.dst_name = pop_names
            self._populations_by_src_nodes[pop_names[0]].append(cur_pop)
        logging.info("Loading connections to population: %s", cur_pop)

    def _find_conn_populations_nodesets(self, src_pop, dst_pop=None):
        populations = self._populations_by_src_nodes[src_pop]
        if dst_pop is not None:
            populations = [p for p in populations if p.dst_name == dst_pop]
        return populations

    def _compute_pop_ids(self, edge_pop_name):
        """Compute pop id automatically. pop src 0 is base population"""
        if not edge_pop_name:
            logging.warning("Neither Sonata population nor populationID set. "
                            "Edges will be merged with base circuit")
            return 0, 0

        def make_id(node_pop):
            pop_hash = hashlib.md5(node_pop.encode()).digest()
            return ((pop_hash[1] & 0x0f) << 8) + pop_hash[0]  # id: 12bit hash

        # From this point we are dealing with edge population names (in BlueConfig)
        # We can extract the node populations, make checks and create ids
        src_pop, dst_pop = self._edge_to_node_population_names(edge_pop_name)

        if self._base_population is None:
            logging.warning("No base connectivity population name. Assuming Edge target population")
            self._base_population = dst_pop

        # For now enforce all projections to target base connectivity.
        if dst_pop != self._base_population:
            ConfigurationError("Edges must target same nodes as base connectivity")
        dst_pop_id = 0  # always 0. Future nodesets will require similar expression as below
        src_pop_id = 0 if src_pop == self._base_population else make_id(src_pop)
        return src_pop_id, dst_pop_id

    # -
    def select_populations(self, src_id, dst_id):
        """Select the active population of connections. `connect_all()` and
        `connect_group()` will apply only to the active population.
        """
        self._cur_population = self.get_population(src_id, dst_id)

    # -
    def get_population(self, src_id, dst_id=0):
        """Retrieves a connection set given node src and dst ids"""
        pop = self._populations.get((src_id, dst_id))
        if not pop:
            pop = self.ConnectionSet(src_id, dst_id, conn_factory=self.conn_factory)
            self._populations[(src_id, dst_id)] = pop
        return pop

    # NOTE: Several methods use a selector of the connectivity populations
    # which, to be backwards compat, can be a single ID of the src_population
    # or a tuple to specify source and destination

    def find_populations(self, population_ids):
        """Finds the populations that match a given population selector.

        Args:
            population_ids: A population ids selector. Accepted formats:

                - None: All
                - int: selects matching source population id.
                - tuple(src: Any, dst: Any): Specify source and dest.
                  Each value can also be None, e.g.: (None, 1) selects all
                  populations having post id 1
        """
        if (isinstance(population_ids, tuple)
                and population_ids[0] is not None
                and population_ids[1] is not None):
            return [self._populations[population_ids]]
        return [
            pop for pop in self._populations.values()
            if pop.ids_match(population_ids)
        ]

    # -
    def all_connections(self):
        """Retrieves all the existing connections"""
        return chain.from_iterable(
            pop.all_connections() for pop in self._populations.values())

    @property
    def connection_count(self):
        return self._total_connections

    @property
    def current_population(self):
        return self._cur_population

    # -
    def get_connections(self, post_gids, pre_gids=None, population_ids=None):
        """Retrieves all connections that match post and pre gids eventually
        in a subset of the populations.

        Note: Due to multi-population, a connection may not be unique
        for a given pre-post gid pair. As such get_connection() doesn't
        make sense anymore and this method shall be used instead.

        Args:
            post_gids: The target gids of the connections to search (None = All)
            pre_gids: idem for pre-gids. [Default: all)
            population_ids: A int/tuple of populations ids. Default: all

        """
        for pop in self.find_populations(population_ids):
            for conn in pop.get_connections(post_gids, pre_gids):
                yield conn

    # -
    def create_connections(self, *, src_target=None):
        """Creates connections according to loaded parameters in 'Connection'
           blocks of the BlueConfig in the currently active ConnectionSet
        """
        # Create all Projection connections if no Connection block uses
        # that source. Otherwise create only specified connections
        conn_src_pop = self.current_population.src_name
        is_base_pop = self.current_population.src_id == 0
        matching_conns = [
            conn for conn in SimConfig.connections.values()
            if TargetSpec(conn.get("Source").s).match_filter(conn_src_pop, src_target, is_base_pop)
        ]

        if not matching_conns:
            logging.info("No matching Connection blocks. Loading all synapses...")
            self.connect_all()
            return

        # if we have a single connect block with weight=0, skip synapse creation entirely
        if len(matching_conns) == 1 and matching_conns[0].valueOf("Weight") == .0:
            logging.warning("SKIPPING Connection create since they have invariably weight=0")
            return

        logging.info("Creating group connections (%d groups match)", len(matching_conns))
        for conn_conf in matching_conns:
            conn_conf = compat.Map(conn_conf).as_dict(parse_strings=True)
            if "Delay" in conn_conf:
                # Delayed connections are for configuration only, not creation
                continue

            # check if we are not supposed to create (only configure later)
            if conn_conf.get("CreateMode") == "NoCreate":
                continue

            conn_src = conn_conf["Source"]
            conn_dst = conn_conf["Destination"]
            synapse_id = conn_conf.get("SynapseID")
            self.connect_group(conn_src, conn_dst, synapse_id)

    # -
    def configure_connections(self):
        """Configure-only circuit connections according to BlueConfig Connection blocks
        """
        for conn_conf in SimConfig.connections.values():
            conn_conf = compat.Map(conn_conf).as_dict(parse_strings=True)

            conn_src = conn_conf["Source"]
            conn_dst = conn_conf["Destination"]
            is_delayed_connection = "Delay" in conn_conf

            log_msg = " * Pathway {:s} -> {:s}".format(conn_src, conn_dst)
            if is_delayed_connection:
                log_msg += ":\t[DELAYED] t={0[Delay]:g}, weight={0[Weight]:g}".format(conn_conf)
            if "SynapseConfigure" in conn_conf:
                log_msg += ":\tconfigure with '{:s}'".format(conn_conf["SynapseConfigure"])
            logging.info(log_msg)

            if is_delayed_connection:
                self.setup_delayed_connection(conn_conf)
            else:
                self.configure_group(conn_conf)

    def setup_delayed_connection(self, conn_config):
        raise NotImplementedError("Manager %s doesn't implement delayed connections"
                                  % self.__class__.__name__)

    # -
    def connect_all(self, weight_factor=1, only_gids=None, only_sgid_in_target=False):
        """For every gid access its synapse parameters and instantiate
        all synapses.

        Args:
            weight_factor: Factor to scale all netcon weights (default: 1)
            only_gids: Create connections only for these tgids (default: Off)
            only_sgid_in_target: sgids must belong to the main circuit target
                This is mostly useful for Gap-Junctions. (default: False)
        """
        conn_options = {'weight_factor': weight_factor,
                        'synapse_mode': self._synapse_mode}
        pop = self._cur_population
        gids = self._local_gids if only_gids is None else only_gids
        src_target_filter = self._circuit_target if only_sgid_in_target else None

        for sgid, tgid, syns_params, offset in self._iterate_conn_params(
                src_target_filter, None, gids, True):
            if GlobalConfig.debug_conn in ([tgid], [sgid, tgid]):
                log_all(logging.DEBUG, "Connection Params:(%d-%d)\n%s", sgid, tgid, syns_params)

            # Create all synapses. No need to lock since the whole file is consumed
            cur_conn = pop.get_or_create_connection(sgid, tgid, **conn_options)
            self._add_synapses(cur_conn, syns_params, None, offset)

    # -
    def connect_group(self, src_target_name, dst_target_name, synapse_type_restrict=None):
        """Instantiates pathway connections & synapses given src-dst

        Args:
            src_target_name (str): The target name of the source cells
            dst_target_name (str): The target of the destination cells
            synapse_type_restrict(int): Create only given synType synapses
        """
        conn_kwargs = {'synapse_mode': self._synapse_mode}
        pop = self._cur_population
        src_tname = TargetSpec(src_target_name).name
        dst_tname = TargetSpec(dst_target_name).name
        src_target = src_tname and self._target_manager.getTarget(src_tname)
        dst_target = dst_tname and self._target_manager.getTarget(dst_tname)
        logging.debug("Connecting group %s -> %s", src_tname, dst_tname)

        for sgid, tgid, syns_params, offset in self._iterate_conn_params(src_target, dst_target):
            if sgid == tgid:
                logging.warning("Making connection within same Gid: %d", sgid)

            cur_conn = pop.get_or_create_connection(sgid, tgid, **conn_kwargs)
            if cur_conn.locked:
                continue

            _dbg_conn = GlobalConfig.debug_conn
            if _dbg_conn and _dbg_conn in ([tgid], [sgid, tgid]):
                log_all(logging.DEBUG, "Connection (%d-%d). Params:\n%s", sgid, tgid, syns_params)

            self._add_synapses(cur_conn, syns_params, synapse_type_restrict, offset)
            cur_conn.locked = True

    # -
    def _add_synapses(self, cur_conn, syns_params, syn_type_restrict=None, base_id=0):
        for i, syn_params in enumerate(syns_params):
            if syn_type_restrict and syn_params.synType != syn_type_restrict:
                continue
            point = self._target_manager.locationToPoint(
                cur_conn.tgid, syn_params.isec, syn_params.ipt, syn_params.offset)
            cur_conn.add_synapse(point, syn_params, base_id + i)

    # -
    def _iterate_conn_params(self, src_target, dst_target, gids=None, show_progress=False):
        """A generator which loads synapse data and yields tuples(sgid, tgid, synapses)

        Args:
            src_target: the target to filter the source cells, or None
            dst_target: the target to filter the destination cells, or None
            gids: Use given gids, instead of the circuit target cells. Default: None
            show_progress: Display a progress bar as tgids are processed
        """
        if gids is None:
            gids = self._local_gids
        if show_progress:
            gids = ProgressBar.iter(gids)
        created_conns_0 = self._cur_population.count()

        for tgid in gids:
            if dst_target is not None and not dst_target.contains(tgid):
                continue

            # Retrieve all synapses for tgid
            syns_params = self._synapse_reader.get_synapse_parameters(tgid)
            logging.debug("GID %d Syn count: %d", tgid, len(syns_params))
            cur_i = 0
            syn_count = len(syns_params)

            # The first field is typically sgid, but to generalize we use field 0
            sgids = syns_params[syns_params.dtype.names[0]].copy()
            found_conns = 0
            yielded_conns = 0

            while cur_i < syn_count:
                # Use numpy to get all the synapses of the same gid at once
                sgid = int(sgids[cur_i])
                next_i = numpy.argmax(sgids[cur_i:] != sgid) + cur_i
                if cur_i == next_i:  # last group
                    next_i = syn_count

                if src_target is None or src_target.completeContains(sgid):
                    yield sgid, tgid, syns_params[cur_i:next_i], cur_i
                    yielded_conns += 1

                found_conns += 1
                cur_i = next_i

            logging.debug(" > Yielded %d out of %d connections. (Filter by src Target: %s)",
                          yielded_conns, found_conns, src_target and src_target.name)

        created_conns = self._cur_population.count() - created_conns_0
        self._total_connections += created_conns

        if created_conns:
            pathway_repr = "[ALL]"
            if src_target and dst_target:
                pathway_repr = "Pathway {} -> {}".format(src_target.name, dst_target.name)
            logging.info(" * %s. [Rank 0]: Created %d connections",
                         pathway_repr, created_conns)

    # -
    def get_target_connections(self, src_target_name,
                                     dst_target_name,
                                     gidvec=None,
                                     conn_population=None):
        """Retrives the connections between src-dst cell targets

        Optional gidvec (post) / conn_population restrict the set of
        connections to be returned
        """
        src_target_spec = TargetSpec(src_target_name)
        dst_target_spec = TargetSpec(dst_target_name)
        src_target = self._target_manager.getTarget(src_target_spec.name) \
            if src_target_spec.name is not None else None
        dst_target = self._target_manager.getTarget(dst_target_spec.name)
        gidvec = self._local_gids if gidvec is None else gidvec

        if conn_population is not None:
            populations = (conn_population,)
        else:
            population_path = (src_target_spec.population, dst_target_spec.population)
            populations = self._find_conn_populations_nodesets(*population_path)
            if not populations:
                logging.warning("No connections found between node populations %s",
                                population_path)
                return

        for population in populations:
            logging.debug("Connections from population %s", population)
            for tgid in gidvec:
                if not dst_target.contains(tgid) or tgid not in population:
                    continue
                for conn in population[tgid]:
                    sgid = conn.sgid
                    if src_target is None or src_target.completeContains(sgid):
                        yield conn

    # -
    def configure_group(self, conn_config, gidvec=None):
        """Configure connections according to a BlueConfig Connection block

        Args:
            conn_config: The connection configuration dict
            gidvec: A restricted set of gids to configure
            population_ids: A tuple of populations' connections.
                Default: None (all populations)
        """
        src_target = conn_config["Source"]
        dst_target = conn_config["Destination"]
        _properties = {
            "Weight": "weight_factor",
            "SpontMinis": "minis_spont_rate",
            "SynDelayOverride": "syndelay_override"
        }
        syn_params = dict_filter_map(conn_config, _properties)

        # Load eventual mod override helper
        if "ModOverride" in conn_config:
            logging.info("   => Overriding mod: %s", conn_config["ModOverride"])
            override_helper = conn_config["ModOverride"] + "Helper"
            Nd.load_hoc(override_helper)
            assert hasattr(Nd.h, override_helper), \
                "ModOverride helper doesn't define hoc template: " + override_helper

        for conn in self.get_target_connections(src_target, dst_target, gidvec):
            for key, val in syn_params.items():
                setattr(conn, key, val)
            if "ModOverride" in conn_config:
                conn.override_mod(conn_config['_hoc'])
            if "SynapseConfigure" in conn_config:
                conn.add_synapse_configuration(conn_config["SynapseConfigure"])

    # -
    def configure_group_delayed(self, conn_config, gidvec=None):
        """Update instantiated connections with configuration from a
        'Delayed Connection' blocks.
        """
        self.update_connections(conn_config["Source"],
                                conn_config["Destination"],
                                gidvec,
                                conn_config.get("SynapseConfigure"),
                                conn_config.get("Weight"))

    # Live connections update
    # -----------------------
    @timeit(name="connUpdate", verbose=False)
    def update_connections(self, src_target, dst_target, gidvec=None,
                                 syn_configure=None, weight=None, **syn_params):
        """Update params on connections that are already instantiated.

        Args:
            src_target: Name of Source Target
            dst_target: Name of Destination Target
            gidvec: A list of gids to apply configuration. Default: all
            syn_configure: A hoc configuration string to apply to the synapses
            weight: new weights for the netcons
            **syn_params: Keyword arguments of synapse properties to be changed
                e.g. conductance: g=xyz
        """
        if syn_configure is None and weight is None and not syn_params:
            logging.warning("No synpases parameters being updated for Targets %s->%s",
                            src_target, dst_target)
            return

        updated_conns = 0
        for conn in self.get_target_connections(src_target, dst_target, gidvec):
            if weight is not None:
                updated_conns += 1
                conn.update_weights(weight)
            if syn_configure is not None:
                conn.configure_synapses(syn_configure)
            if syn_params:
                conn.update_synpase_parameters(**syn_params)

        logging.info("Updated %d conns", updated_conns)

    def restart_events(self):
        """After restore, restart the artificial events (replay and spont minis)
        """
        for conn in self.all_connections():
            conn.restart_events()

    # ------------------------------------------------------------------
    # Delete, Disable / Enable
    # ------------------------------------------------------------------
    def delete(self, sgid, tgid, population_ids=None):
        """Deletes a connection given source and target gids.

        NOTE: Contrary to disable(), deleting a connection cannot be undone,
        however it may help saving computational resources.

        Args:
            sgid: The pre-gid of the cell
            tgid: The post-gid of the cell
            population_ids: The population selector. Default: all
        """
        for pop in self.find_populations(population_ids):
            pop.delete(sgid, tgid)

    def disable(self, sgid, tgid, also_zero_conductance=False, population_ids=None):
        """Disable a connection, its netcons and optionally synapses.

        NOTE: Disabling a connection before calling init() prevents
        it from being instantiated in CoreNeuron.

        Args:
            sgid: The pre-gid of the cell
            tgid: The post-gid of the cell
            also_zero_conductance: Also sets synapses' conductances
                to zero. Default: False
            population_ids: The population selector. Default: all
        """
        for pop in self.find_populations(population_ids):
            c = pop.get_connection(sgid, tgid)
            c.disable(also_zero_conductance)
            self._disabled_conns[tgid].append(c)

    def reenable(self, sgid, tgid, population_ids=None):
        """(Re)enable a connection from given populations.
        """
        allowed_pops = self.find_populations(population_ids)
        delete_indexes = []
        for i, conn in enumerate(self._disabled_conns[tgid]):
            if conn.sgid == sgid and any((p.src_id, p.dst_id) == conn.population_id
                                         for p in allowed_pops):
                conn.enable()
                delete_indexes.append(i)
        self._disabled_conns[tgid] = \
            numpy.delete(self._disabled_conns[tgid], delete_indexes).tolist()

    def reenable_all(self, post_gids=None):
        """Re-enables all disabled connections

        Args:
            post_gids: The list of target gids to enable (Default: all)
        """
        gids = self._local_gids if post_gids is None else post_gids
        for tgid in gids:
            for c in self._disabled_conns[tgid]:
                c.enable()
            del self._disabled_conns[tgid][:]

    # GROUPS
    # ------
    def delete_group(self, post_gids, pre_gids=None, population_ids=None):
        """Delete a number of connections given pre and post gid lists.
           Note: None is neutral and will match all gids.

        Args:
            post_gids: The target gids of the connections to be disabled
                Use None for all post-gids
            pre_gids: idem for pre-gids. [Default: None -> all)
            population_ids: A int/tuple of populations ids. Default: all
        """
        for pop in self.find_populations(population_ids):
            pop.delete_group(post_gids, pre_gids)

    def disable_group(self, post_gids, pre_gids=None,
                            also_zero_conductance=False,
                            population_ids=None):
        """Disable a number of connections given pre and post gid lists.

        Args:
            post_gids: The target gids of the connections to be deleted
            pre_gids: idem for pre-gids. [Default: None -> all)
            also_zero_conductance: Also sets synapse conductance to 0
            population_ids: A int/tuple of populations ids. Default: all
        """
        for pop in self.find_populations(population_ids):
            for conn in pop.get_connections(post_gids, pre_gids):
                self._disabled_conns[conn.tgid].append(conn)
                conn.disable(also_zero_conductance)

    def reenable_group(self, post_gids, pre_gids=None, population_ids=None):
        """Enable a number of connections given lists of pre and post gids.
        Note: None will match all gids.
        """
        post_gids = self._local_gids if post_gids is None else post_gids
        pre_gids = set(pre_gids)
        allowed_pops = self.find_populations(population_ids)

        for tgid in post_gids:
            to_delete = []
            for i, conn in enumerate(self._disabled_conns[tgid]):
                if conn.sgid in pre_gids and \
                        any((p.src_id, p.dst_id) == conn.population_id
                            for p in allowed_pops):
                    conn.enable()
                    to_delete.append(i)

            self._disabled_conns[tgid] = \
                numpy.delete(self._disabled_conns[tgid], to_delete).tolist()

    def get_disabled(self, post_gid=None):
        """Returns the list of disabled connections, optionally for a
        given post-gid.
        """
        if post_gid is not None:
            return self._disabled_conns[post_gid]
        return chain.from_iterable(self._disabled_conns.values())

    @classmethod
    def _find_circuit_file(cls, location):
        """Attempts to find a circuit file given any directory or file
        """
        compat_file = cls.CIRCUIT_FILENAMES[-1]  # last is the nrn.h5 format
        files_avail = [f for f in cls.CIRCUIT_FILENAMES
                       if ospath.isfile(ospath.join(location, f))]
        # Custom readers responsible for their formats
        can_read_newer_formats = SynapseReader.is_syntool_enabled() \
            if cls.SynapseReader is SynapseReader else True
        if not files_avail:
            raise ConfigurationError(
                "nrnPath is not a file and could not find any synapse file within.")
        if not can_read_newer_formats and compat_file not in files_avail:
            raise ConfigurationError(
                "Found synapse file requires synapsetool, which is not available")
        if len(files_avail) > 1:
            logging.warning("DEPRECATION: Found several synapse file formats in nrnPath. "
                            "Auto-select is deprecated and will be removed")
            if not can_read_newer_formats:
                files_avail[0] = compat_file
        return ospath.join(location, files_avail[0])

    def _unlock_all_connections(self):
        """Unlock all, mainly when we load a new connectivity source"""
        for conn in self.all_connections():
            conn.locked = False

    def finalize(self, base_seed=0, sim_corenrn=False, *, conn_type="synapses", **conn_params):
        """Instantiates the netcons and Synapses for all GapJunctions.
        """
        # Connections must have been placed and all weight scalars should have their
        # final values. We can destroy _reader to release memory (all cached params)
        self._synapse_reader = None

        logging.info("Instantiating %s... Params: %s", conn_type, str(conn_params))
        n_created_conns = 0

        for popid, pop in self._populations.items():
            conn_params["attach_src_cell"] = (pop.src_id == 0
                                              or (pop.src_name is None and sim_corenrn))
            for tgid, conns in ProgressBar.iter(pop.items(), name="Pop:" + str(popid)):
                n_created_conns += self._finalize_conns(
                    tgid, conns, base_seed, sim_corenrn, **conn_params)

        all_ranks_total = MPI.allreduce(n_created_conns, MPI.SUM)
        logging.info(" => Created %d %s", all_ranks_total, conn_type)
        return all_ranks_total

    @abstractmethod
    def _finalize_conns(self, tgid, conns, base_seed, sim_corenrn, **_):
        """Method finalizing a gid connections, invoked for each target gid.
        """


# ######################################################################
# SynapseRuleManager
# ######################################################################
class SynapseRuleManager(ConnectionManagerBase):
    """
    The SynapseRuleManager is designed to encapsulate the creation of
    synapses for BlueBrain simulations, handling the data coming from
    the circuit file. If the BlueConfig file provides any Connection
    Rules, those override which synapses are created.

    Note that the Connection rules are processed with the assumption
    that they come in the config file from more general to more specific.
    E.g.: A column->column connection should come before
    layer 4 -> layer 2 which should come before L4PC -> L2PC.

    Once all synapses are preped with final weights, the netcons can be
    created.
    """

    CIRCUIT_FILENAMES = ('edges.sonata',
                         'edges.h5',
                         'circuit.syn2',
                         'nrn.h5')
    CONNECTIONS_TYPE = SynapseReader.SYNAPSES

    def __init__(self, circuit_conf, target_manager, cell_dist, load_offsets=False, **kw):
        """Initializes SynapseRuleManager, reading the circuit file.

        Args:
            circuit_conf: A circuit configuration, to get nrnPath, SynapseMode..
            target_manager: The TargetManager which will be used to
                query targets and translate locations to points
                placed (AmpaOnly vs DualSyns). Default: DualSyns
            cell_dist: The cell distributor to query the local gids
            load_offsets: Whether to load synapse offsets.
                Note: Under nrn format, this can be significantly slow
        """
        super().__init__(circuit_conf, target_manager, cell_dist, **kw)
        if circuit_conf.nrnPath:
            self.init_synapse_location(circuit_conf.nrnPath, circuit_conf, load_offsets)
        else:
            logging.info(" * Circuit connections have been DISABLED")

        synapse_mode = circuit_conf.get("SynapseMode")
        self._synapse_mode = SynapseMode.from_str(synapse_mode) \
            if synapse_mode is not None else SynapseMode.default

    # -
    def finalize(self, base_seed=0, sim_corenrn=False, **kwargs):
        """Create the actual synapses and netcons.

        Note: All weight scalars should have their final values.

        Args:
            base_seed: optional argument to adjust synapse RNGs (default=0)
            sim_corenrn: Finalize accordingly in case we target CoreNeuron
            replay_mode: How shall we instantiate replay? Default: Auto-Detect
                Use DISABLED to skip replay and COMPLETE to instantiate VecStims
                in all synapses
            kwargs: Additional finalize parameters for the specific _finalize_conns
        """
        # CoreNeuron will handle replays automatically with its own PatternStim
        replay_mode = ReplayMode.NONE if sim_corenrn else ReplayMode.AS_REQUIRED
        kwargs.setdefault("replay_mode", replay_mode)
        super().finalize(base_seed, sim_corenrn, **kwargs)

    def _finalize_conns(self, tgid, conns, base_seed, sim_corenrn, **kwargs):
        """ Low-level handling of finalizing connections belonging to a target gid.
        By default it calls finalize on each cell.
        Note: *args normally contains the REPLAY mode but may differ for other types
        """
        cell_distributor = self._cell_manager
        metype = cell_distributor.getMEType(tgid)
        spgid = cell_distributor.getSpGid(tgid)
        n_created_conns = 0

        # Note: (Compat) neurodamus hoc keeps connections in reversed order.
        for conn in reversed(conns):  # type: Connection
            # Skip disabled if we are running with core-neuron
            if conn.finalize(metype, base_seed, spgid, sim_corenrn, **kwargs):
                n_created_conns += 1
        return n_created_conns

    def setup_delayed_connection(self, conn_config):
        """Setup delayed connection weights for synapse initialization.

        Find source and target gids and the associated connection,
        and add the delay and weight to their delay vectors.

        Args:
            conn_config: Connection configuration parsed from BlueConfig
        """
        src_target_name = conn_config["Source"]
        dst_target_name = conn_config["Destination"]
        delay = conn_config["Delay"]
        new_weight = conn_config.get("Weight", .0)

        for conn in self.get_target_connections(src_target_name, dst_target_name):
            conn.add_delayed_weight(delay, new_weight)

    # -
    @timeit(name="Replay inject")
    def replay(self, spike_manager, src_target_name, dst_target_name, start_delay=.0):
        """Create special netcons to trigger timed spikes on those synapses.

        Args:
            spike_manager: map of gids (pre-synaptic) with spike times
            src_target_name: Source population:target of the replay connections
            dst_target_name: Target whose gids should be replayed
            start_delay: Dont deliver events before t=start_delay
        """
        log_verbose("Applying replay map with %d src cells...", len(spike_manager))
        replayed_count = 0

        # Dont deliver events in the past
        if Nd.t > start_delay:
            start_delay = Nd.t
            log_verbose("Restore: Delivering events only after t=%.4f", start_delay)

        for conn in self.get_target_connections(src_target_name, dst_target_name):
            if conn.sgid not in spike_manager:
                continue
            conn.replay(spike_manager[conn.sgid], start_delay)
            replayed_count += 1

        total_replays = MPI.allreduce(replayed_count, MPI.SUM)
        if MPI.rank == 0:
            if total_replays == 0:
                logging.warning("No connections were injected replay stimulus")
            else:
                logging.info(" => Replaying on %d connections", total_replays)
        return total_replays


# ######################################################################
# Gap Junctions
# ######################################################################
class GapJunctionManager(ConnectionManagerBase):
    """
    The GapJunctionManager is similar to the SynapseRuleManager. It will
    open dedicated connectivity files which will have the locations and
    conductance strengths of gap junctions detected in the circuit.
    The user will have the capacity to scale the conductance weights.
    """

    CIRCUIT_FILENAMES = ("gj.sonata", "gj.syn2", "nrn_gj.h5")
    CONNECTIONS_TYPE = SynapseReader.GAP_JUNCTIONS

    def __init__(self, gj_conf, target_manager, cell_dist, circuit_target, **kw):
        """Initialize GapJunctionManager, opening the specified GJ
        connectivity file.

        Args:
            gj_conf: The gaps junctions configuration block / dict
            target_manager: The TargetManager which will be used to query
                targets and translate locations to points
            cell_dist: The cell distributor to query the local gids
            circuit_target: The name of the circuit target. GapJunctions src and
                target cells must all belong to the target
        """
        if circuit_target is None:
            raise ConfigurationError(
                "No circuit target. Required when initializing GapJunctionManager")
        if "Path" not in gj_conf:
            raise ConfigurationError("Missing GapJunction 'Path' configuration")

        super().__init__(gj_conf, target_manager, cell_dist, **kw)
        gj_file = self.init_synapse_location(gj_conf["Path"], gj_conf, False)

        self._circuit_target = target_manager.getTarget(circuit_target)
        self._gj_offsets = None

        # nrn_gj were relative to gid. From syn2 onwards we use absolute offsets
        if gj_file.endswith("nrn_gj.h5"):
            self._gj_offsets = self._compute_gj_offsets(gj_conf)

    def _compute_gj_offsets(self, gj_conf):
        log_verbose("Computing gap-junction offsets from gjinfo.txt")
        gj_offsets = compat.Vector("I")
        gjfname = ospath.join(gj_conf["Path"], "gjinfo.txt")
        gj_sum = 0

        for line in open(gjfname):
            gj_offsets.append(gj_sum)  # fist gid has no offset. the final total is not used
            gid, offset = map(int, line.strip().split())
            gj_sum += 2 * offset

        return gj_offsets

    def finalize(self, *_, **_kw):
        super().finalize(conn_type="Gap-Junctions")

    def _finalize_conns(self, tgid, conns, *_, **_kw):
        metype = self._cell_manager.getMEType(tgid)

        if self._gj_offsets is None:
            for conn in reversed(conns):
                conn.finalize_gap_junctions(metype, 0, 0)
        else:
            t_gj_offset = self._gj_offsets[tgid - 1]   # Old nrn_gj uses offsets
            for conn in reversed(conns):
                conn.finalize_gap_junctions(metype, t_gj_offset, self._gj_offsets[conn.sgid - 1])
        return len(conns)
