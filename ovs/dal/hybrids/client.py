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
Client module
"""
from ovs.dal.dataobject import DataObject
from ovs.dal.structures import Property, Relation, Dynamic
from ovs.dal.hybrids.user import User


class Client(DataObject):
    """
    The Client class represents a client (application) used by the User. A user might use multiple clients and
    will at least have one default application (frontend GUI)
    """
    __properties = [Property('name', str, mandatory=False, doc='Name of the client'),
                    Property('client_secret', str, mandatory=False, doc='Client secret (application password)'),
                    Property('grant_type', ['PASSWORD', 'CLIENT_CREDENTIALS'], doc='Grant type of the Client'),
                    Property('ovs_type', ['INTERNAL', 'USER'], doc='The type of the client within Open vStorage')]
    __relations = [Relation('user', User, 'clients')]
    __dynamics = [Dynamic('client_id', str, 86400)]

    def _client_id(self):
        """
        The client_id is in fact our model's guid
        """
        return self.guid
