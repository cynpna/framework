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
Helper module
"""
from ovs.dal.hybrids.backendtype import BackendType
from ovs.dal.hybrids.domain import Domain
from ovs.dal.hybrids.j_mdsservice import MDSService
from ovs.dal.hybrids.j_mdsservicevdisk import MDSServiceVDisk
from ovs.dal.hybrids.j_storagerouterdomain import StorageRouterDomain
from ovs.dal.hybrids.service import Service
from ovs.dal.hybrids.servicetype import ServiceType
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.hybrids.vpool import VPool
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.storageserver.storagedriver import StorageDriverClient, StorageDriverConfiguration
from ovs.extensions.storageserver.tests.mockups import MDSClient, StorageRouterClient


class Helper(object):
    """
    This class contains functionality used by all or most vDisk related tests
    """
    @staticmethod
    def generate_nc_function(address, mds_service):
        """
        Generates the lambda that will return the address or ip
        """
        if address is True:
            return lambda s: mds_service.service.storagerouter.ip
        return lambda s: int(mds_service.service.ports[0])

    @staticmethod
    def generate_bc_function(_configs):
        """
        Generates the lambda that will return the config list
        """
        return lambda s: _configs

    @staticmethod
    def build_service_structure(structure):
        """
        Builds an MDS service structure
        Example:
            structure = Helper.build_service_structure(
                {'vpools': [1],
                 'domains': [],
                 'storagerouters': [1],
                 'storagedrivers': [(1, 1, 1)],  # (<id>, <vpool_id>, <storagerouter_id>)
                 'mds_services': [(1, 1)],  # (<id>, <storagedriver_id>)
                 'storagerouter_domains': []}  # (<id>, <storagerouter_id>, <domain_id>)
            )
        """
        vdisks = {}
        vpools = {}
        domains = {}
        services = {}
        mds_services = {}
        storagerouters = {}
        storagedrivers = {}
        storagerouter_domains = {}
        service_type = ServiceType()
        service_type.name = 'MetadataServer'
        service_type.save()
        backend_type = BackendType()
        backend_type.name = 'BackendType'
        backend_type.code = 'BT'
        backend_type.save()
        for domain_id in structure.get('domains', []):
            domain = Domain()
            domain.name = 'domain_{0}'.format(domain_id)
            domain.save()
            domains[domain_id] = domain
        for vpool_id in structure.get('vpools', []):
            vpool = VPool()
            vpool.name = str(vpool_id)
            vpool.status = 'RUNNING'
            vpool.backend_type = backend_type
            vpool.save()
            vpools[vpool_id] = vpool
        for vdisk_id, storage_driver_id, vpool_id in structure.get('vdisks', ()):
            vpool = vpools[vpool_id]
            vdisk = VDisk()
            vdisk.name = str(vdisk_id)
            vdisk.devicename = 'vdisk_{0}'.format(vdisk_id)
            vdisk.volume_id = 'vdisk_{0}'.format(vdisk_id)
            vdisk.vpool = vpool
            vdisk.size = 0
            vdisk.save()
            vdisk.reload_client('storagedriver')
            StorageRouterClient.vrouter_id[vpool.guid]['vdisk_{0}'.format(vdisk_id)] = str(storage_driver_id)
            vdisks[vdisk_id] = vdisk
        for sr_id in structure.get('storagerouters', []):
            storagerouter = StorageRouter()
            storagerouter.name = str(sr_id)
            storagerouter.ip = '10.0.0.{0}'.format(sr_id)
            storagerouter.rdma_capable = False
            storagerouter.save()
            storagerouters[sr_id] = storagerouter
        for sd_id, vpool_id, sr_id in structure.get('storagedrivers', ()):
            storagedriver = StorageDriver()
            storagedriver.vpool = vpools[vpool_id]
            storagedriver.storagerouter = storagerouters[sr_id]
            storagedriver.name = str(sd_id)
            storagedriver.mountpoint = '/'
            storagedriver.cluster_ip = storagerouters[sr_id].ip
            storagedriver.storage_ip = '10.0.1.{0}'.format(sr_id)
            storagedriver.storagedriver_id = str(sd_id)
            storagedriver.ports = {'management': 1,
                                   'xmlrpc': 2,
                                   'dtl': 3,
                                   'edge': 4}
            storagedriver.save()
            storagedrivers[sd_id] = storagedriver
            Helper._set_vpool_storage_driver_configuration(vpool=vpools[vpool_id], storagedriver=storagedriver)
        for mds_id, sd_id in structure.get('mds_services', ()):
            sd = storagedrivers[sd_id]
            s_id = '{0}-{1}'.format(sd.storagerouter.name, mds_id)
            service = Service()
            service.name = s_id
            service.storagerouter = sd.storagerouter
            service.ports = [mds_id]
            service.type = service_type
            service.save()
            services[s_id] = service
            mds_service = MDSService()
            mds_service.service = service
            mds_service.number = 0
            mds_service.capacity = 10
            mds_service.vpool = sd.vpool
            mds_service.save()
            mds_services[mds_id] = mds_service
        for srd_id, sr_id, domain_id, backup in structure.get('storagerouter_domains', ()):
            sr_domain = StorageRouterDomain()
            sr_domain.backup = backup
            sr_domain.domain = domains[domain_id]
            sr_domain.storagerouter = storagerouters[sr_id]
            sr_domain.save()
            storagerouter_domains[srd_id] = sr_domain
        return {'vdisks': vdisks,
                'vpools': vpools,
                'domains': domains,
                'services': services,
                'service_type': service_type,
                'mds_services': mds_services,
                'storagerouters': storagerouters,
                'storagedrivers': storagedrivers,
                'storagerouter_domains': storagerouter_domains}

    @staticmethod
    def create_vdisks_for_mds_service(amount, start_id, mds_service=None, vpool=None):
        """
        Generates vdisks and appends them to a given mds_service
        """
        vdisks = {}
        storagedriver_id = None
        if mds_service is not None:
            for sd in mds_service.vpool.storagedrivers:
                if sd.storagerouter_guid == mds_service.service.storagerouter_guid:
                    storagedriver_id = sd.storagedriver_id
        for i in xrange(start_id, start_id + amount):
            vdisk = VDisk()
            vdisk.name = str(i)
            vdisk.devicename = 'vdisk_{0}'.format(i)
            vdisk.volume_id = 'vdisk_{0}'.format(i)
            vdisk.vpool = mds_service.vpool if mds_service is not None else vpool
            vdisk.size = 0
            vdisk.save()
            vdisk.reload_client('storagedriver')
            if mds_service is not None and mds_service.service.storagerouter_guid is not None:
                junction = MDSServiceVDisk()
                junction.vdisk = vdisk
                junction.mds_service = mds_service
                junction.is_master = True
                junction.save()
                config = type('MDSNodeConfig', (),
                              {'address': Helper.generate_nc_function(True, mds_service),
                               'port': Helper.generate_nc_function(False, mds_service)})()
                mds_backend_config = type('MDSMetaDataBackendConfig', (),
                                          {'node_configs': Helper.generate_bc_function([config])})()
                StorageRouterClient.metadata_backend_config[vdisk.vpool_guid]['vdisk_{0}'.format(i)] = mds_backend_config
                MDSClient.catchup['vdisk_{0}'.format(i)] = 50
                StorageRouterClient.vrouter_id[vdisk.vpool_guid]['vdisk_{0}'.format(i)] = storagedriver_id
            vdisks[i] = vdisk
        return vdisks

    @staticmethod
    def _set_vpool_storage_driver_configuration(vpool, storagedriver, configuration=None):
        """
        Mock the vpool configuration
        :param vpool: vPool to mock the configuration for
        :type vpool: vPool
        :param storagedriver: StorageDriver on which the vPool is running
        :type storagedriver: StorageDriver
        :param configuration: If not specified, default configuration is used, else default configuration is extended with specified configuration
        :type configuration: dict
        :return: None
        """
        default_config = {'backend_connection_manager': {'local_connection_path': ''},
                          'content_addressed_cache': {'read_cache_serialization_path': '/var/rsp/{0}'.format(vpool.name)},
                          'distributed_lock_store': {},
                          'distributed_transaction_log': {'dtl_path': '',
                                                          'dtl_transport': 'TCP'},
                          'event_publisher': {},
                          'file_driver': {'fd_cache_path': '',
                                          'fd_namespace': 'fd-{0}-{1}'.format(vpool.name, vpool.guid)},
                          'filesystem': {'fs_dtl_mode': StorageDriverClient.VOLDRV_DTL_ASYNC,
                                         'fs_dtl_config_mode': StorageDriverClient.VOLDRV_DTL_AUTOMATIC_MODE,
                                         'fs_virtual_disk_format': 'raw'},
                          'metadata_server': {},
                          'network_interface': {},
                          'scocache': {'trigger_gap': '1GB',
                                       'backoff_gap': '2GB',
                                       'scocache_mount_points': [{'path': '',
                                                                  'size': '{0}KiB'.format(20 * 1024 * 1024)}]},
                          'threadpool_component': {},
                          'volume_manager': {'metadata_path': '',
                                             'tlog_path': '',
                                             'clean_interval': 1,
                                             'default_cluster_size': 4096,
                                             'number_of_scos_in_tlog': 16,
                                             'read_cache_default_mode': StorageDriverClient.VOLDRV_LOCATION_BASED,
                                             'non_disposable_scos_factor': 2.0,
                                             'read_cache_default_behaviour': StorageDriverClient.VOLDRV_NO_CACHE},
                          'volume_registry': {'vregistry_arakoon_cluster_id': 'voldrv',
                                              'vregistry_arakoon_cluster_nodes': []},
                          'volume_router': {'vrouter_id': storagedriver.storagedriver_id,
                                            'vrouter_sco_multiplier': 1024},
                          'volume_router_cluster': {'vrouter_cluster_id': vpool.guid}}

        if configuration is not None:
            sd_params = StorageDriverConfiguration.parameters.get('storagedriver', {})
            if not isinstance(configuration, dict):
                raise ValueError('Configuration specified should be of type dict')
            for key, value in configuration.iteritems():
                if key not in sd_params:
                    raise KeyError('Unknown key "{0}" specified'.format(key))
                if not isinstance(value, dict):
                    raise ValueError('Configuration for key "{0}" should be of type dict'.format(key))
                known_params = sd_params[key].get('mandatory', []) + sd_params[key].get('optional', [])
                for sub_key in value.iterkeys():
                    if sub_key not in known_params:
                        raise ValueError('Parameter "{0}" in section "{1}" is unknown'.format(sub_key, key))
            default_config.update(configuration)

        key = '/ovs/vpools/{0}/hosts/{1}/config'.format(vpool.guid, storagedriver.storagedriver_id)
        Configuration.set(key, default_config)
