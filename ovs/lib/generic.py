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
GenericTaskController module
"""

import copy
import time
import uuid
import socket
from datetime import datetime, timedelta
from Queue import Empty, Queue
from threading import Thread
from time import mktime
from ovs.dal.hybrids.diskpartition import DiskPartition
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.storagerouterlist import StorageRouterList
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.extensions.generic.configuration import Configuration
from ovs_extensions.generic.filemutex import file_mutex
from ovs.extensions.generic.logger import Logger
from ovs_extensions.generic.remote import remote
from ovs.extensions.generic.sshclient import NotAuthenticatedException, SSHClient, UnableToConnectException, TimeOutException
from ovs.extensions.generic.system import System
from ovs_extensions.generic.toolbox import ExtensionsToolbox
from ovs.extensions.generic.volatilemutex import volatile_mutex
from ovs.extensions.packages.packagefactory import PackageFactory
from ovs.extensions.services.servicefactory import ServiceFactory
from ovs.extensions.storage.volatilefactory import VolatileFactory
from ovs.lib.helpers.arakoon import ArakoonHelper
from ovs.lib.helpers.decorators import ovs_task
from ovs.lib.helpers.toolbox import Toolbox, Schedule
from ovs.lib.mdsservice import MDSServiceController
from ovs.lib.vdisk import VDiskController


class GenericController(object):
    """
    This controller contains all generic task code. These tasks can be
    executed at certain intervals and should be self-containing
    """
    _logger = Logger('lib')

    @staticmethod
    @ovs_task(name='ovs.generic.snapshot_all_vdisks', schedule=Schedule(minute='0', hour='*'), ensure_single_info={'mode': 'DEFAULT', 'extra_task_names': ['ovs.generic.delete_snapshots']})
    def snapshot_all_vdisks():
        """
        Snapshots all vDisks
        """
        GenericController._logger.info('[SSA] started')
        success = []
        fail = []
        for vdisk in VDiskList.get_vdisks():
            if vdisk.is_vtemplate is True:
                continue
            try:
                metadata = {'label': '',
                            'is_consistent': False,
                            'timestamp': str(int(time.time())),
                            'is_automatic': True,
                            'is_sticky': False}
                VDiskController.create_snapshot(vdisk_guid=vdisk.guid,
                                                metadata=metadata)
                success.append(vdisk.guid)
            except Exception:
                GenericController._logger.exception('Error taking snapshot for vDisk {0}'.format(vdisk.guid))
                fail.append(vdisk.guid)
        GenericController._logger.info('[SSA] Snapshot has been taken for {0} vDisks, {1} failed.'.format(len(success), len(fail)))
        return success, fail

    @staticmethod
    @ovs_task(name='ovs.generic.delete_snapshots', schedule=Schedule(minute='1', hour='2'), ensure_single_info={'mode': 'DEFAULT'})
    def delete_snapshots(timestamp=None):
        """
        Delete snapshots & scrubbing policy

        Implemented delete snapshot policy:
        < 1d | 1d bucket | 1 | best of bucket   | 1d
        < 1w | 1d bucket | 6 | oldest of bucket | 7d = 1w
        < 1m | 1w bucket | 3 | oldest of bucket | 4w = 1m
        > 1m | delete

        :param timestamp: Timestamp to determine whether snapshots should be kept or not, if none provided, current time will be used
        :type timestamp: float

        :return: None
        """
        GenericController._logger.info('Delete snapshots started')

        day = timedelta(1)
        week = day * 7

        def make_timestamp(offset):
            """
            Create an integer based timestamp
            :param offset: Offset in days
            :return: Timestamp
            """
            return int(mktime((base - offset).timetuple()))

        # Calculate bucket structure
        if timestamp is None:
            timestamp = time.time()
        base = datetime.fromtimestamp(timestamp).date() - day
        buckets = []
        # Buckets first 7 days: [0-1[, [1-2[, [2-3[, [3-4[, [4-5[, [5-6[, [6-7[
        for i in xrange(0, 7):
            buckets.append({'start': make_timestamp(day * i),
                            'end': make_timestamp(day * (i + 1)),
                            'type': '1d',
                            'snapshots': []})
        # Week buckets next 3 weeks: [7-14[, [14-21[, [21-28[
        for i in xrange(1, 4):
            buckets.append({'start': make_timestamp(week * i),
                            'end': make_timestamp(week * (i + 1)),
                            'type': '1w',
                            'snapshots': []})
        buckets.append({'start': make_timestamp(week * 4),
                        'end': 0,
                        'type': 'rest',
                        'snapshots': []})

        # Get a list of all snapshots that are used as parents for clones
        parent_snapshots = set([vd.parentsnapshot for vd in VDiskList.get_with_parent_snaphots()])

        # Place all snapshots in bucket_chains
        bucket_chains = []
        for vdisk in VDiskList.get_vdisks():
            if vdisk.info['object_type'] in ['BASE']:
                bucket_chain = copy.deepcopy(buckets)
                for snapshot in vdisk.snapshots:
                    if snapshot.get('is_sticky') is True:
                        continue
                    if snapshot['guid'] in parent_snapshots:
                        GenericController._logger.info('Not deleting snapshot {0} because it has clones'.format(snapshot['guid']))
                        continue
                    timestamp = int(snapshot['timestamp'])
                    for bucket in bucket_chain:
                        if bucket['start'] >= timestamp > bucket['end']:
                            bucket['snapshots'].append({'timestamp': timestamp,
                                                        'snapshot_id': snapshot['guid'],
                                                        'vdisk_guid': vdisk.guid,
                                                        'is_consistent': snapshot['is_consistent']})
                bucket_chains.append(bucket_chain)

        # Clean out the snapshot bucket_chains, we delete the snapshots we want to keep
        # And we'll remove all snapshots that remain in the buckets
        for bucket_chain in bucket_chains:
            first = True
            for bucket in bucket_chain:
                if first is True:
                    best = None
                    for snapshot in bucket['snapshots']:
                        if best is None:
                            best = snapshot
                        # Consistent is better than inconsistent
                        elif snapshot['is_consistent'] and not best['is_consistent']:
                            best = snapshot
                        # Newer (larger timestamp) is better than older snapshots
                        elif snapshot['is_consistent'] == best['is_consistent'] and \
                                snapshot['timestamp'] > best['timestamp']:
                            best = snapshot
                    bucket['snapshots'] = [s for s in bucket['snapshots'] if
                                           s['timestamp'] != best['timestamp']]
                    first = False
                elif bucket['end'] > 0:
                    oldest = None
                    for snapshot in bucket['snapshots']:
                        if oldest is None:
                            oldest = snapshot
                        # Older (smaller timestamp) is the one we want to keep
                        elif snapshot['timestamp'] < oldest['timestamp']:
                            oldest = snapshot
                    bucket['snapshots'] = [s for s in bucket['snapshots'] if
                                           s['timestamp'] != oldest['timestamp']]

        # Delete obsolete snapshots
        for bucket_chain in bucket_chains:
            for bucket in bucket_chain:
                for snapshot in bucket['snapshots']:
                    VDiskController.delete_snapshot(vdisk_guid=snapshot['vdisk_guid'],
                                                    snapshot_id=snapshot['snapshot_id'])
        GenericController._logger.info('Delete snapshots finished')

    @staticmethod
    @ovs_task(name='ovs.generic.execute_scrub', schedule=Schedule(minute='0', hour='3'), ensure_single_info={'mode': 'DEDUPED'})
    def execute_scrub(vpool_guids=None, vdisk_guids=None, storagerouter_guid=None, manual=False):
        """
        Divide the scrub work among all StorageRouters with a SCRUB partition
        :param vpool_guids: Guids of the vPools that need to be scrubbed completely
        :type vpool_guids: list
        :param vdisk_guids: Guids of the vDisks that need to be scrubbed
        :type vdisk_guids: list
        :param storagerouter_guid: Guid of the StorageRouter to execute the scrub work on
        :type storagerouter_guid: str
        :param manual: Indicator whether the execute_scrub is called manually or as scheduled task (automatically)
        :type manual: bool
        :return: None
        :rtype: NoneType
        """
        if vdisk_guids is None:
            vdisk_guids = []
        if vpool_guids is None:
            vpool_guids = []
        if not isinstance(vpool_guids, list):
            raise ValueError('vpool_guids should be a list')
        if not isinstance(vdisk_guids, list):
            raise ValueError('vdisk_guids should be a list')
        if storagerouter_guid is not None and not isinstance(storagerouter_guid, basestring):
            raise ValueError('storagerouter_guid should be a str')

        GenericController._logger.info('Scrubber - Started')
        if manual is True:
            vpool_vdisk_map = {}
            for vpool_guid in set(vpool_guids):
                vpool = VPool(vpool_guid)
                vpool_vdisk_map[vpool] = list(vpool.vdisks)
            for vdisk_guid in set(vdisk_guids):
                vdisk = VDisk(vdisk_guid)
                if vdisk.vpool not in vpool_vdisk_map:
                    vpool_vdisk_map[vdisk.vpool] = []
                if vdisk not in vpool_vdisk_map[vdisk.vpool]:
                    vpool_vdisk_map[vdisk.vpool].append(vdisk)
        else:
            vpool_vdisk_map = dict((vpool, list(vpool.vdisks)) for vpool in VPoolList.get_vpools())

        if len(vpool_vdisk_map) == 0:
            GenericController._logger.info('Scrubber - Nothing to scrub')
            return

        scrub_locations = []
        storagerouters = StorageRouterList.get_storagerouters() if storagerouter_guid is None else [StorageRouter(storagerouter_guid)]
        for storage_router in storagerouters:
            scrub_partitions = storage_router.partition_config.get(DiskPartition.ROLES.SCRUB, [])
            if len(scrub_partitions) == 0:
                continue

            try:
                SSHClient(endpoint=storage_router, username='root')
                for partition_guid in scrub_partitions:
                    partition = DiskPartition(partition_guid)
                    GenericController._logger.info('Scrubber - Storage Router {0} has {1} partition at {2}'.format(storage_router.ip, DiskPartition.ROLES.SCRUB, partition.folder))
                    scrub_locations.append({'scrub_path': str(partition.folder),
                                            'partition_guid': partition.guid,
                                            'storage_router': storage_router})
            except UnableToConnectException:
                GenericController._logger.warning('Scrubber - Storage Router {0} is not reachable'.format(storage_router.ip))

        if len(scrub_locations) == 0:
            raise ValueError('No scrub locations found, cannot scrub')

        number_of_vpools = len(vpool_vdisk_map)
        if number_of_vpools >= 6:
            max_stacks_per_vpool = 1
        elif number_of_vpools >= 3:
            max_stacks_per_vpool = 2
        else:
            max_stacks_per_vpool = 5

        threads = []
        counter = 0
        error_messages = []
        for vp, vdisks in vpool_vdisk_map.iteritems():
            # Verify amount of vDisks on vPool
            GenericController._logger.info('Scrubber - vPool {0} - Checking scrub work'.format(vp.name))
            if len(vdisks) == 0:
                GenericController._logger.info('Scrubber - vPool {0} - No scrub work'.format(vp.name))
                continue

            # Fill queue with all vDisks for current vPool
            vpool_queue = Queue()
            for vd in vdisks:
                if vd.is_vtemplate is True:
                    GenericController._logger.info('Scrubber - vPool {0} - vDisk {1} {2} - Is a template, not scrubbing'.format(vp.name, vd.guid, vd.name))
                    continue
                vd.invalidate_dynamics('storagedriver_id')
                if not vd.storagedriver_id:
                    GenericController._logger.warning('Scrubber - vPool {0} - vDisk {1} {2} - No StorageDriver ID found'.format(vp.name, vd.guid, vd.name))
                    continue
                vpool_queue.put(vd.guid)

            stacks_to_spawn = min(max_stacks_per_vpool, len(scrub_locations))
            GenericController._logger.info('Scrubber - vPool {0} - Spawning {1} stack{2}'.format(vp.name, stacks_to_spawn, '' if stacks_to_spawn == 1 else 's'))
            for _ in xrange(stacks_to_spawn):
                scrub_target = scrub_locations[counter % len(scrub_locations)]
                stack = Thread(target=GenericController._deploy_stack_and_scrub,
                               args=(vpool_queue, vp, scrub_target, error_messages))
                stack.start()
                threads.append(stack)
                counter += 1

        for thread in threads:
            thread.join()

        if len(error_messages) > 0:
            raise Exception('Errors occurred while scrubbing:\n  - {0}'.format('\n  - '.join(error_messages)))

    @staticmethod
    def _execute_scrub(queue, vpool, scrub_info, scrub_dir, error_messages):
        def _verify_mds_config(current_vdisk):
            current_vdisk.invalidate_dynamics('info')
            vdisk_configs = current_vdisk.info['metadata_backend_config']
            if len(vdisk_configs) == 0:
                raise RuntimeError('Could not load MDS configuration')
            return vdisk_configs

        storagerouter = scrub_info['storage_router']
        partition_guid = scrub_info['partition_guid']
        volatile_client = VolatileFactory.get_client()
        backend_config_key = 'ovs/vpools/{0}/proxies/scrub/backend_config_{1}'.format(vpool.guid, partition_guid)
        try:
            # Empty the queue with vDisks to scrub
            with remote(storagerouter.ip, [VDisk]) as rem:
                while True:
                    vdisk = None
                    vdisk_guid = queue.get(False)  # Raises Empty Exception when queue is empty, so breaking the while True loop
                    volatile_key = 'ovs_scrubbing_vdisk_{0}'.format(vdisk_guid)
                    try:
                        # Check MDS master is local. Trigger MDS handover if necessary
                        vdisk = rem.VDisk(vdisk_guid)
                        GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - Started scrubbing at location {3}'.format(vpool.name, storagerouter.name, vdisk.name, scrub_dir))
                        configs = _verify_mds_config(current_vdisk=vdisk)
                        storagedriver = StorageDriverList.get_by_storagedriver_id(vdisk.storagedriver_id)
                        if configs[0].get('ip') != storagedriver.storagerouter.ip:
                            GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - MDS master is not local, trigger handover'.format(vpool.name, storagerouter.name, vdisk.name))
                            MDSServiceController.ensure_safety(vdisk_guid=vdisk_guid)  # Do not use a remote VDisk instance here
                            configs = _verify_mds_config(current_vdisk=vdisk)
                            if configs[0].get('ip') != storagedriver.storagerouter.ip:
                                GenericController._logger.warning('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - Skipping because master MDS still not local'.format(vpool.name, storagerouter.name, vdisk.name))
                                continue

                        # Check if vDisk is already being scrubbed
                        if volatile_client.add(key=volatile_key, value=volatile_key, time=24 * 60 * 60) is False:
                            GenericController._logger.warning('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - Skipping because vDisk is already being scrubbed'.format(vpool.name, storagerouter.name, vdisk.name))
                            continue

                        # Do the actual scrubbing
                        with vdisk.storagedriver_client.make_locked_client(str(vdisk.volume_id)) as locked_client:
                            GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - Retrieve and apply scrub work'.format(vpool.name, storagerouter.name, vdisk.name))
                            work_units = locked_client.get_scrubbing_workunits()
                            for work_unit in work_units:
                                res = locked_client.scrub(work_unit=work_unit,
                                                          scratch_dir=scrub_dir,
                                                          log_sinks=[Logger.get_sink_path(source='scrubber_{0}'.format(vpool.name),
                                                                                          forced_target_type=Logger.TARGET_TYPE_FILE)],
                                                          backend_config=Configuration.get_configuration_path(backend_config_key))
                                locked_client.apply_scrubbing_result(scrubbing_work_result=res)
                            if work_units:
                                GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - {3} work units successfully applied'.format(vpool.name, storagerouter.name, vdisk.name, len(work_units)))
                            else:
                                GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - No scrubbing required'.format(vpool.name, storagerouter.name, vdisk.name))
                    except Exception:
                        if vdisk is None:
                            message = 'Scrubber - vPool {0} - StorageRouter {1} - vDisk with guid {2} could not be found'.format(vpool.name, storagerouter.name, vdisk_guid)
                        else:
                            message = 'Scrubber - vPool {0} - StorageRouter {1} - vDisk {2} - Scrubbing failed'.format(vpool.name, storagerouter.name, vdisk.name)
                        error_messages.append(message)
                        GenericController._logger.exception(message)
                    finally:
                        # Remove vDisk from volatile memory
                        volatile_client.delete(volatile_key)

        except Empty:  # Raised when all items have been fetched from the queue
            GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Queue completely processed'.format(vpool.name, storagerouter.name))
        except Exception:
            message = 'Scrubber - vPool {0} - StorageRouter {1} - Scrubbing failed'.format(vpool.name, storagerouter.name)
            error_messages.append(message)
            GenericController._logger.exception(message)

    @staticmethod
    def _deploy_stack_and_scrub(queue, vpool, scrub_info, error_messages):
        """
        Executes scrub work for a given vDisk queue and vPool, based on scrub_info
        :param queue: a Queue with vDisk guids that need to be scrubbed (they should only be member of a single vPool)
        :type queue: Queue
        :param vpool: the vPool object of the vDisks
        :type vpool: VPool
        :param scrub_info: A dict containing scrub information:
                           `scrub_path` with the path where to scrub
                           `storage_router` with the StorageRouter that needs to do the work
        :type scrub_info: dict
        :param error_messages: A list of error messages to be filled (by reference)
        :type error_messages: list
        :return: None
        :rtype: NoneType
        """
        if len(vpool.storagedrivers) == 0 or not vpool.storagedrivers[0].storagedriver_id:
            error_messages.append('vPool {0} does not have any valid StorageDrivers configured'.format(vpool.name))
            return

        alba_pkg_name, alba_version_cmd = PackageFactory.get_package_and_version_cmd_for(component=PackageFactory.COMP_ALBA)

        service_manager = ServiceFactory.get_manager()
        client = None
        lock_time = 5 * 60
        random_uuid = uuid.uuid4()
        storagerouter = scrub_info['storage_router']
        partition_guid = scrub_info['partition_guid']
        alba_proxy_service = 'ovs-albaproxy_{0}_scrub'.format(random_uuid)
        scrub_directory = '{0}/scrub_work_{1}'.format(scrub_info['scrub_path'], random_uuid)
        scrub_config_key = 'ovs/vpools/{0}/proxies/scrub/scrub_config_{1}'.format(vpool.guid, partition_guid)
        backend_config_key = 'ovs/vpools/{0}/proxies/scrub/backend_config_{1}'.format(vpool.guid, partition_guid)

        # Deploy a proxy
        try:
            with file_mutex(name='ovs_albaproxy_scrub', wait=lock_time):
                GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Deploying ALBA proxy {2}'.format(vpool.name, storagerouter.name, alba_proxy_service))
                client = SSHClient(storagerouter, 'root')
                client.dir_create(scrub_directory)
                client.dir_chmod(scrub_directory, 0777)  # Celery task executed by 'ovs' user and should be able to write in it
                if service_manager.has_service(name=alba_proxy_service, client=client) is True and service_manager.get_service_status(name=alba_proxy_service, client=client) == 'active':
                    GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Re-using existing proxy service {2}'.format(vpool.name, storagerouter.name, alba_proxy_service))
                    scrub_config = Configuration.get(scrub_config_key)
                else:
                    machine_id = System.get_my_machine_id(client)
                    port_range = Configuration.get('/ovs/framework/hosts/{0}/ports|storagedriver'.format(machine_id))
                    with volatile_mutex('deploy_proxy_for_scrub_{0}'.format(storagerouter.guid), wait=30):
                        port = System.get_free_ports(selected_range=port_range, nr=1, client=client)[0]
                    scrub_config = Configuration.get('ovs/vpools/{0}/proxies/scrub/generic_scrub'.format(vpool.guid))
                    scrub_config['port'] = port
                    scrub_config['transport'] = 'tcp'
                    Configuration.set(key=scrub_config_key, value=scrub_config)

                    params = {'VPOOL_NAME': vpool.name,
                              'LOG_SINK': Logger.get_sink_path(alba_proxy_service),
                              'CONFIG_PATH': Configuration.get_configuration_path(scrub_config_key),
                              'ALBA_PKG_NAME': alba_pkg_name,
                              'ALBA_VERSION_CMD': alba_version_cmd}
                    service_manager.add_service(name='ovs-albaproxy', params=params, client=client, target_name=alba_proxy_service)
                    service_manager.start_service(name=alba_proxy_service, client=client)
                    GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Deployed ALBA proxy {2}'.format(vpool.name, storagerouter.name, alba_proxy_service))

                backend_config = Configuration.get('ovs/vpools/{0}/hosts/{1}/config'.format(vpool.guid, vpool.storagedrivers[0].storagedriver_id))['backend_connection_manager']
                if backend_config.get('backend_type') != 'MULTI':
                    backend_config['alba_connection_host'] = '127.0.0.1'
                    backend_config['alba_connection_port'] = scrub_config['port']
                else:
                    for value in backend_config.itervalues():
                        if isinstance(value, dict):
                            value['alba_connection_host'] = '127.0.0.1'
                            value['alba_connection_port'] = scrub_config['port']
                # Copy backend connection manager information in separate key
                Configuration.set(key=backend_config_key, value={"backend_connection_manager": backend_config})
        except Exception:
            message = 'Scrubber - vPool {0} - StorageRouter {1} - An error occurred deploying ALBA proxy {2}'.format(vpool.name, storagerouter.name, alba_proxy_service)
            error_messages.append(message)
            GenericController._logger.exception(message)
            if client is not None and service_manager.has_service(name=alba_proxy_service, client=client) is True:
                if service_manager.get_service_status(name=alba_proxy_service, client=client) == 'active':
                    service_manager.stop_service(name=alba_proxy_service, client=client)
                service_manager.remove_service(name=alba_proxy_service, client=client)
            if Configuration.exists(scrub_config_key):
                Configuration.delete(scrub_config_key)

        # Execute the actual scrubbing
        threads = []
        threads_key = '/ovs/framework/hosts/{0}/config|scrub_stack_threads'.format(storagerouter.machine_id)
        amount_threads = Configuration.get(key=threads_key) if Configuration.exists(key=threads_key) else 2
        if not isinstance(amount_threads, int):
            error_messages.append('Amount of threads to spawn must be an integer for StorageRouter with ID {0}'.format(storagerouter.machine_id))
            return

        amount_threads = max(amount_threads, 1)  # Make sure amount_threads is at least 1
        amount_threads = min(min(queue.qsize(), amount_threads), 20)  # Make sure amount threads is max 20
        GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Spawning {2} threads for proxy service {3}'.format(vpool.name, storagerouter.name, amount_threads, alba_proxy_service))
        for index in range(amount_threads):
            thread = Thread(name='execute_scrub_{0}_{1}_{2}'.format(vpool.guid, partition_guid, index),
                            target=GenericController._execute_scrub,
                            args=(queue, vpool, scrub_info, scrub_directory, error_messages))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

        # Delete the proxy again
        try:
            with file_mutex(name='ovs_albaproxy_scrub', wait=lock_time):
                GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Removing service {2}'.format(vpool.name, storagerouter.name, alba_proxy_service))
                client = SSHClient(storagerouter, 'root')
                client.dir_delete(scrub_directory)
                if service_manager.has_service(alba_proxy_service, client=client):
                    service_manager.stop_service(alba_proxy_service, client=client)
                    service_manager.remove_service(alba_proxy_service, client=client)
                if Configuration.exists(scrub_config_key):
                    Configuration.delete(scrub_config_key)
                GenericController._logger.info('Scrubber - vPool {0} - StorageRouter {1} - Removed service {2}'.format(vpool.name, storagerouter.name, alba_proxy_service))
        except Exception:
            message = 'Scrubber - vPool {0} - StorageRouter {1} - Removing service {2} failed'.format(vpool.name, storagerouter.name, alba_proxy_service)
            error_messages.append(message)
            GenericController._logger.exception(message)

    @staticmethod
    @ovs_task(name='ovs.generic.collapse_arakoon', schedule=Schedule(minute='10', hour='0,2,4,6,8,10,12,14,16,18,20,22'), ensure_single_info={'mode': 'DEFAULT'})
    def collapse_arakoon():
        """
        Collapse Arakoon's Tlogs
        :return: None
        """
        GenericController._logger.info('Arakoon collapse started')
        workload = ArakoonHelper.get_basic_config()
        collapse_stats= ArakoonHelper.retrieve_collapse_stats(workload)
        for cluster_name, metadata in collapse_stats.iteritems():
            for node, stats in metadata['collapse_result'].iteritems():
                ip = node.ip
                node_id = metadata['ips:node_ids'][ip]
                identifier_log = 'Arakoon cluster {0} on node {1}'.format(cluster_name, ip)

                if len(stats['errors']) > 0:
                    # Determine where issues were found
                    for step, exception in stats['errors']:
                        if step == 'build_client':
                            try:
                                # Raise the thrown exception
                                raise exception
                            except TimeOutException:
                                GenericController._logger.error('Connection to {0} has timed out'.format(identifier_log))
                            except (socket.error, UnableToConnectException):
                                GenericController._logger.error('Connection to {0} could not be established'.format(identifier_log))
                            except NotAuthenticatedException:
                                GenericController._logger.error('Connection to {0} could not be authenticated. This node has no access to the Arakoon node.'.format(identifier_log))
                            except Exception:
                                message = 'Connection to {0} could not be established due to an unhandled exception.'.format(identifier_log)
                                GenericController._logger.exception(message)
                        elif step == 'stat_dir':
                            try:
                                raise exception
                            except Exception:
                                message = 'Unable to list the contents of the tlog directory ({0}) for {1}'.format(metadata['config'], cluster_name)
                                GenericController._logger.exception(message)
                    continue

                try:
                    storagerouter = StorageRouterList.get_by_ip(ip)
                    client = SSHClient(storagerouter)
                    headdb_files = stats['result']['headDB']
                    avail_size = stats['result']['avail_size']
                    headdb_size = sum([int(i[2]) for i in headdb_files])
                    # Check if there is enough memory for collapse
                    collapse_size_msg = 'Spare space for local collapse is '
                    if avail_size < 2 * headdb_size:
                        GenericController._logger.exception('{0} insufficient (n <2 x head.db size')
                    else:
                        if avail_size >= headdb_size * 4:
                            GenericController._logger.debug('{0} sufficient (n > 4x head.db size)'.format(collapse_size_msg))
                        elif avail_size >= headdb_size * 3:
                            GenericController._logger.debug('{0} running short (n > 3x head.db size)'.format(collapse_size_msg))
                        elif avail_size >= headdb_size * 2:
                            GenericController._logger.warning('{0} just enough (n > 2x head.db size'.format(collapse_size_msg))
                        # Collapse
                        try:
                            GenericController._logger.debug('  Collapsing cluster {0} on {1}'.format(cluster_name, storagerouter.ip))
                            client.run(['arakoon', '--collapse-local', node_id, '2', '-config', metadata['config'].external_config_path])
                            GenericController._logger.debug('  Collapsing cluster {0} on {1} completed'.format(cluster_name, storagerouter.ip))
                        except Exception:
                            GenericController._logger.exception('  Collapsing cluster {0} on {1} failed'.format(cluster_name, storagerouter.ip))

                except Exception:
                    GenericController._logger.error('  Could not collapse any cluster on {0} (not reachable)'.format(storagerouter.name))

        GenericController._logger.info('Arakoon collapse finished')

    @staticmethod
    @ovs_task(name='ovs.generic.refresh_package_information', schedule=Schedule(minute='10', hour='*'), ensure_single_info={'mode': 'DEFAULT'})
    def refresh_package_information():
        """
        Retrieve and store the package information of all StorageRouters
        :return: None
        """
        GenericController._logger.info('Updating package information')

        client_map = {}
        prerequisites = []
        package_info_cluster = {}
        all_storagerouters = StorageRouterList.get_storagerouters()
        all_storagerouters.sort(key=lambda sr: ExtensionsToolbox.advanced_sort(element=sr.ip, separator='.'))
        for storagerouter in all_storagerouters:
            package_info_cluster[storagerouter.ip] = {}
            try:
                # We make use of these clients in Threads --> cached = False
                client_map[storagerouter] = SSHClient(endpoint=storagerouter, username='root', cached=False)
            except (NotAuthenticatedException, UnableToConnectException):
                GenericController._logger.warning('StorageRouter {0} is inaccessible'.format(storagerouter.ip))
                prerequisites.append(['node_down', storagerouter.name])
                package_info_cluster[storagerouter.ip]['errors'] = ['StorageRouter {0} is inaccessible'.format(storagerouter.name)]

        # Retrieve for each StorageRouter in the cluster the installed and candidate versions of related packages
        # This also validates whether all required packages have been installed
        GenericController._logger.debug('Retrieving package information for the cluster')
        threads = []
        for storagerouter, client in client_map.iteritems():
            for fct in Toolbox.fetch_hooks(component='update', sub_component='get_package_info_cluster'):
                thread = Thread(target=fct, args=(client, package_info_cluster))
                thread.start()
                threads.append(thread)

        for thread in threads:
            thread.join()

        # Retrieve the related downtime / service restart information
        GenericController._logger.debug('Retrieving update information for the cluster')
        update_info_cluster = {}
        for storagerouter, client in client_map.iteritems():
            update_info_cluster[storagerouter.ip] = {'errors': package_info_cluster[storagerouter.ip].get('errors', [])}
            for fct in Toolbox.fetch_hooks(component='update', sub_component='get_update_info_cluster'):
                fct(client, update_info_cluster, package_info_cluster[storagerouter.ip])

        # Retrieve the update information for plugins (eg: ALBA, iSCSI)
        GenericController._logger.debug('Retrieving package and update information for the plugins')
        threads = []
        update_info_plugin = {}
        for fct in Toolbox.fetch_hooks('update', 'get_update_info_plugin'):
            thread = Thread(target=fct, args=(update_info_plugin, ))
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        # Add the prerequisites
        if len(prerequisites) > 0:
            for ip, component_info in update_info_cluster.iteritems():
                if PackageFactory.COMP_FWK in component_info:
                    component_info[PackageFactory.COMP_FWK]['prerequisites'].extend(prerequisites)

        # Store information in model and collect errors for OVS cluster
        errors = set()
        for storagerouter in all_storagerouters:
            GenericController._logger.debug('Storing update information for StorageRouter {0}'.format(storagerouter.ip))
            update_info = update_info_cluster.get(storagerouter.ip, {})

            # Remove the errors from the update information
            sr_errors = update_info.pop('errors', [])
            if len(sr_errors) > 0:
                errors.update(['{0}: {1}'.format(storagerouter.ip, error) for error in sr_errors])
                update_info = {}  # If any error occurred, we store no update information for this StorageRouter

            # Remove the components without updates from the update information
            update_info_copy = copy.deepcopy(update_info)
            for component, info in update_info_copy.iteritems():
                if len(info['packages']) == 0:
                    update_info.pop(component)

            # Store the update information
            storagerouter.package_information = update_info
            storagerouter.save()

        # Collect errors for plugins
        for ip, plugin_errors in update_info_plugin.iteritems():
            if len(plugin_errors) > 0:
                errors.update(['{0}: {1}'.format(ip, error) for error in plugin_errors])

        if len(errors) > 0:
            raise Exception('\n - {0}'.format('\n - '.join(errors)))
        GenericController._logger.info('Finished updating package information')

    @staticmethod
    @ovs_task(name='ovs.generic.run_backend_domain_hooks')
    def run_backend_domain_hooks(backend_guid):
        """
        Run hooks when the Backend Domains have been updated
        :param backend_guid: Guid of the Backend to update
        :type backend_guid: str
        :return: None
        """
        for fct in Toolbox.fetch_hooks('backend', 'domains-update'):
            fct(backend_guid=backend_guid)
