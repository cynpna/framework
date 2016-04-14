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
Module for working with celery tasks
"""

from backend.decorators import load
from backend.decorators import log
from backend.decorators import required_roles
from celery.task.control import inspect
from ovs.celery_run import celery
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import link
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


class TaskViewSet(viewsets.ViewSet):
    """
    Information about celery tasks
    """
    permission_classes = (IsAuthenticated,)
    prefix = r'tasks'
    base_name = 'tasks'

    @log()
    @required_roles(['read'])
    @load()
    def list(self):
        """
        Overview of active, scheduled, reserved and revoked tasks
        """
        inspector = inspect()
        data = {'active': inspector.active(),
                'scheduled': inspector.scheduled(),
                'reserved': inspector.reserved(),
                'revoked': inspector.revoked()}
        return Response(data, status=status.HTTP_200_OK)

    @log()
    @required_roles(['read'])
    @load()
    def retrieve(self, pk):
        """
        Load information about a given task
        :param pk: Primary key
        """
        result = celery.AsyncResult(pk)
        status = result.status
        if result.successful():
            result_data = result.result
        else:
            result_data = str(result.result) if result.result is not None else None
        data = {'id': result.id,
                'status': status,
                'successful': result.successful(),
                'failed': result.failed(),
                'ready': result.ready(),
                'result': result_data}
        return Response(data, status=status.HTTP_200_OK)

    @link()
    @log()
    @required_roles(['read'])
    @load()
    def get(self, pk):
        """
        Gets a given task's result
        :param pk: Primary key
        """
        result = celery.AsyncResult(pk)
        return Response(result.get(), status=status.HTTP_200_OK)
