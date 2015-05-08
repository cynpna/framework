# Copyright 2014 CloudFounders NV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
StorageRouter module
"""
import os
import copy
import uuid
import json
from ConfigParser import RawConfigParser
from subprocess import check_output

from ovs.celery_run import celery
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.hybrids.j_albaproxy import AlbaProxy
from ovs.dal.hybrids.service import Service as DalService
from ovs.dal.lists.clientlist import ClientList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.storagerouterlist import StorageRouterList
from ovs.dal.lists.backendtypelist import BackendTypeList
from ovs.dal.lists.vmachinelist import VMachineList
from ovs.dal.lists.servicetypelist import ServiceTypeList
from ovs.extensions.api.client import OVSClient
from ovs.extensions.generic.system import System
from ovs.extensions.db.arakoon.ArakoonManagement import ArakoonManagementEx
from ovs.extensions.generic.sshclient import SSHClient
from ovs.extensions.generic.remote import Remote
from ovs.extensions.hypervisor.factory import Factory
from ovs.extensions.storage.persistentfactory import PersistentFactory
from ovs.extensions.storageserver.storagedriver import StorageDriverConfiguration
from ovs.extensions.support.agent import SupportAgent
from ovs.lib.mdsservice import MDSServiceController
from ovs.lib.helpers.toolbox import Toolbox
from ovs.log.logHandler import LogHandler
from ovs.plugin.provider.configuration import Configuration
from ovs.plugin.provider.package import Package
from ovs.plugin.provider.service import Service as PluginService
from volumedriver.storagerouter.storagerouterclient import ClusterRegistry, ArakoonNodeConfig, ClusterNodeConfig, LocalStorageRouterClient


logger = LogHandler('lib', name='storagerouter')


class StorageRouterController(object):
    """
    Contains all BLL related to StorageRouter
    """

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_physical_metadata')
    def get_physical_metadata(files, storagerouter_guid):
        """
        Gets physical information about the machine this task is running on
        """
        from ovs.lib.vpool import VPoolController

        storagerouter = StorageRouter(storagerouter_guid)
        mountpoints = check_output('mount -v', shell=True).strip().splitlines()
        mountpoints = [p.split(' ')[2] for p in mountpoints if len(p.split(' ')) > 2
                       and not p.split(' ')[2].startswith('/dev') and not p.split(' ')[2].startswith('/proc')
                       and not p.split(' ')[2].startswith('/sys') and not p.split(' ')[2].startswith('/run')
                       and p.split(' ')[2] != '/' and not p.split(' ')[2].startswith('/mnt/alba-asd')]
        arakoon_mountpoint = Configuration.get('ovs.core.db.arakoon.location')
        if arakoon_mountpoint in mountpoints:
            mountpoints.remove(arakoon_mountpoint)
        # include directories chosen during ovs setup
        readcaches = Configuration.get('ovs.vpool_partitions.readcaches').split(',')
        writecaches = Configuration.get('ovs.vpool_partitions.writecaches').split(',')
        storage = Configuration.get('ovs.vpool_partitions.storage').split(',')
        mountpoints.extend(storage)
        if storagerouter.pmachine.hvtype == 'KVM':
            ipaddresses = ['127.0.0.1']
        else:
            ipaddresses = check_output("ip a | grep 'inet ' | sed 's/\s\s*/ /g' | cut -d ' ' -f 3 | cut -d '/' -f 1", shell=True).strip().splitlines()
            ipaddresses = [ip.strip() for ip in ipaddresses]
            ipaddresses.remove('127.0.0.1')
        allow_vpool = VPoolController.can_be_served_on(storagerouter_guid)
        file_existence = {}
        for check_file in files:
            file_existence[check_file] = os.path.exists(check_file) and os.path.isfile(check_file)

        logger.info('mountpoints:{0}'.format(mountpoints))
        logger.info('readcaches:{0}'.format(readcaches))
        logger.info('writecaches:{0}'.format(writecaches))

        return {'mountpoints': mountpoints,
                'readcaches': readcaches,
                'writecaches': writecaches,
                'ipaddresses': ipaddresses,
                'files': file_existence,
                'allow_vpool': allow_vpool}

    @staticmethod
    @celery.task(name='ovs.storagerouter.add_vpool')
    def add_vpool(parameters):
        """
        Add a vPool to the machine this task is running on
        """
        required_params = {'vpool_name': (str, Toolbox.regex_vpool),
                           'storage_ip': (str, Toolbox.regex_ip),
                           'storagerouter_ip': (str, Toolbox.regex_ip),
                           'mountpoint_md': (str, Toolbox.regex_mountpoint),
                           'mountpoint_bfs': (str, Toolbox.regex_mountpoint, False),
                           'mountpoint_foc': (str, Toolbox.regex_mountpoint),
                           'mountpoint_temp': (str, Toolbox.regex_mountpoint),
                           'mountpoint_readcaches': (list, Toolbox.regex_mountpoint),
                           'mountpoint_writecaches': (list, Toolbox.regex_mountpoint)}
        required_params_wihout_vpool = {'type': (str, ['local', 'distributed', 'alba', 'ceph_s3', 'amazon_s3', 'swift_s3']),
                                        'connection_host': (str, Toolbox.regex_ip, False),
                                        'connection_port': (int, None),
                                        'connection_backend': (str, None),
                                        'connection_username': (str, None),
                                        'connection_password': (str, None)}

        if not isinstance(parameters, dict):
            raise ValueError('Parameters should be of type "dict"')
        Toolbox.verify_required_params(required_params, parameters)

        ip = parameters['storagerouter_ip']
        vpool_name = parameters['vpool_name']
        storage_ip = parameters['storage_ip']
        mountpoint_md = parameters['mountpoint_md']
        mountpoint_bfs = parameters['mountpoint_bfs']
        mountpoint_foc = parameters['mountpoint_foc']
        mountpoint_temp = parameters['mountpoint_temp']
        mountpoint_readcaches = parameters['mountpoint_readcaches']
        mountpoint_writecaches = parameters['mountpoint_writecaches']

        client = SSHClient(ip)
        root_client = SSHClient(ip, username='root')
        unique_id = System.get_my_machine_id(client)

        storagerouter = None
        for current_storagerouter in StorageRouterList.get_storagerouters():
            if current_storagerouter.ip == ip and current_storagerouter.machine_id == unique_id:
                storagerouter = current_storagerouter
                break
        if storagerouter is None:
            raise RuntimeError('Could not find Storage Router with given ip address')

        vpool = VPoolList.get_vpool_by_name(vpool_name)
        storagedriver = None
        if vpool is not None:
            if vpool.backend_type.code == 'local':
                # Might be an issue, investigating whether it's on the same Storage Router or not
                if len(vpool.storagedrivers) == 1 and vpool.storagedrivers[0].storagerouter.machine_id != unique_id:
                    raise RuntimeError('A local vPool with name {0} already exists'.format(vpool_name))
            for vpool_storagedriver in vpool.storagedrivers:
                if vpool_storagedriver.storagerouter_guid == storagerouter.guid:
                    storagedriver = vpool_storagedriver  # The vPool is already added to this Storage Router and this might be a cleanup/recovery

            # Check whether there are running machines on this vPool
            machine_guids = []
            for vdisk in vpool.vdisks:
                if vdisk.vmachine_guid is None:
                    continue
                if vdisk.vmachine_guid not in machine_guids:
                    machine_guids.append(vdisk.vmachine_guid)
                    if vdisk.vmachine.hypervisor_status in ['RUNNING', 'PAUSED']:
                        raise RuntimeError('At least one vMachine using this vPool is still running or paused. Make sure there are no active vMachines')

        all_storagerouters = [storagerouter]
        if vpool is not None:
            all_storagerouters += [sd.storagerouter for sd in vpool.storagedrivers]
        voldrv_service = 'volumedriver_{0}'.format(vpool_name)

        # Stop services
        ip_client_map = {}
        for sr in all_storagerouters:
            sr_client = SSHClient(sr.ip, username='root')
            if PluginService.has_service(voldrv_service, client=sr_client):
                PluginService.disable_service(voldrv_service, client=sr_client)
                PluginService.stop_service(voldrv_service, client=sr_client)
            ip_client_map[sr.ip] = {'root': sr_client,
                                    'ovs': SSHClient(sr.ip, username='ovs')}

        # Keep in mind that if the Storage Driver exists, the vPool does as well
        if vpool is None:
            Toolbox.verify_required_params(required_params_wihout_vpool, parameters)
            vpool = VPool()
            supported_backends = client.config_read('ovs.storagedriver.backends').split(',')
            if 'rest' in supported_backends:
                supported_backends.remove('rest')  # REST is not supported for now
            backend_type = BackendTypeList.get_backend_type_by_code(parameters['type'])
            vpool.backend_type = backend_type
            connection_host = parameters['connection_host']
            connection_port = parameters['connection_port']
            connection_username = parameters['connection_username']
            connection_password = parameters['connection_password']
            if vpool.backend_type.code in ['local', 'distributed']:
                metadata = {'backend_type': 'LOCAL',
                            'local_connection_path': mountpoint_bfs}
            elif vpool.backend_type.code == 'alba':
                if connection_host == '':
                    connection_host = Configuration.get('ovs.grid.ip')
                    connection_port = 443
                    oauth_client = ClientList.get_by_types('INTERNAL', 'CLIENT_CREDENTIALS')[0]
                    ovs_client = OVSClient(connection_host, connection_port,
                                           credentials=(oauth_client.client_id, oauth_client.client_secret),
                                           version=1)
                else:
                    ovs_client = OVSClient(connection_host, connection_port,
                                           credentials=(connection_username, connection_password),
                                           version=1)
                task_id = ovs_client.get('/alba/backends/{0}/get_config_metadata'.format(parameters['connection_backend']))
                successful, metadata = ovs_client.wait_for_task(task_id, timeout=300)
                if successful is False:
                    raise RuntimeError('Could not load metadata from remote environment {0}'.format(connection_host))
            elif vpool.backend_type.code in ['ceph_s3', 'amazon_s3', 'swift_s3']:
                if vpool.backend_type.code in ['swift_s3']:
                    strict_consistency = 'false'
                    s3_connection_flavour = 'SWIFT'
                else:
                    strict_consistency = 'true'
                    s3_connection_flavour = 'S3'

                metadata = {'s3_connection_host': connection_host,
                            's3_connection_port': connection_port,
                            's3_connection_username': connection_username,
                            's3_connection_password': connection_password,
                            's3_connection_flavour': s3_connection_flavour,
                            's3_connection_strict_consistency': strict_consistency,
                            's3_connection_verbose_logging': 1,
                            'backend_type': 'S3'}
            else:
                raise ValueError('Unsupported backend type specified: "{0}"'.format(vpool.backend_type.code))

            vpool.name = vpool_name
            vpool.login = connection_username
            vpool.password = connection_password
            vpool.metadata = metadata
            vpool.connection = '{0}:{1}'.format(connection_host, connection_port) if connection_host else None
            vpool.description = "{0} {1}".format(vpool.backend_type.code, vpool_name)
            vpool.save()

        if len(mountpoint_readcaches) == 0:
            raise RuntimeError('No read cache mountpoints specified')
        if len(mountpoint_writecaches) == 0:
            raise RuntimeError('No write cache mountpoints specified')

        mountpoint_fcache = mountpoint_writecaches[0]
        mountpoint_fragmentcache = mountpoint_readcaches[0] if vpool.backend_type.code == 'alba' else ''

        # Check inodes and count the usages (to divide available space later on)
        all_locations = set()
        all_mountpoints = [mountpoint_bfs, mountpoint_temp, mountpoint_md, mountpoint_foc, mountpoint_fragmentcache, mountpoint_fcache] + mountpoint_readcaches + mountpoint_writecaches
        for mountpoint in all_mountpoints[:]:
            if not mountpoint:  # Eg: when bfs mountpoint is not used, the value is ''
                all_mountpoints.remove(mountpoint)
                continue
            all_locations.add(mountpoint)

        root_client.dir_create(all_locations)

        root_inode = os.stat('/').st_dev
        inode_count = {}
        mountpoint_inode_mapping = {}
        for mountpoint in all_mountpoints:
            inode = os.stat(mountpoint).st_dev
            if inode not in inode_count:
                inode_count[inode] = 0
            inode_count[inode] += 1
            mountpoint_inode_mapping[mountpoint] = inode

        if vpool.backend_type.code in ['local', 'distributed']:
            root_client.dir_chmod(parameters['mountpoint_bfs'], 0777)

        fdcache = '{0}/fd_{1}'.format(mountpoint_foc, vpool_name)
        failovercache = '{0}/foc_{1}'.format(mountpoint_foc, vpool_name)
        metadatapath = '{0}/metadata_{1}'.format(mountpoint_md, vpool_name)
        tlogpath = '{0}/tlogs_{1}'.format(mountpoint_md, vpool_name)
        rsppath = '/var/rsp/{0}'.format(vpool_name)

        dirs2create = [failovercache, metadatapath, tlogpath, rsppath,
                       client.config_read('ovs.storagedriver.readcache.serialization.path')]
        files2create = list()
        readcaches = list()
        writecaches = list()
        readcache_size = 0
        frag_size = None

        # Create same inode count mapping, but for mountpoints now
        mountpoint_count_mapping = {}
        for mountpoint in set(all_mountpoints):
            inode = os.stat(mountpoint).st_dev
            mountpoint_count_mapping[mountpoint] = inode_count[inode]

        # Calculate available space for read-, write- and fragmentcache
        for mountpoint, count in mountpoint_count_mapping.iteritems():
            if mountpoint_inode_mapping[mountpoint] == root_inode:
                # Divide by 2 because we don't want to allow root running full, so we only take 50% of available space
                available_size = os.statvfs(mountpoint).f_bavail * os.statvfs(mountpoint).f_bsize / count / 2
            else:
                available_size = os.statvfs(mountpoint).f_bavail * os.statvfs(mountpoint).f_bsize / count

            if mountpoint in mountpoint_readcaches:
                if mountpoint == mountpoint_fragmentcache and vpool.backend_type.code == 'alba':
                    # Multiply by 2 again because we don't want to divide available space evenly between fragment cache and readcache
                    r_size = int(available_size * 2 * 0.88 / 1024 / 4096) * 4096  # KiB
                    frag_size = int(available_size * 2 * .10)  # Bytes
                else:
                    r_size = int(available_size * 0.98 / 1024 / 4096) * 4096
                readcache_size += r_size
                readcaches.append({'path': '{0}/read_{1}'.format(mountpoint, vpool_name),
                                   'size': '{0}KiB'.format(r_size)})
                files2create.append('{0}/read_{1}'.format(mountpoint, vpool_name))
            elif mountpoint in mountpoint_writecaches:
                w_size = int(available_size * .98 / 1024 / 4096) * 4096
                dir2create = '{0}/sco_{1}'.format(mountpoint, vpool_name)
                writecaches.append({'path': dir2create,
                                    'size': '{0}KiB'.format(w_size)})
                dirs2create.append(dir2create)

        if vpool.backend_type.code == 'alba' and frag_size is None:
            raise ValueError('Something went wrong trying to calculate the fragment cache size')

        logger.info('readcaches: {0}'.format(readcaches))
        logger.info('writecaches: {0}'.format(writecaches))
        logger.info('mountpoint_temp: {0}'.format(mountpoint_temp))
        logger.info('mountpoint_md: {0}'.format(mountpoint_md))
        logger.info('mountpoint_foc: {0}'.format(mountpoint_foc))
        logger.info('mountpoint_fragmentcache: {0}'.format(mountpoint_fragmentcache))
        logger.info('mountpoint_fcache: {0}'.format(mountpoint_fcache))
        logger.info('moutpoint_readcaches: {0}'.format(mountpoint_readcaches))
        logger.info('moutpoint_writecaches: {0}'.format(mountpoint_writecaches))
        logger.info('all_locations: {0}'.format(mountpoint_count_mapping.keys()))

        model_ports_in_use = []
        for port_storagedriver in StorageDriverList.get_storagedrivers():
            if port_storagedriver.storagerouter_guid == storagerouter.guid:
                # Local storagedrivers
                model_ports_in_use += port_storagedriver.ports
                if port_storagedriver.alba_proxy is not None:
                    model_ports_in_use.append(port_storagedriver.alba_proxy.service.ports[0])

        # Connection information is Storage Driver related information
        new_storagedriver = False
        if storagedriver is None:
            ports = StorageRouterController._get_free_ports(client, model_ports_in_use, 3)
            storagedriver = StorageDriver()
            new_storagedriver = True
        else:
            ports = storagedriver.ports
        model_ports_in_use += ports

        cmd = "ip a | grep 'inet ' | sed 's/\s\s*/ /g' | cut -d ' ' -f 3 | cut -d '/' -f 1"
        ipaddresses = client.run(cmd).strip().splitlines()
        ipaddresses = [ipaddr.strip() for ipaddr in ipaddresses]
        grid_ip = client.config_read('ovs.grid.ip')
        if grid_ip in ipaddresses:
            ipaddresses.remove(grid_ip)
        if not ipaddresses:
            raise RuntimeError('No available ip addresses found suitable for Storage Router storage ip')
        if storagerouter.pmachine.hvtype == 'KVM':
            volumedriver_storageip = '127.0.0.1'
        else:
            volumedriver_storageip = storage_ip
        vrouter_id = '{0}{1}'.format(vpool_name, unique_id)

        vrouter_config = {'vrouter_id': vrouter_id,
                          'vrouter_redirect_timeout_ms': '5000',
                          'vrouter_routing_retries': 10,
                          'vrouter_volume_read_threshold': 1024,
                          'vrouter_volume_write_threshold': 1024,
                          'vrouter_file_read_threshold': 1024,
                          'vrouter_file_write_threshold': 1024,
                          'vrouter_min_workers': 4,
                          'vrouter_max_workers': 16}
        voldrv_arakoon_cluster_id = str(client.config_read('ovs.storagedriver.db.arakoon.clusterid'))
        voldrv_arakoon_cluster = ArakoonManagementEx().getCluster(voldrv_arakoon_cluster_id)
        voldrv_arakoon_client_config = voldrv_arakoon_cluster.getClientConfig()
        arakoon_nodes = []
        for node_id, node_config in voldrv_arakoon_client_config.iteritems():
            arakoon_nodes.append({'node_id': node_id, 'host': node_config[0][0], 'port': node_config[1]})
        arakoon_node_configs = []
        for arakoon_node in voldrv_arakoon_client_config.keys():
            arakoon_node_configs.append(ArakoonNodeConfig(arakoon_node,
                                                          voldrv_arakoon_client_config[arakoon_node][0][0],
                                                          voldrv_arakoon_client_config[arakoon_node][1]))
        vrouter_clusterregistry = ClusterRegistry(str(vpool.guid), voldrv_arakoon_cluster_id, arakoon_node_configs)
        node_configs = []
        for existing_storagedriver in StorageDriverList.get_storagedrivers():
            if existing_storagedriver.vpool_guid == vpool.guid:
                node_configs.append(ClusterNodeConfig(str(existing_storagedriver.storagedriver_id),
                                                      str(existing_storagedriver.cluster_ip),
                                                      existing_storagedriver.ports[0],
                                                      existing_storagedriver.ports[1],
                                                      existing_storagedriver.ports[2]))
        if new_storagedriver:
            node_configs.append(ClusterNodeConfig(vrouter_id, grid_ip, ports[0], ports[1], ports[2]))
        vrouter_clusterregistry.set_node_configs(node_configs)

        filesystem_config = StorageDriverConfiguration.build_filesystem_by_hypervisor(storagerouter.pmachine.hvtype)
        filesystem_config.update({'fs_metadata_backend_arakoon_cluster_nodes': [],
                                  'fs_metadata_backend_mds_nodes': [],
                                  'fs_metadata_backend_type': 'MDS'})
        readcache_serialization_path = client.config_read('ovs.storagedriver.readcache.serialization.path')
        queue_protocol = Configuration.get('ovs.core.broker.protocol')
        queue_login = Configuration.get('ovs.core.broker.login')
        queue_password = Configuration.get('ovs.core.broker.password')
        queue_volumerouterqueue = Configuration.get('ovs.core.broker.volumerouter.queue')
        queue_urls = []
        for current_storagerouter in StorageRouterList.get_masters():
            queue_urls.append({'amqp_uri': '{0}://{1}:{2}@{3}'.format(queue_protocol,
                                                                      queue_login,
                                                                      queue_password,
                                                                      current_storagerouter.ip)})

        alba_proxy = storagedriver.alba_proxy
        if alba_proxy is None and vpool.backend_type.code == 'alba':
            service = DalService()
            service.storagerouter = storagerouter
            service.ports = [StorageRouterController._get_free_ports(client, model_ports_in_use, 1)]
            service.name = 'albaproxy_{0}'.format(vpool_name)
            service.type = ServiceTypeList.get_by_name('AlbaProxy')
            service.save()
            alba_proxy = AlbaProxy()
            alba_proxy.service = service
            alba_proxy.storagedriver = storagedriver
            alba_proxy.save()
            config = RawConfigParser()
            for section in vpool.metadata:
                config.add_section(section)
                for key, value in vpool.metadata[section].iteritems():
                    config.set(section, key, value)
            config_dir = '{0}/storagedriver/storagedriver'.format(client.config_read('ovs.core.cfgdir'))
            client.dir_create(config_dir)
            cache_dir = '{}/fcache_{}'.format(mountpoint_fragmentcache, vpool_name)
            root_client.dir_create(cache_dir)
            System.write_config(config, '{0}/{1}_alba.cfg'.format(config_dir, vpool_name), client)

            # manifest cache is in memory
            client.file_write('{0}/{1}_alba.json'.format(config_dir, vpool_name), json.dumps({
                'log_level': 'debug',
                'port': alba_proxy.service.ports[0],
                'ips': ['127.0.0.1'],
                'manifest_cache_size': 100000,
                'fragment_cache_dir': cache_dir,
                'fragment_cache_size': frag_size,
                'albamgr_cfg_file': '{0}/{1}_alba.cfg'.format(config_dir, vpool_name)
            }))

        storagedriver_config = StorageDriverConfiguration('storagedriver', vpool_name)
        storagedriver_config.load(client)
        storagedriver_config.clean()  # Clean out obsolete values
        if vpool.backend_type.code == 'alba':
            storagedriver_config.configure_backend_connection_manager(alba_connection_host='127.0.0.1',
                                                                      alba_connection_port=alba_proxy.service.ports[0],
                                                                      backend_type='ALBA')
        else:
            storagedriver_config.configure_backend_connection_manager(**vpool.metadata)
        storagedriver_config.configure_content_addressed_cache(clustercache_mount_points=readcaches,
                                                               read_cache_serialization_path=readcache_serialization_path)
        storagedriver_config.configure_scocache(scocache_mount_points=writecaches,
                                                trigger_gap='1GB',
                                                backoff_gap='2GB')
        storagedriver_config.configure_failovercache(failovercache_path=failovercache)
        storagedriver_config.configure_filesystem(**filesystem_config)
        storagedriver_config.configure_volume_manager(clean_interval=1,
                                                      metadata_path=metadatapath,
                                                      tlog_path=tlogpath,
                                                      foc_throttle_usecs=4000,
                                                      read_cache_default_behaviour='CacheOnWrite',
                                                      non_disposable_scos_factor=12)
        storagedriver_config.configure_volume_router(**vrouter_config)
        storagedriver_config.configure_volume_router_cluster(vrouter_cluster_id=vpool.guid)
        storagedriver_config.configure_volume_registry(vregistry_arakoon_cluster_id=voldrv_arakoon_cluster_id,
                                                       vregistry_arakoon_cluster_nodes=arakoon_nodes)
        storagedriver_config.configure_file_driver(fd_cache_path=fdcache,
                                                   fd_extent_cache_capacity='1024',
                                                   fd_namespace='fd-{0}-{1}'.format(vpool_name, vpool.guid))
        storagedriver_config.configure_event_publisher(events_amqp_routing_key=queue_volumerouterqueue,
                                                       events_amqp_uris=queue_urls)
        storagedriver_config.save(client, reload_config=False)

        # Updating the model
        storagedriver.name = vrouter_id.replace('_', ' ')
        storagedriver.ports = ports
        storagedriver.vpool = vpool
        storagedriver.cluster_ip = grid_ip
        storagedriver.storage_ip = volumedriver_storageip
        storagedriver.mountpoint = '/mnt/{0}'.format(vpool_name)
        storagedriver.description = storagedriver.name
        storagedriver.storagerouter = storagerouter
        storagedriver.storagedriver_id = vrouter_id
        storagedriver.mountpoint_md = mountpoint_md
        storagedriver.mountpoint_foc = mountpoint_foc
        storagedriver.mountpoint_bfs = mountpoint_bfs
        storagedriver.mountpoint_temp = mountpoint_temp
        storagedriver.mountpoint_readcaches = mountpoint_readcaches
        storagedriver.mountpoint_writecaches = mountpoint_writecaches
        storagedriver.mountpoint_fragmentcache = mountpoint_fragmentcache
        storagedriver.save()

        MDSServiceController.prepare_mds_service(client, storagerouter, vpool, reload_config=False)

        mds_config_set = MDSServiceController.get_mds_storagedriver_config_set(vpool)
        for sr in all_storagerouters:
            node_client = ip_client_map[sr.ip]['ovs']
            storagedriver_config = StorageDriverConfiguration('storagedriver', vpool_name)
            storagedriver_config.load(node_client)
            if storagedriver_config.is_new is False:
                storagedriver_config.clean()  # Clean out obsolete values
                storagedriver_config.configure_filesystem(fs_metadata_backend_mds_nodes=mds_config_set[sr.guid])
                storagedriver_config.save(node_client, reload_config=False)

        dirs2create.append(storagedriver.mountpoint)
        dirs2create.append(fdcache)

        root_client.dir_create(dirs2create)
        root_client.file_create(files2create)

        params = {'<VPOOL_MOUNTPOINT>': storagedriver.mountpoint,
                  '<HYPERVISOR_TYPE>': storagerouter.pmachine.hvtype,
                  '<VPOOL_NAME>': vpool_name,
                  '<UUID>': str(uuid.uuid4()),
                  '<OVS_UID>': check_output('id -u ovs', shell=True).strip(),
                  '<OVS_GID>': check_output('id -g ovs', shell=True).strip(),
                  '<KILL_TIMEOUT>': str(int(readcache_size / 1024.0 / 1024.0 / 6.0 + 30))}

        template_dir = '/opt/OpenvStorage/config/templates/upstart'
        template_configs = {'ovs-volumedriver.conf': 'ovs-volumedriver_{0}.conf'.format(vpool.name),
                            'ovs-failovercache.conf': 'ovs-failovercache_{0}.conf'.format(vpool.name)}
        if vpool.backend_type.code == 'alba':
            template_configs['ovs-albaproxy.conf'] = 'ovs-albaproxy_{0}.conf'.format(vpool.name)
        for template_file, vpool_file in template_configs.iteritems():
            if client.file_exists('{0}/{1}'.format(template_dir, template_file)):
                client.run('cp -f {0}/{1} {0}/{2}'.format(template_dir, template_file, vpool_file))

        PluginService.add_service(name='volumedriver_{0}'.format(vpool_name), params=params, client=root_client)
        PluginService.add_service(name='failovercache_{0}'.format(vpool_name), params=params, client=root_client)
        if vpool.backend_type.code == 'alba':
            PluginService.add_service(name='albaproxy_{0}'.format(vpool_name), params=params, client=root_client)

        # Remove copied template config files (obsolete after add service)
        client.file_delete('{0}/ovs-failovercache_{1}.conf'.format(template_dir, vpool.name))
        client.file_delete('{0}/ovs-volumedriver_{1}.conf'.format(template_dir, vpool.name))
        if vpool.backend_type.code == 'alba':
            client.file_delete('{0}/ovs-albaproxy_{1}.conf'.format(template_dir, vpool.name))

        if storagerouter.pmachine.hvtype == 'VMWARE':
            root_client.run("grep -q '/tmp localhost(ro,no_subtree_check)' /etc/exports || echo '/tmp localhost(ro,no_subtree_check)' >> /etc/exports")
            root_client.run('service nfs-kernel-server start')

        if storagerouter.pmachine.hvtype == 'KVM':
            root_client.run('virsh pool-define-as {0} dir - - - - {1}'.format(vpool_name, storagedriver.mountpoint))
            root_client.run('virsh pool-build {0}'.format(vpool_name))
            root_client.run('virsh pool-start {0}'.format(vpool_name))
            root_client.run('virsh pool-autostart {0}'.format(vpool_name))

        # Start services
        for sr in all_storagerouters:
            sr_client = ip_client_map.get(sr.ip, {'root': SSHClient(ip, username='root')})['root']
            PluginService.enable_service(voldrv_service, client=sr_client)
            PluginService.start_service(voldrv_service, client=sr_client)

        # Fill vPool size
        vfs_info = os.statvfs('/mnt/{0}'.format(vpool_name))
        vpool.size = vfs_info.f_blocks * vfs_info.f_bsize
        vpool.save()

        for vdisk in vpool.vdisks:
            MDSServiceController.ensure_safety(vdisk)

        mgmt_center = Factory.get_mgmtcenter(storagerouter.pmachine)
        if mgmt_center:
            # Store specific metadata in mgmtcenter object
            metadata = mgmt_center.get_metadata(storagerouter.pmachine.mgmtcenter.metadata, parameters)
            storagerouter.pmachine.mgmtcenter.metadata.update(metadata)
            storagerouter.pmachine.mgmtcenter.save()
            mgmt_center.set_metadata(metadata)
            # Configure vpool on management center
            mgmt_center.configure_vpool(vpool_name, storagedriver.mountpoint)
        else:
            logger.info('Storagerouter {0} does not have management center'.format(storagerouter.name))

    @staticmethod
    @celery.task(name='ovs.storagerouter.remove_storagedriver')
    def remove_storagedriver(storagedriver_guid):
        """
        Removes a StorageDriver (and, if it was the last Storage Driver for a vPool, the vPool is removed as well)
        """
        logger.info('Deleting storage driver with guid {0}'.format(storagedriver_guid))

        # Get objects & Make some checks
        storagedriver = StorageDriver(storagedriver_guid)
        storagerouter = storagedriver.storagerouter
        ip = storagerouter.ip
        pmachine = storagerouter.pmachine
        vmachines = VMachineList.get_customer_vmachines()
        pmachine_guids = [vm.pmachine_guid for vm in vmachines]
        vpool_guids = [vm.vpool_guid for vm in vmachines if vm.vpool_guid is not None]

        vpool = storagedriver.vpool
        if pmachine.guid in pmachine_guids and vpool.guid in vpool_guids:
            raise RuntimeError('There are still vMachines served from the given Storage Driver')
        if any(vdisk for vdisk in vpool.vdisks if vdisk.storagedriver_id == storagedriver.storagedriver_id):
            raise RuntimeError('There are still vDisks served from the given Storage Driver')

        voldrv_service = 'volumedriver_{0}'.format(vpool.name)
        foc_service = 'failovercache_{0}'.format(vpool.name)
        albaproxy_service = 'albaproxy_{0}'.format(vpool.name)
        storagedrivers_left = False
        removal_mdsservices = [mds_service for mds_service in vpool.mds_services
                               if mds_service.service.storagerouter_guid == storagerouter.guid]

        # Unconfigure or reconfigure the MDSses
        vdisks = []
        for mds in removal_mdsservices:
            for junction in mds.vdisks:
                vdisks.append(junction.vdisk)
        for vdisk in vdisks:
            MDSServiceController.ensure_safety(vdisk, [storagerouter])

        # Stop services
        ip_client_map = {}
        for current_storagedriver in vpool.storagedrivers:
            if current_storagedriver.guid != storagedriver_guid:
                storagedrivers_left = True
            client = SSHClient(current_storagedriver.storagerouter.ip, username='root')
            if client.ip not in ip_client_map:
                ip_client_map[client.ip] = client

            if PluginService.has_service(voldrv_service, client=client):
                PluginService.disable_service(voldrv_service, client=client)
                PluginService.stop_service(voldrv_service, client=client)

        # Unconfigure vpool on management
        logger.debug('Unconfigure vPool from MgmtCenter')
        with Remote(ip, [System, Factory]) as remote:
            mgmtcenter = remote.Factory.get_mgmtcenter(remote.System.get_my_storagerouter().pmachine)
            if mgmtcenter:
                mgmtcenter.unconfigure_vpool(vpool.name, storagedriver.mountpoint, not storagedrivers_left)

        # KVM pool
        if pmachine.hvtype == 'KVM':
            # 'Name                 State      Autostart '
            # '-------------------------------------------'
            # ' vpool1               active     yes'
            # ' vpool2               active     no'
            client = SSHClient(ip)
            vpool_overview = client.run('virsh pool-list --all').splitlines()
            vpool_overview.pop(1)  # Pop   ---------------
            vpool_overview.pop(0)  # Pop   Name   State   Autostart
            for vpool_info in vpool_overview:
                vpool_name = vpool_info.split()[0].strip()
                if vpool.name == vpool_name:
                    client.run('virsh pool-destroy {0}'.format(vpool.name))
                    try:
                        client.run('virsh pool-undefine {0}'.format(vpool.name))
                    except:
                        pass  # Ignore undefine errors, since that can happen on re-entrance
                    break

        # Remove services
        services_to_remove = [voldrv_service, foc_service]
        if storagedriver.alba_proxy is not None:
            services_to_remove.append(albaproxy_service)
        for service in services_to_remove:
            if PluginService.has_service(service, client=client):
                PluginService.remove_service(name=service, client=client)

        configuration_dir = client.config_read('ovs.core.cfgdir')

        voldrv_arakoon_cluster_id = str(client.config_read('ovs.storagedriver.db.arakoon.clusterid'))
        voldrv_arakoon_cluster = ArakoonManagementEx().getCluster(voldrv_arakoon_cluster_id)
        voldrv_arakoon_client_config = voldrv_arakoon_cluster.getClientConfig()
        arakoon_node_configs = []
        for arakoon_node in voldrv_arakoon_client_config.keys():
            arakoon_node_configs.append(ArakoonNodeConfig(arakoon_node,
                                                          voldrv_arakoon_client_config[arakoon_node][0][0],
                                                          voldrv_arakoon_client_config[arakoon_node][1]))
        vrouter_clusterregistry = ClusterRegistry(str(vpool.guid), voldrv_arakoon_cluster_id, arakoon_node_configs)

        # Reconfigure volumedriver
        if storagedrivers_left is True:
            node_configs = []
            for current_storagedriver in vpool.storagedrivers:
                if current_storagedriver.guid != storagedriver_guid:
                    node_configs.append(ClusterNodeConfig(str(current_storagedriver.storagedriver_id),
                                                          str(current_storagedriver.cluster_ip),
                                                          current_storagedriver.ports[0],
                                                          current_storagedriver.ports[1],
                                                          current_storagedriver.ports[2]))
            vrouter_clusterregistry.set_node_configs(node_configs)
        else:
            try:
                storagedriver_client = LocalStorageRouterClient('{0}/storagedriver/storagedriver/{1}.json'.format(configuration_dir, vpool.name))
                storagedriver_client.destroy_filesystem()
                vrouter_clusterregistry.erase_node_configs()
            except RuntimeError as ex:
                print('Could not destroy filesystem or erase node configs due to error: {0}'.format(ex))

        for mds_service in removal_mdsservices:
            # All MDSServiceVDisk object should have been deleted above
            MDSServiceController.remove_mds_service(mds_service, client, storagerouter, vpool, reload_config=False)

        # Cleanup directories/files
        for readcache in storagedriver.mountpoint_readcaches:
            file_name = '{0}/read_{1}'.format(readcache, vpool.name)
            client.file_delete(file_name)
            logger.info('Removed file {0}'.format(file_name))

        for writecache in storagedriver.mountpoint_writecaches:
            dir_name = '{0}/sco_{1}'.format(writecache, vpool.name)
            client.dir_delete(dir_name)
            logger.info('Recursively removed {0}'.format(dir_name))

        client.dir_delete('{0}/foc_{1}'.format(storagedriver.mountpoint_foc, vpool.name))
        client.dir_delete('{0}/fd_{1}'.format(storagedriver.mountpoint_foc, vpool.name))
        client.dir_delete('{0}/fcache_{1}'.format(storagedriver.mountpoint_fragmentcache, vpool.name))
        client.dir_delete('{0}/metadata_{1}'.format(storagedriver.mountpoint_md, vpool.name))
        client.dir_delete('{0}/tlogs_{1}'.format(storagedriver.mountpoint_md, vpool.name))
        client.dir_delete('/var/rsp/{0}'.format(vpool.name))
        client.file_delete('{0}/storagedriver/storagedriver/{1}.json'.format(configuration_dir, vpool.name))
        if vpool.backend_type.code == 'alba':
            client.file_delete('{0}/storagedriver/storagedriver/{1}_alba.cfg'.format(configuration_dir, vpool.name))
            client.file_delete('{0}/storagedriver/storagedriver/{1}_alba.json'.format(configuration_dir, vpool.name))

        # Remove top directories
        dirs2remove = list()
        dirs2remove.extend(storagedriver.mountpoint_readcaches)
        dirs2remove.extend(storagedriver.mountpoint_writecaches)
        dirs2remove.append(storagedriver.mountpoint_fragmentcache)
        dirs2remove.append(storagedriver.mountpoint_foc)
        dirs2remove.append(storagedriver.mountpoint_md)
        dirs2remove.append(storagedriver.mountpoint)

        for directory in set(dirs2remove):
            if directory:
                client.run('if [ -d {0} ] && [ ! "$(ls -A {0})" ]; then rmdir {0}; fi'.format(directory))

        # First model cleanup
        if storagedriver.alba_proxy is not None:
            storagedriver.alba_proxy.delete()
        storagedriver.delete(abandon=['logs'])  # Detach from the log entries

        MDSServiceController.mds_checkup()

        if storagedrivers_left:
            # Restart leftover services
            for current_storagedriver in vpool.storagedrivers:
                ip = current_storagedriver.storagerouter.ip
                sr_client = ip_client_map.get(ip, SSHClient(ip))
                if current_storagedriver.guid != storagedriver_guid:
                    if PluginService.has_service(voldrv_service, client=sr_client):
                        PluginService.enable_service(voldrv_service, client=sr_client)
                        PluginService.start_service(voldrv_service, client=sr_client)
        else:
            # Final model cleanup
            vpool.delete()

    @staticmethod
    @celery.task(name='ovs.storagerouter.update_storagedrivers')
    def update_storagedrivers(storagedriver_guids, storagerouters, parameters):
        """
        Add/remove multiple vPools
        @param storagedriver_guids: Storage Drivers to be removed
        @param storagerouters: StorageRouters on which to add a new link
        @param parameters: Settings for new links
        """
        print 'update storagedrivers: {0}'.format(str(parameters))
        success = True
        # Add Storage Drivers
        for storagerouter_ip, storageappliance_machineid in storagerouters:
            try:
                new_parameters = copy.copy(parameters)
                new_parameters['storagerouter_ip'] = storagerouter_ip
                local_machineid = System.get_my_machine_id()
                if local_machineid == storageappliance_machineid:
                    # Inline execution, since it's on the same node (preventing deadlocks)
                    StorageRouterController.add_vpool(new_parameters)
                else:
                    # Async execution, since it has to be executed on another node
                    # @TODO: Will break in Celery 3.2, need to find another solution
                    # Requirements:
                    # - This code cannot continue until this new task is completed (as all these Storage Router
                    #   need to be handled sequentially
                    # - The wait() or get() method are not allowed anymore from within a task to prevent deadlocks
                    result = StorageRouterController.add_vpool.s(new_parameters).apply_async(
                        routing_key='sr.{0}'.format(storageappliance_machineid)
                    )
                    result.wait()
            except:
                success = False
        # Remove Storage Drivers
        for storagedriver_guid in storagedriver_guids:
            try:
                storagedriver = StorageDriver(storagedriver_guid)
                storagerouter_machineid = storagedriver.storagerouter.machine_id
                local_machineid = System.get_my_machine_id()
                if local_machineid == storagerouter_machineid:
                    # Inline execution, since it's on the same node (preventing deadlocks)
                    StorageRouterController.remove_storagedriver(storagedriver_guid)
                else:
                    # Async execution, since it has to be executed on another node
                    # @TODO: Will break in Celery 3.2, need to find another solution
                    # Requirements:
                    # - This code cannot continue until this new task is completed (as all these VSAs need to be
                    # handled sequentially
                    # - The wait() or get() method are not allowed anymore from within a task to prevent deadlocks
                    result = StorageRouterController.remove_storagedriver.s(storagedriver_guid).apply_async(
                        routing_key='sr.{0}'.format(storagerouter_machineid)
                    )
                    result.wait()
            except:
                success = False
        return success

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_version_info')
    def get_version_info(storagerouter_guid):
        """
        Returns version information regarding a given StorageRouter
        """
        return {'storagerouter_guid': storagerouter_guid,
                'versions': Package.get_versions()}

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_support_info')
    def get_support_info(storagerouter_guid):
        """
        Returns support information regarding a given StorageRouter
        """
        return {'storagerouter_guid': storagerouter_guid,
                'nodeid': Configuration.get('ovs.support.nid'),
                'clusterid': Configuration.get('ovs.support.cid'),
                'enabled': int(Configuration.get('ovs.support.enabled')) > 0,
                'enablesupport': int(Configuration.get('ovs.support.enablesupport')) > 0}

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_support_metadata')
    def get_support_metadata():
        """
        Returns support metadata for a given storagerouter. This should be a routed task!
        """
        return SupportAgent.get_heartbeat_data()

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_logfiles')
    def get_logfiles(local_storagerouter_guid):
        """
        Collects logs, moves them to a web-accessible location and returns log tgz's filename
        """
        storagerouter = StorageRouter(local_storagerouter_guid)
        webpath = '/opt/OpenvStorage/webapps/frontend/downloads'
        logfile = check_output('ovs collect logs', shell=True).strip()
        logfilename = logfile.split('/')[-1]
        client = SSHClient(storagerouter.ip)
        client.dir_create(webpath)
        client.file_upload('{0}/{1}'.format(webpath, logfilename), logfile)
        client.run('chmod 666 {0}/{1}'.format(webpath, logfilename))
        return logfilename

    @staticmethod
    @celery.task(name='ovs.storagerouter.configure_support')
    def configure_support(enable, enable_support):
        """
        Configures support on all StorageRouters
        """
        for storagerouter in StorageRouterList.get_storagerouters():
            client = SSHClient(storagerouter.ip, username='root')
            client.config_set('ovs.support.enabled', 1 if enable else 0)
            client.config_set('ovs.support.enablesupport', 1 if enable_support else 0)
            if enable_support is False:
                client.run('service openvpn stop')
                client.file_delete('/etc/openvpn/ovs_*')
            if enable is True:
                if not PluginService.has_service(StorageRouterController.SUPPORT_AGENT, client=client):
                    PluginService.add_service(StorageRouterController.SUPPORT_AGENT, client=client)
                    PluginService.enable_service(StorageRouterController.SUPPORT_AGENT, client=client)
                if not PluginService.get_service_status(StorageRouterController.SUPPORT_AGENT, client=client):
                    PluginService.start_service(StorageRouterController.SUPPORT_AGENT, client=client)
                else:
                    PluginService.restart_service(StorageRouterController.SUPPORT_AGENT, client=client)
            else:
                if PluginService.has_service(StorageRouterController.SUPPORT_AGENT, client=client):
                    if PluginService.get_service_status(StorageRouterController.SUPPORT_AGENT, client=client):
                        PluginService.stop_service(StorageRouterController.SUPPORT_AGENT, client=client)
                    PluginService.remove_service(None, StorageRouterController.SUPPORT_AGENT, client=client)
        return True

    @staticmethod
    @celery.task(name='ovs.storagerouter.check_s3')
    def check_s3(host, port, accesskey, secretkey):
        """
        Validates whether connection to a given S3 backend can be made
        """
        try:
            import boto
            import boto.s3.connection
            backend = boto.connect_s3(aws_access_key_id=accesskey,
                                      aws_secret_access_key=secretkey,
                                      port=port,
                                      host=host,
                                      is_secure=(port == 443),
                                      calling_format=boto.s3.connection.OrdinaryCallingFormat())
            backend.get_all_buckets()
            return True
        except Exception as ex:
            logger.exception('Error during S3 check: {0}'.format(ex))
            return False

    @staticmethod
    @celery.task(name='ovs.storagerouter.check_mtpt')
    def check_mtpt(name):
        """
        Checks whether a given mountpoint for vPool is in use
        """
        mountpoint = '/mnt/{0}'.format(name)
        if not os.path.exists(mountpoint):
            return True
        return check_output('sudo -s ls -al {0} | wc -l'.format(mountpoint), shell=True).strip() == '3'

    @staticmethod
    def _get_free_ports(client, ports_in_use, number):
        """
        Gets `number` free ports ports that are not in use and not reserved
        """
        port_range = client.config_read('ovs.ports.storagedriver')
        ports = System.get_free_ports(port_range, ports_in_use, number, client)

        return ports if number != 1 else ports[0]
