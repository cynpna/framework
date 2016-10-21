# Copyright (C) 2016 iNuron NV
#
# This file is part of Open vStorage Open Source Edition (OSE),
# as available from
#
#      http://www.openvstorage.org and
#      http://www.openvstorage.com.
#
# This file is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
# as published by the Free Software Foundation, in version 3 as it comes
# in the LICENSE.txt file of the Open vStorage OSE distribution.
#
# Open vStorage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY of any kind.

"""
StorageDriver module
"""

import time
from ovs.dal.dataobject import DataObject
from ovs.dal.structures import Property, Relation, Dynamic
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.extensions.storageserver.storagedriver import StorageDriverClient
from ovs.log.log_handler import LogHandler


class StorageDriver(DataObject):
    """
    The StorageDriver class represents a Storage Driver. A Storage Driver is an application
    on a Storage Router to which the vDisks connect. The Storage Driver is the gateway to the Storage Backend.
    """
    _logger = LogHandler.get('dal', name='hybrid')

    __properties = [Property('name', str, doc='Name of the Storage Driver.'),
                    Property('description', str, mandatory=False, doc='Description of the Storage Driver.'),
                    Property('ports', dict, doc='Ports on which the Storage Driver is listening (management, xmlrpc, dtl, edge).'),
                    Property('cluster_ip', str, doc='IP address on which the Storage Driver is listening.'),
                    Property('storage_ip', str, doc='IP address on which the vpool is shared to hypervisor'),
                    Property('storagedriver_id', str, doc='ID of the Storage Driver as known by the Storage Drivers.'),
                    Property('mountpoint', str, doc='Mountpoint from which the Storage Driver serves data'),
                    Property('mountpoint_dfs', str, mandatory=False, doc='Location of the backend in case of a distributed FS'),
                    Property('startup_counter', int, default=0, doc='StorageDriver startup counter')]
    __relations = [Relation('vpool', VPool, 'storagedrivers'),
                   Relation('storagerouter', StorageRouter, 'storagedrivers')]
    __dynamics = [Dynamic('status', str, 30),
                  Dynamic('statistics', dict, 4),
                  Dynamic('edge_clients', list, 30),
                  Dynamic('vdisks_guids', list, 15)]

    def _status(self):
        """
        Fetches the Status of the Storage Driver.
        """
        _ = self
        return None

    def _statistics(self, dynamic):
        """
        Aggregates the Statistics (IOPS, Bandwidth, ...) of the vDisks connected to the Storage Driver.
        """
        from ovs.dal.hybrids.vdisk import VDisk
        statistics = {}
        for key in StorageDriverClient.STAT_KEYS:
            statistics[key] = 0
            statistics['{0}_ps'.format(key)] = 0
        for key, value in self.fetch_statistics().iteritems():
            statistics[key] += value
        statistics['timestamp'] = time.time()
        VDisk.calculate_delta(self._key, dynamic, statistics)
        return statistics

    def _edge_clients(self):
        """
        Retrieves all edge clients
        """
        clients = []
        try:
            for item in self.vpool.storagedriver_client.list_client_connections(str(self.storagedriver_id)):
                clients.append({'key': '{0}:{1}'.format(item.ip, item.port),
                                'object_id': item.object_id,
                                'ip': item.ip,
                                'port': item.port})
        except Exception as ex:
                StorageDriver._logger.error('Error loading edge clients from {0}: {1}'.format(self.storagedriver_id, ex))
        clients.sort(key=lambda e: (e['ip'], e['port']))
        return clients

    def _vdisks_guids(self):
        """
        Gets the vDisk guids served by this StorageDriver.
        """
        from ovs.dal.lists.vdisklist import VDiskList
        volume_ids = []
        for entry in self.vpool.objectregistry_client.get_all_registrations():
            if entry.node_id() == self.storagedriver_id:
                volume_ids.append(entry.object_id())
        return VDiskList.get_in_volume_ids(volume_ids).guids

    def fetch_statistics(self):
        """
        Loads statistics from this vDisk - returns unprocessed data
        """
        # Load data from volumedriver
        if self.storagedriver_id and self.vpool:
            try:
                sdstats = self.vpool.storagedriver_client.statistics_node(str(self.storagedriver_id))
            except Exception as ex:
                StorageDriver._logger.error('Error loading statistics_node from {0}: {1}'.format(self.storagedriver_id, ex))
                sdstats = StorageDriverClient.EMPTY_STATISTICS()
        else:
            sdstats = StorageDriverClient.EMPTY_STATISTICS()
        # Load volumedriver data in dictionary
        sdstatsdict = {}
        try:
            pc = sdstats.performance_counters
            sdstatsdict['backend_data_read'] = pc.backend_read_request_size.sum()
            sdstatsdict['backend_data_written'] = pc.backend_write_request_size.sum()
            sdstatsdict['backend_read_operations'] = pc.backend_read_request_size.events()
            sdstatsdict['backend_write_operations'] = pc.backend_write_request_size.events()
            sdstatsdict['data_read'] = pc.read_request_size.sum()
            sdstatsdict['data_written'] = pc.write_request_size.sum()
            sdstatsdict['read_operations'] = pc.read_request_size.events()
            sdstatsdict['write_operations'] = pc.write_request_size.events()
            for key in ['cluster_cache_hits', 'cluster_cache_misses', 'metadata_store_hits',
                        'metadata_store_misses', 'sco_cache_hits', 'sco_cache_misses', 'stored']:
                sdstatsdict[key] = getattr(sdstats, key)
            # Do some more manual calculations
            block_size = 0
            if len(self.vpool.vdisks) > 0:
                vdisk = self.vpool.vdisks[0]
                block_size = vdisk.metadata.get('lba_size', 0) * vdisk.metadata.get('cluster_multiplier', 0)
            if block_size == 0:
                block_size = 4096
            sdstatsdict['4k_read_operations'] = sdstatsdict['data_read'] / block_size
            sdstatsdict['4k_write_operations'] = sdstatsdict['data_written'] / block_size
            # Pre-calculate sums
            for key, items in StorageDriverClient.STAT_SUMS.iteritems():
                sdstatsdict[key] = 0
                for item in items:
                    sdstatsdict[key] += sdstatsdict[item]
        except:
            pass
        return sdstatsdict
