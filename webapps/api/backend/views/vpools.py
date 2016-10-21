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
VPool module
"""

import time
from rest_framework import viewsets
from rest_framework.decorators import link, action
from rest_framework.permissions import IsAuthenticated
from backend.decorators import required_roles, load, return_list, return_object, return_task, return_plain, log
from backend.exceptions import HttpNotAcceptableException
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.lib.storagerouter import StorageRouterController
from ovs.lib.vdisk import VDiskController


class VPoolViewSet(viewsets.ViewSet):
    """
    Information about vPools
    """
    permission_classes = (IsAuthenticated,)
    prefix = r'vpools'
    base_name = 'vpools'

    @log()
    @required_roles(['read', 'manage'])
    @return_list(VPool, 'name')
    @load()
    def list(self):
        """
        Overview of all vPools
        """
        return VPoolList.get_vpools()

    @log()
    @required_roles(['read', 'manage'])
    @return_object(VPool)
    @load(VPool)
    def retrieve(self, vpool):
        """
        Load information about a given vPool
        :param vpool: vPool object to retrieve
        """
        return vpool

    @link()
    @log()
    @required_roles(['read'])
    @return_list(StorageRouter)
    @load(VPool)
    def storagerouters(self, vpool, hints):
        """
        Retrieves a list of StorageRouters, serving a given vPool
        :param vpool: vPool to retrieve the storagerouter information for
        :param hints: Dictionary with hints
        """
        if hints.get('full', False) is True:
            return [storagedriver.storagerouter for storagedriver in vpool.storagedrivers]
        return [storagedriver.storagerouter_guid for storagedriver in vpool.storagedrivers]

    @action()
    @log()
    @required_roles(['read', 'write', 'manage'])
    @return_task()
    @load(VPool)
    def shrink_vpool(self, vpool, storagerouter_guid):
        """
        Remove the storagedriver linking the specified vPool and storagerouter_guid
        :param vpool: vPool to shrink (or delete if its the last storagerouter linked to it)
        :param storagerouter_guid: Guid of the Storage Router
        :return: Celery tasks' async result
        """
        sr = StorageRouter(storagerouter_guid)
        sd_guid = None
        sd_guids = [storagedriver.guid for storagedriver in vpool.storagedrivers]
        for storagedriver in sr.storagedrivers:
            if storagedriver.guid in sd_guids:
                sd_guid = storagedriver.guid
                break
        if sd_guid is None:
            raise HttpNotAcceptableException(error_description='Storage Router {0} is not a member of vPool {1}'.format(sr.name, vpool.name),
                                             error='impossible_request')
        return StorageRouterController.remove_storagedriver.delay(sd_guid)

    @action()
    @log()
    @required_roles(['read', 'write', 'manage'])
    @return_task()
    @load(VPool)
    def update_storagedrivers(self, vpool, storagedriver_guid, version, storagerouter_guids=None, storagedriver_guids=None):
        """
        Update Storage Drivers for a given vPool (both adding and removing Storage Drivers)
        :param vpool: vPool to update
        :param storagedriver_guid: Storage Driver to update
        :param version: Version of API
        :param storagerouter_guids: Storage Router guids
        :param storagedriver_guids: Storage Driver guids
        :return: Celery task
        """
        if version > 1:
            raise HttpNotAcceptableException(error_description='Only available in API version 1',
                                             error='invalid_version')
        storagerouters = []
        if storagerouter_guids is not None:
            if storagerouter_guids.strip() != '':
                for storagerouter_guid in storagerouter_guids.strip().split(','):
                    storagerouter = StorageRouter(storagerouter_guid)
                    storagerouters.append((storagerouter.ip, storagerouter.machine_id))
        valid_storagedriver_guids = []
        if storagedriver_guids is not None:
            if storagedriver_guids.strip() != '':
                for storagedriver_guid in storagedriver_guids.strip().split(','):
                    storagedriver = StorageDriver(storagedriver_guid)
                    if storagedriver.vpool_guid != vpool.guid:
                        raise HttpNotAcceptableException(error_description='Given Storage Driver does not belong to this vPool',
                                                         error='impossible_request')
                    valid_storagedriver_guids.append(storagedriver.guid)

        storagedriver = StorageDriver(storagedriver_guid)
        parameters = {'connection_host': None if vpool.connection is None else vpool.connection.split(':')[0],
                      'connection_port': None if vpool.connection is None else int(vpool.connection.split(':')[1]),
                      'connection_username': vpool.login,
                      'connection_password': vpool.password,
                      'storage_ip': storagedriver.storage_ip,
                      'type': vpool.backend_type.code,
                      'vpool_name': vpool.name}
        for field in parameters:
            if isinstance(parameters[field], basestring):
                parameters[field] = str(parameters[field])

        return StorageRouterController.update_storagedrivers.delay(valid_storagedriver_guids, storagerouters, parameters)

    @link()
    @log()
    @required_roles(['read'])
    @return_plain()
    @load(VPool)
    def devicename_exists(self, vpool, name=None, names=None):
        """
        Checks whether a given name can be created on the vpool
        :param vpool: vPool object
        :param name: Candidate name
        :param names: Candidate names
        :return: True or False
        """
        error_message = None
        if not (name is None) ^ (names is None):
            error_message = 'Either the name (string) or the names (list of strings) parameter must be passed'
        if name is not None and not isinstance(name, basestring):
            error_message = 'The name parameter must be a string'
        if names is not None and not isinstance(names, list):
            error_message = 'The names parameter must be a list of strings'
        if error_message is not None:
            raise HttpNotAcceptableException(error_description=error_message,
                                             error='impossible_request')

        if name is not None:
            devicename = VDiskController.clean_devicename(name)
            return VDiskList.get_by_devicename_and_vpool(devicename, vpool) is not None
        for name in names:
            devicename = VDiskController.clean_devicename(name)
            if VDiskList.get_by_devicename_and_vpool(devicename, vpool) is not None:
                return True
        return False

    @action()
    @log()
    @required_roles(['read', 'write'])
    @return_task()
    @load(VPool)
    def create_snapshots(self, vdisk_guids, name, timestamp=None, consistent=False, automatic=False, sticky=False):
        """
        Creates snapshots for a list of VDisks
        :param vdisk_guids: Guids of the virtual disks to create snapshot from
        :type vdisk_guids: list
        :param name: Name of the snapshot (label)
        :type name: str
        :param timestamp: Timestamp of the snapshot
        :type timestamp: int
        :param consistent: Flag - is_consistent
        :type consistent: bool
        :param automatic: Flag - is_automatic
        :type automatic: bool
        :param sticky: Flag - is_sticky
        :type sticky: bool
        """
        if timestamp is None:
            timestamp = str(int(time.time()))
        metadata = {'label': name,
                    'timestamp': timestamp,
                    'is_consistent': True if consistent else False,
                    'is_sticky': True if sticky else False,
                    'is_automatic': True if automatic else False}
        return VDiskController.create_snapshots.delay(vdisk_guids=vdisk_guids,
                                                      metadata=metadata)

    @action()
    @log()
    @required_roles(['read', 'write'])
    @return_task()
    @load(VPool)
    def remove_snapshots(self, snapshot_mapping):
        """
        Remove a snapshot from a list of VDisks
        :param snapshot_mapping: Dict containing vDisk guid / Snapshot ID pairs
        :type snapshot_mapping: dict
        """
        return VDiskController.delete_snapshots.delay(snapshot_mapping=snapshot_mapping)
