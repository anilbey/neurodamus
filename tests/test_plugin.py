"""
A module definiting and running a simple engine.
We define ACell cells and corresponding managers
"""

import logging
import numpy as np
import os
import pytest
import subprocess

from neurodamus.cell_distributor import CellManagerBase
from neurodamus.connection import ConnectionBase
from neurodamus.connection_manager import ConnectionManagerBase
from neurodamus.core import EngineBase
from neurodamus.core import NeurodamusCore as Nd
from neurodamus.io.synapse_reader import SynapseParameters
from neurodamus.io.cell_readers import split_round_robin


class ACellType:
    """A new testing cell type
    """
    def __init__(self, gid, cell_info, circuit_conf):
        """Instantiate a new Cell from mvd/node info"""
        self.gid = gid
        self.section = Nd.Section(name="soma", cell="a" + str(gid))
        self.f0 = cell_info[0]
        self.f1 = cell_info[1]

    @property
    def CellRef(self):
        return self  # We dont use hoc cells

    def connect2target(self, target_pp):
        return Nd.NetCon(self.section(1)._ref_v, target_pp, sec=self.section)


class ACellManager(CellManagerBase):
    CellType = ACellType

    @staticmethod
    def load_cell_info(run_conf, gidvec, stride=1, stride_offset=0):
        logging.info(" * HELLO from loading ACELL info")
        total_cells = 50

        gidvec = split_round_robin(gidvec, stride, stride_offset, total_cells)
        local_cell_count = len(gidvec)
        if not len(gidvec):  # Not enough cells to give this rank a few
            return gidvec, {}, total_cells

        properties = [
            np.ones(local_cell_count, dtype='f4'),  # fake field 1
            np.arange(local_cell_count, dtype='f4')  # fake field 2
        ]
        cell_info = dict(zip(gidvec, np.stack(properties, axis=-1)))
        return gidvec, cell_info, total_cells


class ACellConnection(ConnectionBase):
    """
    ACellConnections: simple so we aggregate all sources
    """
    def __init__(self, _, tgid, src_pop_id=0, dst_pop_id=0, weight_factor=1, sgids=None, **kw):
        """Init Connection. sgid as indexer is not used and set to None"""
        super().__init__(None, tgid, src_pop_id, dst_pop_id, weight_factor)
        self._src_gids = sgids or np.array([], dtype="uint32")
        self._synapse_params = ASynParameters.empty

    # Public properties
    conn_count = property(lambda self: len(self._src_gids))

    def append_src_cells(self, sgids, syn_params):
        self._src_gids = np.concatenate((self._src_gids, sgids))
        self._synapse_params = np.concatenate((self._synapse_params, syn_params))

    def finalize(self, pnm, target_cell, *_):
        syn = Nd.ExpSyn(target_cell.section(0.5))
        self._synapses = (syn,)
        self._netcons = []

        for sgid, syn_params in zip(self._src_gids, self._synapse_params):
            nc = pnm.pc.gid_connect(sgid, syn)
            nc.weight[0] = syn_params.conductance * self._conn_params.weight_factor
            nc.delay = syn_params.delay
            self._netcons.append(nc)
        return len(self._src_gids)


class ASynParameters(SynapseParameters):
    _synapse_fields = ["sgid", "delay", "conductance"]


class ACellSynReader(object):
    def get_synapse_parameters(self, tgid):
        # for testing, each cell connects to src gids tgid+1 and tgid+2
        params = ASynParameters.create_array(2)
        params.sgid = [tgid+1, tgid+2]
        params.delay = 0.1
        params.conductance = 1
        return params


class ACellSynapseManager(ConnectionManagerBase):
    conn_factory = ACellConnection

    def open_synapse_file(self, synapse_file, n_synapse_files=None, src_pop_id=0):
        logging.info("Opening Synapse file %s", synapse_file)
        self._synapse_reader = ACellSynReader()

    def _add_synapses(self, cur_conn, syns_params, syn_type_restrict=None, base_id=0):
        cur_conn.append_src_cells(syns_params.sgid, syns_params)

    def _finalize_conns(self, tgid, conns, *_):
        target_cell = self._cell_distibutor[tgid]
        conns[0].finalize(self._cell_distibutor.pnm, target_cell)
        return conns[0].conn_count


class ACellEngine(EngineBase):
    CellManagerCls = ACellManager
    SynapseManagerCls = ACellSynapseManager


#
# Launching of the engine as a test
#
sims = os.path.abspath(os.path.join(os.path.dirname(__file__), "simulations"))
requires_mpi = pytest.mark.skipif(
    os.environ.get("SLURM_JOB_ID") is None and os.environ.get("RUN_MPI") is None,
    reason="Simulation tests require MPI")


@requires_mpi
def test_run_acell_circuit():
    simdir = os.path.join(sims, "acell_engine")
    env = os.environ.copy()
    env['PYTHONPATH'] += ":" + os.path.dirname(__file__)
    env['NEURODAMUS_PLUGIN'] = os.path.splitext(os.path.basename(__file__))[0]
    ps = subprocess.run(["bash", "tests/test_simulation.bash", simdir], env=env)
    assert ps.returncode == 0


if __name__ == "__main__":
    test_run_acell_circuit()