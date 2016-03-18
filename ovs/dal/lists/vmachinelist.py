# Copyright 2014 iNuron NV
#
# Licensed under the Open vStorage Modified Apache License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.openvstorage.org/license
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
VMachineList module
"""
from ovs.dal.datalist import DataList
from ovs.dal.hybrids.vmachine import VMachine


class VMachineList(object):
    """
    This VMachineList class contains various lists regarding to the VMachine class
    """

    @staticmethod
    def get_vmachines():
        """
        Returns a list of all VMachines
        """
        return DataList(VMachine, {'type': DataList.where_operator.AND,
                                   'items': []})

    @staticmethod
    def get_vmachine_by_name(vmname):
        """
        Returns all VMachines which have a given name
        """
        vmachines = DataList(VMachine, {'type': DataList.where_operator.AND,
                                        'items': [('name', DataList.operator.EQUALS, vmname)]})
        if len(vmachines) > 0:
            return vmachines
        return None

    @staticmethod
    def get_by_devicename_and_vpool(devicename, vpool):
        """
        Returns a list of all vMachines based on a given devicename and vpool
        """
        vpool_guid = None if vpool is None else vpool.guid
        vms = DataList(VMachine, {'type': DataList.where_operator.AND,
                                  'items': [('devicename', DataList.operator.EQUALS, devicename),
                                            ('vpool_guid', DataList.operator.EQUALS, vpool_guid)]})
        if len(vms) > 0:
            if len(vms) != 1:
                raise RuntimeError('Invalid amount of vMachines found: {0}'.format(len(vms)))
            return vms[0]
        return None

    @staticmethod
    def get_customer_vmachines():
        """
        Returns "real" vmachines. No vTemplates
        """
        return DataList(VMachine, {'type': DataList.where_operator.AND,
                                   'items': [('is_vtemplate', DataList.operator.EQUALS, False)]})

    @staticmethod
    def get_vtemplates():
        """
        Returns vTemplates
        """
        return DataList(VMachine, {'type': DataList.where_operator.AND,
                                   'items': [('is_vtemplate', DataList.operator.EQUALS, True)]})
