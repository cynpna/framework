# Copyright (C) 2017 iNuron NV
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
MigrationController module
"""
import copy
import json
from ovs.lib.helpers.decorators import ovs_task
from ovs.lib.helpers.toolbox import Schedule
from ovs.log.log_handler import LogHandler


class MigrationController(object):
    """
    This controller contains (part of the) migration code. It runs out-of-band with the updater so we reduce the risk of
    failures during the update
    """
    _logger = LogHandler.get('lib', name='migrations')

    @staticmethod
    @ovs_task(name='ovs.migration.migrate', schedule=Schedule(minute='0', hour='6'), ensure_single_info={'mode': 'DEFAULT'})
    def migrate():
        """
        Executes async migrations. It doesn't matter too much when they are executed, as long as they get eventually
        executed. This code will typically contain:
        * "dangerous" migration code (it needs certain running services)
        * Migration code depending on a cluster-wide state
        * ...
        """
        MigrationController._logger.info('Preparing out of band migrations...')

        from ovs.dal.lists.storagedriverlist import StorageDriverList
        from ovs.dal.lists.storagerouterlist import StorageRouterList
        from ovs.dal.lists.vpoollist import VPoolList
        from ovs.extensions.generic.configuration import Configuration
        from ovs.extensions.generic.sshclient import SSHClient
        from ovs.extensions.generic.toolbox import ExtensionsToolbox
        from ovs.extensions.services.service import ServiceManager
        from ovs.extensions.services.systemd import Systemd
        from ovs.extensions.storageserver.storagedriver import StorageDriverConfiguration
        from ovs.lib.generic import GenericController

        MigrationController._logger.info('Start out of band migrations...')

        sr_client_map = {}
        for storagerouter in StorageRouterList.get_storagerouters():
            sr_client_map[storagerouter.guid] = SSHClient(endpoint=storagerouter,
                                                          username='root')

        #########################################################
        # Addition of 'ExecReload' for AlbaProxy SystemD services
        getattr(ServiceManager, 'has_service')  # Invoke ServiceManager to fill out the ImplementationClass (default None)
        if ServiceManager.ImplementationClass == Systemd:  # Upstart does not have functionality to reload a process' configuration
            changed_clients = set()
            for storagedriver in StorageDriverList.get_storagedrivers():
                root_client = sr_client_map[storagedriver.storagerouter_guid]
                for alba_proxy in storagedriver.alba_proxies:
                    service = alba_proxy.service
                    service_name = 'ovs-{0}'.format(service.name)
                    if not ServiceManager.has_service(name=service_name, client=root_client):
                        continue
                    if 'ExecReload=' in root_client.file_read(filename='/lib/systemd/system/{0}.service'.format(service_name)):
                        continue

                    try:
                        ServiceManager.regenerate_service(name='ovs-albaproxy', client=root_client, target_name=service_name)
                        changed_clients.add(root_client)
                    except:
                        MigrationController._logger.exception('Error rebuilding service {0}'.format(service_name))
            for root_client in changed_clients:
                root_client.run(['systemctl', 'daemon-reload'])

        ##################################################################
        # Adjustment of open file descriptors for Arakoon services to 8192
        getattr(ServiceManager, 'has_service')  # Invoke ServiceManager to fill out the ImplementationClass (default None)
        changed_clients = set()
        for storagerouter in StorageRouterList.get_storagerouters():
            root_client = sr_client_map[storagerouter.guid]
            for service_name in ServiceManager.list_services(client=root_client):
                if not service_name.startswith('ovs-arakoon-'):
                    continue

                if ServiceManager.ImplementationClass == Systemd:
                    path = '/lib/systemd/system/{0}.service'.format(service_name)
                    check = 'LimitNOFILE=8192'
                else:
                    path = '/etc/init/{0}.conf'.format(service_name)
                    check = 'limit nofile 8192 8192'

                if not root_client.file_exists(path):
                    continue
                if check in root_client.file_read(path):
                    continue

                try:
                    ServiceManager.regenerate_service(name='ovs-arakoon', client=root_client, target_name=service_name)
                    changed_clients.add(root_client)
                    ExtensionsToolbox.edit_version_file(client=root_client, package_name='arakoon', old_service_name=service_name)
                except:
                    MigrationController._logger.exception('Error rebuilding service {0}'.format(service_name))
        for root_client in changed_clients:
            root_client.run(['systemctl', 'daemon-reload'])

        #############################
        # Migrate to multiple proxies
        for storagedriver in StorageDriverList.get_storagedrivers():
            vpool = storagedriver.vpool
            root_client = sr_client_map[storagedriver.storagerouter_guid]
            for alba_proxy in storagedriver.alba_proxies:
                # Rename alba_proxy service in model
                service = alba_proxy.service
                old_service_name = 'albaproxy_{0}'.format(vpool.name)
                new_service_name = 'albaproxy_{0}_0'.format(vpool.name)
                if old_service_name != service.name:
                    continue
                service.name = new_service_name
                service.save()

                if not ServiceManager.has_service(name=old_service_name, client=root_client):
                    continue
                old_configuration_key = '/ovs/framework/hosts/{0}/services/{1}'.format(storagedriver.storagerouter.machine_id, old_service_name)
                if not Configuration.exists(key=old_configuration_key):
                    continue

                # Add '-reboot' to alba_proxy services (because of newly created services and removal of old service)
                ExtensionsToolbox.edit_version_file(client=root_client,
                                                    package_name='alba',
                                                    old_service_name=old_service_name,
                                                    new_service_name=new_service_name)

                # Register new service and remove old service
                ServiceManager.add_service(name='ovs-albaproxy',
                                           client=root_client,
                                           params=Configuration.get(old_configuration_key),
                                           target_name='ovs-{0}'.format(new_service_name))

                # Update scrub proxy config
                proxy_config_key = '/ovs/vpools/{0}/proxies/{1}/config/main'.format(vpool.guid, alba_proxy.guid)
                proxy_config = None if Configuration.exists(key=proxy_config_key) is False else Configuration.get(proxy_config_key)
                if proxy_config is not None:
                    fragment_cache = proxy_config.get('fragment_cache', ['none', {}])
                    if fragment_cache[0] == 'alba' and fragment_cache[1].get('cache_on_write') is True:  # Accelerated ALBA configured
                        fragment_cache_scrub_info = copy.deepcopy(fragment_cache)
                        fragment_cache_scrub_info[1]['cache_on_read'] = False
                        proxy_scrub_config_key = '/ovs/vpools/{0}/proxies/scrub/generic_scrub'.format(vpool.guid)
                        proxy_scrub_config = None if Configuration.exists(key=proxy_scrub_config_key) is False else Configuration.get(proxy_scrub_config_key)
                        if proxy_scrub_config is not None and proxy_scrub_config['fragment_cache'] == ['none']:
                            proxy_scrub_config['fragment_cache'] = fragment_cache_scrub_info
                            Configuration.set(proxy_scrub_config_key,
                                              json.dumps(proxy_scrub_config, indent=4),
                                              raw=True)

            # Update 'backend_connection_manager' section
            changes = False
            storagedriver_config = StorageDriverConfiguration('storagedriver', vpool.guid, storagedriver.storagedriver_id)
            storagedriver_config.load()
            if 'backend_connection_manager' not in storagedriver_config.configuration:
                continue

            current_config = storagedriver_config.configuration['backend_connection_manager']
            if current_config.get('backend_type') != 'MULTI':
                changes = True
                backend_connection_manager = {'backend_type': 'MULTI'}
                for index, proxy in enumerate(sorted(storagedriver.alba_proxies, key=lambda pr: pr.service.ports[0])):
                    backend_connection_manager[str(index)] = copy.deepcopy(current_config)
                    # noinspection PyUnresolvedReferences
                    backend_connection_manager[str(index)]['alba_connection_use_rora'] = True
                    # noinspection PyUnresolvedReferences
                    backend_connection_manager[str(index)]['alba_connection_rora_manifest_cache_capacity'] = 16 * 1024 ** 3
                    # noinspection PyUnresolvedReferences
                    for key, value in backend_connection_manager[str(index)].items():
                        if key.startswith('backend_interface'):
                            backend_connection_manager[key] = value
                            # noinspection PyUnresolvedReferences
                            del backend_connection_manager[str(index)][key]
                for key, value in {'backend_interface_retries_on_error': 5,
                                   'backend_interface_retry_interval_secs': 1,
                                   'backend_interface_retry_backoff_multiplier': 2.0}.iteritems():
                    if key not in backend_connection_manager:
                        backend_connection_manager[key] = value
            else:
                backend_connection_manager = current_config
                for value in backend_connection_manager.values():
                    if isinstance(value, dict):
                        for key, val in value.items():
                            if key.startswith('backend_interface'):
                                backend_connection_manager[key] = val
                                changes = True
                                del value[key]
                for key, value in {'backend_interface_retries_on_error': 5,
                                   'backend_interface_retry_interval_secs': 1,
                                   'backend_interface_retry_backoff_multiplier': 2.0}.iteritems():
                    if key not in backend_connection_manager:
                        changes = True
                        backend_connection_manager[key] = value

            if changes is True:
                storagedriver_config.clear_backend_connection_manager()
                storagedriver_config.configure_backend_connection_manager(**backend_connection_manager)
                storagedriver_config.save(root_client)

                # Add '-reboot' to volumedriver services (because of updated 'backend_connection_manager' section)
                ExtensionsToolbox.edit_version_file(client=root_client,
                                                    package_name='volumedriver',
                                                    old_service_name='volumedriver_{0}'.format(vpool.name))
                if ServiceManager.ImplementationClass == Systemd:
                    root_client.run(['systemctl', 'daemon-reload'])

        ########################################
        # Update metadata_store_bits information
        getattr(ServiceManager, 'has_service')  # Invoke ServiceManager to fill out the ImplementationClass (default None)
        for vpool in VPoolList.get_vpools():
            bits = None
            for storagedriver in vpool.storagedrivers:
                if bits is None:
                    entries = ServiceManager.extract_from_service_file(name='ovs-volumedriver_{0}'.format(vpool.name),
                                                                       client=sr_client_map[storagedriver.storagerouter_guid],
                                                                       entries=['METADATASTORE_BITS='])
                    if len(entries) == 1:
                        bits = entries[0].split('=')[-1]
                        bits = int(bits) if bits.isdigit() else 5

                if bits is not None:
                    try:
                        key = '/ovs/framework/hosts/{0}/services/volumedriver_{1}'.format(storagedriver.storagerouter.machine_id, vpool.name)
                        content = Configuration.get(key=key)
                        if 'METADATA_STORE_BITS' not in content:
                            content['METADATA_STORE_BITS'] = bits
                            Configuration.set(key=key, value=content)
                    except:
                        MigrationController._logger.exception('Error updating volumedriver info for vPool {0} on StorageRouter {1}'.format(vpool.name, storagedriver.storagerouter.name))

            vpool.metadata_store_bits = bits
            vpool.save()

        MigrationController._logger.info('Finished out of band migrations')
        GenericController.refresh_package_information()
