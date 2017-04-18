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
Celery entry point module
"""
from __future__ import absolute_import

import sys
sys.path.append('/opt/OpenvStorage')

import os
import threading
from celery import Celery
from celery.task.control import inspect
from celery.signals import task_postrun, worker_process_init, after_setup_logger, after_setup_task_logger
from kombu import Queue
from threading import Thread
from ovs.extensions.db.arakoon.configuration import ArakoonConfiguration
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.generic.system import System
from ovs.extensions.generic.volatilemutex import volatile_mutex
from ovs.extensions.storage.exceptions import KeyNotFoundException
from ovs.extensions.storage.persistentfactory import PersistentFactory
from ovs.extensions.storage.volatilefactory import VolatileFactory
from ovs.lib.messaging import MessageController
from ovs.log.log_handler import LogHandler


class CeleryMockup(object):
    """
    Mockup class for Celery
    """
    def task(self, *args, **kwargs):
        """
        Celery task mockup
        Is used as a decorator, so should return a function, which does nothing (for now)
        """
        _ = self, args, kwargs

        def _wrapper(func):
            def _wrapped(*arguments, **kwarguments):
                _ = arguments, kwarguments
                if 'bind' in kwargs:
                    return func(type('Task', (), {'request': type('Request', (), {'id': None})}), *arguments, **kwarguments)
                return func(*arguments, **kwarguments)

            def _delayed(*arguments, **kwarguments):
                def _catch_errors_in_function(*more_args, **more_kwargs):
                    try:
                        return func(*more_args, **more_kwargs)
                    except Exception as ex:
                        async_result['exception'] = ex

                if 'bind' in kwargs:
                    arguments = tuple([type('Task', (), {'request': type('Request', (), {'id': 0})})] + list(arguments))
                thread = Thread(target=_catch_errors_in_function, name='{0}_delayed'.format(threading.current_thread().getName()), args=arguments, kwargs=kwarguments)
                async_result = {'thread': thread,
                                'exception': None}
                thread.start()
                return async_result

            _wrapped.delay = _delayed
            _wrapped.__name__ = func.__name__
            _wrapped.__module__ = func.__module__
            return _wrapped
        return _wrapper


if os.environ.get('RUNNING_UNITTESTS') == 'True':
    celery = CeleryMockup()
else:
    memcache_servers = Configuration.get('/ovs/framework/memcache|endpoints')
    rmq_servers = Configuration.get('/ovs/framework/messagequeue|endpoints')

    unique_id = System.get_my_machine_id()

    include = []
    path = '/'.join([os.path.dirname(__file__), 'lib'])
    for filename in os.listdir(path):
        if os.path.isfile('/'.join([path, filename])) and filename.endswith('.py') and filename != '__init__.py':
            name = filename.replace('.py', '')
            include.append('ovs.lib.{0}'.format(name))

    celery = Celery('ovs', include=include)

    # http://docs.celeryproject.org/en/latest/configuration.html#cache-backend-settings
    celery.conf.CELERY_RESULT_BACKEND = "cache+memcached://{0}/".format(';'.join(memcache_servers))
    celery.conf.BROKER_URL = ';'.join(['{0}://{1}:{2}@{3}//'.format(Configuration.get('/ovs/framework/messagequeue|protocol'),
                                                                    Configuration.get('/ovs/framework/messagequeue|user'),
                                                                    Configuration.get('/ovs/framework/messagequeue|password'),
                                                                    server)
                                       for server in rmq_servers])
    celery.conf.BROKER_CONNECTION_MAX_RETRIES = 5
    celery.conf.BROKER_HEARTBEAT = 10
    celery.conf.BROKER_HEARTBEAT_CHECKRATE = 2
    celery.conf.CELERY_DEFAULT_QUEUE = 'ovs_generic'
    celery.conf.CELERY_QUEUES = tuple([Queue('ovs_generic', routing_key='generic.#'),
                                       Queue('ovs_masters', routing_key='masters.#'),
                                       Queue('ovs_{0}'.format(unique_id), routing_key='sr.{0}.#'.format(unique_id))])
    celery.conf.CELERY_DEFAULT_EXCHANGE = 'generic'
    celery.conf.CELERY_DEFAULT_EXCHANGE_TYPE = 'topic'
    celery.conf.CELERY_DEFAULT_ROUTING_KEY = 'generic.default'
    celery.conf.CELERYD_PREFETCH_MULTIPLIER = 1  # This makes sure that the workers won't be pre-fetching tasks, this to prevent deadlocks
    celery.conf.CELERYBEAT_SCHEDULE = {}
    celery.conf.CELERY_TRACK_STARTED = True  # http://docs.celeryproject.org/en/latest/configuration.html#std:setting-CELERY_TRACK_STARTED
    celery.conf.CELERYD_HIJACK_ROOT_LOGGER = False


@task_postrun.connect
def task_postrun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, **kwds):
    """
    Hook for celery postrun event
    """
    _ = sender, task, args, kwargs, kwds
    try:
        MessageController.fire(MessageController.Type.TASK_COMPLETE, task_id)
    except Exception as ex:
        loghandler = LogHandler.get('celery', name='celery')
        loghandler.error('Caught error during postrun handler: {0}'.format(ex))


@worker_process_init.connect
def worker_process_init_handler(args=None, kwargs=None, **kwds):
    """
    Hook for process init
    """
    _ = args, kwargs, kwds
    VolatileFactory.store = None
    PersistentFactory.store = None
    ArakoonConfiguration.client = None


@after_setup_task_logger.connect
@after_setup_logger.connect
def load_ovs_logger(**kwargs):
    """Load a logger."""
    if 'logger' in kwargs:
        kwargs['logger'] = LogHandler.get('celery', name='celery')


def _clean_cache():
    loghandler = LogHandler.get('celery', name='celery')
    loghandler.info('Executing celery "clear_cache" startup script...')
    from ovs.lib.helpers.decorators import ENSURE_SINGLE_KEY
    active = inspect().active()
    active_tasks = []
    if active is not None:
        for tasks in active.itervalues():
            active_tasks += [task['id'] for task in tasks]
    cache = PersistentFactory.get_client()
    for key in cache.prefix(ENSURE_SINGLE_KEY):
        try:
            with volatile_mutex(name=key, wait=5):
                entry = cache.get(key)
                values = entry.get('values', [])
                new_values = []
                for v in values:
                    task_id = v.get('task_id')
                    if task_id is not None and task_id in active_tasks:
                        new_values.append(v)
                if len(new_values) > 0:
                    entry['values'] = new_values
                    cache.set(key, entry)
                    loghandler.info('Updated key {0}'.format(key))
                else:
                    cache.delete(key)
                    loghandler.info('Deleted key {0}'.format(key))
        except KeyNotFoundException:
            pass
    loghandler.info('Executing celery "clear_cache" startup script... done')


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == 'clear_cache':
        _clean_cache()
