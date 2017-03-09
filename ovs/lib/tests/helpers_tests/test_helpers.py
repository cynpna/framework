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
Helpers test module
"""

import time
import unittest
import threading
from threading import Event, Thread
from ovs.dal.tests.helpers import DalHelper
from ovs.extensions.generic.threadhelpers import Waiter
# noinspection PyProtectedMember
from ovs.lib.helpers.decorators import Decorators, _ensure_single, ovs_task


class Helpers(unittest.TestCase):
    """
    This test class will validate the various scenarios of the Helpers logic
    """
    helpers = {}
    exceptions = []

    def setUp(self):
        """
        (Re)Sets the stores on every test
        """
        self.volatile, self.persistent = DalHelper.setup()

    def tearDown(self):
        """
        Clean up test suite
        """
        DalHelper.teardown()

    @staticmethod
    def _execute_delayed_or_inline(function, delayed, **kwargs):
        if delayed is True:
            return_value = function.delay(**kwargs)
            return_value['thread'].join()
            if return_value['exception'] is not None:
                exceptions.append(return_value['exception'].message)
        else:
            try:
                function(**kwargs)
            except Exception as ex:
                exceptions.append(ex.message)

    @staticmethod
    def _wait_for(condition):
        start = time.time()
        while condition() is False:
            time.sleep(0.01)
            if time.time() > start + 5:
                raise RuntimeError('Waiting for condition timed out')

    def test_ensure_single_decorator_chained(self):
        """
        Tests Helpers._ensure_single functionality in CHAINED mode
        """
        # DECORATED FUNCTIONS
        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'CHAINED', 'extra_task_names': []})
        def _function_w_extra_task_names():
            pass

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'CHAINED', 'callback': Callback.call_back_function})
        def _function_w_callback(arg1):
            _ = arg1

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'CHAINED', 'callback': Callback.call_back_function2})
        def _function_w_callback_incorrect_args(arg1):
            _ = arg1

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'CHAINED'})
        def _function_wo_callback(arg1):
            _ = arg1
            threadname = threading.current_thread().getName()
            if threadname == 'finished_async_initial_delayed':
                waiter = helpers['1']
            elif threadname == 'finished_async_after_wait_delayed':
                waiter = helpers['2']
            else:
                waiter = helpers['3']
            waiter.wait()

        # Use extra task names, which is not allowed in CHAINED mode
        with self.assertRaises(ValueError) as raise_info:
            _function_w_extra_task_names()
        self.assertIn(member='Extra tasks are not allowed in this mode',
                      container=raise_info.exception.message)

        # Discarding and Queueing of tasks
        global helpers, exceptions
        waiter1 = Waiter(2)
        waiter2 = Waiter(2)
        waiter3 = Waiter(2)
        helpers = {'1': waiter1,
                   '2': waiter2,
                   '3': waiter3}
        exceptions = []

        thread1 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_initial', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread2 = Thread(target=Helpers._execute_delayed_or_inline, name='exception_async', args=(_function_wo_callback, True), kwargs={'arg1': 'arg', 'ensure_single_timeout': 0.1})
        thread3 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_after_wait', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread4 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_other_args', args=(_function_wo_callback, True), kwargs={'arg1': 'other_arg'})
        thread5 = Thread(target=Helpers._execute_delayed_or_inline, name='discarded_wo_callback_async', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread6 = Thread(target=Helpers._execute_delayed_or_inline, name='waited_wo_callback_sync', args=(_function_wo_callback, False), kwargs={'arg1': 'arg'})
        thread7 = Thread(target=Helpers._execute_delayed_or_inline, name='discarded_w_callback_async', args=(_function_w_callback, True), kwargs={'arg1': 'arg'})
        thread8 = Thread(target=Helpers._execute_delayed_or_inline, name='callback_w_callback_sync', args=(_function_w_callback, False), kwargs={'arg1': 'arg'})
        thread9 = Thread(target=Helpers._execute_delayed_or_inline, name='callback_w_callback_sync_incorrect_args', args=(_function_w_callback_incorrect_args, False), kwargs={'arg1': 'arg'})
        thread10 = Thread(target=Helpers._execute_delayed_or_inline, name='exception_wo_callback_sync_timeout', args=(_function_wo_callback, False), kwargs={'arg1': 'arg', 'ensure_single_timeout': 0.1})
        thread11 = Thread(target=Helpers._execute_delayed_or_inline, name='waited_sync_wait_for_async', args=(_function_wo_callback, False), kwargs={'arg1': 'arg'})

        thread1.start()  # Start initial thread and wait for it to be EXECUTING
        Helpers._wait_for(condition=lambda: ('finished_async_initial_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['finished_async_initial_delayed'][0] == 'EXECUTING'))

        thread2.start()  # Start thread2, which should timeout because thread1 is still executing
        Helpers._wait_for(condition=lambda: ('exception_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['exception_async_delayed'][0] == 'EXCEPTION'))

        thread3.start()  # Start thread3, which should be put in the queue, waiting for thread1 to finish
        thread4.start()  # Start thread4 with different params, which should be put in the queue, waiting for thread1 to finish
        Helpers._wait_for(condition=lambda: ('finished_async_after_wait_delayed' in Decorators.unittest_thread_info_by_state['WAITING']))
        Helpers._wait_for(condition=lambda: ('finished_async_other_args_delayed' in Decorators.unittest_thread_info_by_state['WAITING']))

        # At this point, we have 1 task executing and 2 tasks in queue, other tasks should be discarded. (Thread1 being executed and thread3 and thread4 waiting for execution)
        thread5.start()   # Thread5 should be discarded due to identical params (a-sync)
        thread6.start()   # Thread6 should be discarded due to identical params (sync)
        thread7.start()   # Thread7 should be discarded due to identical params and callback should not be executed since its a-sync
        thread8.start()   # Thread8 should be discarded due to identical params and callback should be executed since its sync
        thread9.start()   # Thread9 should be discarded due to identical params and callback should be executed since its sync, but fail due to incorrect arguments
        thread10.start()  # Thread10 should timeout (sync) while trying to wait for the a-sync tasks to complete
        thread11.start()  # Thread11 should wait for the a-sync tasks to complete

        # Make sure every thread is at expected state before letting thread1 finish
        Helpers._wait_for(condition=lambda: ('callback_w_callback_sync' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['callback_w_callback_sync'][0] == 'CALLBACK'))
        Helpers._wait_for(condition=lambda: ('callback_w_callback_sync_incorrect_args' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['callback_w_callback_sync_incorrect_args'][0] == 'CALLBACK'))
        Helpers._wait_for(condition=lambda: ('discarded_w_callback_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['discarded_w_callback_async_delayed'][0] == 'DISCARDED'))
        Helpers._wait_for(condition=lambda: ('discarded_wo_callback_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['discarded_wo_callback_async_delayed'][0] == 'DISCARDED'))
        Helpers._wait_for(condition=lambda: ('waited_wo_callback_sync' in Decorators.unittest_thread_info_by_state['WAITING']))
        Helpers._wait_for(condition=lambda: ('waited_sync_wait_for_async' in Decorators.unittest_thread_info_by_state['WAITING']))

        waiter1.wait()  # Make sure thread1 finishes its task

        # Either thread3 or thread4 should now start executing, the other should still wait in queue
        Helpers._wait_for(condition=lambda: ('finished_async_after_wait_delayed' in Decorators.unittest_thread_info_by_name))
        Helpers._wait_for(condition=lambda: ('finished_async_other_args_delayed' in Decorators.unittest_thread_info_by_name))
        Helpers._wait_for(condition=lambda: (['EXECUTING', 'WAITING'] == sorted([Decorators.unittest_thread_info_by_name['finished_async_after_wait_delayed'][0],
                                                                                 Decorators.unittest_thread_info_by_name['finished_async_other_args_delayed'][0]])))

        # Make sure currently executing thread finishes its task
        if Decorators.unittest_thread_info_by_name['finished_async_after_wait_delayed'][0] == 'EXECUTING':
            waiter2.wait()
        else:
            waiter3.wait()

        Helpers._wait_for(condition=lambda: (['EXECUTING', 'FINISHED'] == sorted([Decorators.unittest_thread_info_by_name['finished_async_after_wait_delayed'][0],
                                                                                  Decorators.unittest_thread_info_by_name['finished_async_other_args_delayed'][0]])))
        # Make sure last executing thread finishes its task
        if Decorators.unittest_thread_info_by_name['finished_async_after_wait_delayed'][0] == 'FINISHED':
            waiter3.wait()
        else:
            waiter2.wait()

        thread3.join()
        thread4.join()
        thread6.join()
        thread9.join()
        thread10.join()
        thread11.join()

        # Validations
        # Validate the individual state for each thread
        for thread_name in ['finished_async_initial_delayed', 'finished_async_after_wait_delayed', 'finished_async_other_args_delayed',
                            'discarded_wo_callback_async_delayed', 'discarded_w_callback_async_delayed',
                            'waited_sync_wait_for_async', 'waited_wo_callback_sync',
                            'exception_async_delayed', 'exception_wo_callback_sync_timeout',
                            'callback_w_callback_sync', 'callback_w_callback_sync_incorrect_args']:
            if thread_name.startswith('finished'):
                value = 'FINISHED'
            elif thread_name.startswith('discarded'):
                value = 'DISCARDED'
            elif thread_name.startswith('waited'):
                value = 'WAITED'
            elif thread_name.startswith('exception'):
                value = 'EXCEPTION'
            else:
                value = 'CALLBACK'

            self.assertIn(member=thread_name,
                          container=Decorators.unittest_thread_info_by_name)
            self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][0],
                             second=value)

            if thread_name == 'exception_async_delayed':
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][1],
                                 second='Could not start within timeout of 0.1s while queued')
            elif thread_name == 'exception_wo_callback_sync_timeout':
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][1],
                                 second='Could not start within timeout of 0.1s while waiting for other tasks')

        # Validate total amount of exceptions, 3 expected (2 timeouts and 1 incorrect callback)
        self.assertEqual(first=len(exceptions),
                         second=3)
        self.assertIn(member='call_back_function2() takes exactly 2 arguments (1 given)',
                      container=exceptions)

        # Validate the expected tasks which should have been waiting at some point
        self.assertListEqual(list1=sorted(Decorators.unittest_thread_info_by_state['WAITING']),
                             list2=sorted(['exception_async_delayed', 'finished_async_after_wait_delayed', 'finished_async_other_args_delayed', 'waited_wo_callback_sync', 'waited_sync_wait_for_async']))

        # Validate initial task has been executed before another in queue with identical params
        self.assertLess(a=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_initial_delayed'),  # Since thread3 waits for thread1 to finish, index should be lower for thread1
                        b=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_after_wait_delayed'))
        # Validate initial task has been executed before another in queue with different params
        self.assertLess(a=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_initial_delayed'),  # Since thread4 waits for thread1 to finish, index should be lower for thread1
                        b=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_other_args_delayed'))

    def test_ensure_single_decorator_deduped(self):
        """
        Tests Helpers._ensure_single functionality in DEDUPED mode
        """
        # DECORATED FUNCTIONS
        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'DEDUPED', 'extra_task_names': []})
        def _function_w_extra_task_names():
            pass

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'DEDUPED', 'callback': Callback.call_back_function})
        def _function_w_callback(arg1):
            _ = arg1
            helpers['waiter'].wait()

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'DEDUPED', 'callback': Callback.call_back_function2})
        def _function_w_callback_incorrect_args(arg1):
            _ = arg1

        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'DEDUPED'})
        def _function_wo_callback(arg1):
            _ = arg1
            if helpers['waiter'] is not None:
                helpers['waiter'].wait()
            elif helpers['event'] is not None:
                count = 0
                while helpers['event'].is_set() is False and count < 500:
                    time.sleep(0.01)
                    count += 1
                    if count == 500:
                        raise Exception('Event was not set in due time')
            else:
                raise ValueError('At least 1 helper needs to be specified')

        # Use extra task names, which is not allowed in DEDUPED mode
        with self.assertRaises(ValueError) as raise_info:
            _function_w_extra_task_names()
        self.assertIn(member='Extra tasks are not allowed in this mode',
                      container=raise_info.exception.message)

        # Discarding and Queueing of tasks
        global helpers, exceptions
        event = Event()
        waiter = Waiter(3)
        helpers = {'waiter': None, 'event': event}
        exceptions = []
        thread1 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_initial', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread2 = Thread(target=Helpers._execute_delayed_or_inline, name='exception_async', args=(_function_wo_callback, True), kwargs={'arg1': 'arg', 'ensure_single_timeout': 0.1})
        thread3 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_after_wait', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread4 = Thread(target=Helpers._execute_delayed_or_inline, name='finished_async_other_args', args=(_function_wo_callback, True), kwargs={'arg1': 'other_arg'})
        thread5 = Thread(target=Helpers._execute_delayed_or_inline, name='discarded_wo_callback_async', args=(_function_wo_callback, True), kwargs={'arg1': 'arg'})
        thread6 = Thread(target=Helpers._execute_delayed_or_inline, name='waited_wo_callback_sync', args=(_function_wo_callback, False), kwargs={'arg1': 'arg'})
        thread7 = Thread(target=Helpers._execute_delayed_or_inline, name='discarded_w_callback_async', args=(_function_w_callback, True), kwargs={'arg1': 'arg'})
        thread8 = Thread(target=Helpers._execute_delayed_or_inline, name='callback_w_callback_sync', args=(_function_w_callback, False), kwargs={'arg1': 'arg'})
        thread9 = Thread(target=Helpers._execute_delayed_or_inline, name='callback_w_callback_sync_incorrect_args', args=(_function_w_callback_incorrect_args, False), kwargs={'arg1': 'arg'})
        thread10 = Thread(target=Helpers._execute_delayed_or_inline, name='exception_wo_callback_sync_timeout', args=(_function_wo_callback, False), kwargs={'arg1': 'arg', 'ensure_single_timeout': 0.1})
        thread11 = Thread(target=Helpers._execute_delayed_or_inline, name='waited_sync_wait_for_async', args=(_function_wo_callback, False), kwargs={'arg1': 'arg'})

        thread1.start()  # Start initial thread and wait for it to be EXECUTING
        Helpers._wait_for(condition=lambda: ('finished_async_initial_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['finished_async_initial_delayed'][0] == 'EXECUTING'))

        thread2.start()  # Start thread2, which should timeout because thread1 is still executing
        Helpers._wait_for(condition=lambda: ('exception_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['exception_async_delayed'][0] == 'EXCEPTION'))

        helpers['waiter'] = waiter
        thread3.start()  # Start thread3, which should be put in the queue, waiting for thread1 to finish
        Helpers._wait_for(condition=lambda: ('finished_async_after_wait_delayed' in Decorators.unittest_thread_info_by_state['WAITING']))

        # At this point, we have 2 tasks in queue, other tasks should be discarded. (Thread1 being executed and thread3 waiting for execution)
        thread4.start()   # Thread4 should succeed because of different params
        thread5.start()   # Thread5 should be discarded due to identical params (a-sync)
        thread6.start()   # Thread6 should be discarded due to identical params (sync)
        thread7.start()   # Thread7 should be discarded due to identical params and callback should not be executed since its a-sync
        thread8.start()   # Thread8 should be discarded due to identical params and callback should be executed since its sync
        thread9.start()   # Thread9 should be discarded due to identical params and callback should be executed since its sync, but fail due to incorrect arguments
        thread10.start()  # Thread10 should timeout (sync) while trying to wait for the a-sync tasks to complete
        thread11.start()  # Thread11 should wait for the a-sync tasks to complete

        # Make sure every thread it at expected state before letting thread1 finish
        Helpers._wait_for(condition=lambda: ('finished_async_other_args_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['finished_async_other_args_delayed'][0] == 'EXECUTING'))
        Helpers._wait_for(condition=lambda: ('callback_w_callback_sync_incorrect_args' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['callback_w_callback_sync_incorrect_args'][0] == 'CALLBACK'))
        Helpers._wait_for(condition=lambda: ('callback_w_callback_sync' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['callback_w_callback_sync'][0] == 'CALLBACK'))
        Helpers._wait_for(condition=lambda: ('discarded_w_callback_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['discarded_w_callback_async_delayed'][0] == 'DISCARDED'))
        Helpers._wait_for(condition=lambda: ('discarded_wo_callback_async_delayed' in Decorators.unittest_thread_info_by_name and Decorators.unittest_thread_info_by_name['discarded_wo_callback_async_delayed'][0] == 'DISCARDED'))
        Helpers._wait_for(condition=lambda: ('waited_wo_callback_sync' in Decorators.unittest_thread_info_by_state['WAITING']))
        Helpers._wait_for(condition=lambda: ('waited_sync_wait_for_async' in Decorators.unittest_thread_info_by_state['WAITING']))

        # Important difference between DEDUPED and CHAINED: Tasks with different params will run simultaneously, so both 'initial' and 'other_args' should be executing at this point
        self.assertEqual(first=Decorators.unittest_thread_info_by_name['finished_async_initial_delayed'][0],
                         second='EXECUTING')
        self.assertEqual(first=Decorators.unittest_thread_info_by_name['finished_async_other_args_delayed'][0],
                         second='EXECUTING')

        event.set()  # Make sure thread1 now finishes, so thread3 can start executing
        waiter.wait()

        thread3.join()
        thread4.join()
        thread6.join()
        thread9.join()
        thread10.join()
        thread11.join()

        # Validations
        # Validate the individual state for each thread
        for thread_name in ['finished_async_initial_delayed', 'finished_async_after_wait_delayed', 'finished_async_other_args_delayed',
                            'discarded_wo_callback_async_delayed', 'discarded_w_callback_async_delayed',
                            'waited_sync_wait_for_async', 'waited_wo_callback_sync',
                            'exception_async_delayed', 'exception_wo_callback_sync_timeout',
                            'callback_w_callback_sync', 'callback_w_callback_sync_incorrect_args']:
            if thread_name.startswith('finished'):
                value = 'FINISHED'
            elif thread_name.startswith('discarded'):
                value = 'DISCARDED'
            elif thread_name.startswith('waited'):
                value = 'WAITED'
            elif thread_name.startswith('exception'):
                value = 'EXCEPTION'
            else:
                value = 'CALLBACK'

            self.assertIn(member=thread_name,
                          container=Decorators.unittest_thread_info_by_name)
            self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][0],
                             second=value)

            if thread_name == 'exception_async_delayed':
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][1],
                                 second='Could not start within timeout of 0.1s while queued')
            elif thread_name == 'exception_wo_callback_sync_timeout':
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name][1],
                                 second='Could not start within timeout of 0.1s while waiting for other tasks')

        # Validate total amount of exceptions, 3 expected (2 timeouts and 1 incorrect callback)
        self.assertEqual(first=len(exceptions),
                         second=3)
        self.assertIn(member='call_back_function2() takes exactly 2 arguments (1 given)',
                      container=exceptions)

        # Validate the expected tasks which should have been waiting at some point
        self.assertListEqual(list1=sorted(Decorators.unittest_thread_info_by_state['WAITING']),
                             list2=sorted(['exception_async_delayed', 'finished_async_after_wait_delayed', 'waited_wo_callback_sync', 'waited_sync_wait_for_async']))

        # Validate initial task has been executed before another in queue with identical params
        self.assertLess(a=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_initial_delayed'),  # Since thread3 waits for thread1 to finish, index should be lower for thread1
                        b=Decorators.unittest_thread_info_by_state['FINISHED'].index('finished_async_after_wait_delayed'))

    def test_ensure_single_decorator_default(self):
        """
        Tests Helpers._ensure_single functionality in DEFAULT mode
        Whether the first task is being executed sync or async, the 2nd will always be discarded
        This is because the 2nd task is being executed by another worker (or mocked to invoke this behavior)
        """
        # DECORATED FUNCTIONS
        @ovs_task(name='unittest_task1', ensure_single_info={'mode': 'DEFAULT'})
        def _function(arg1):
            _ = arg1
            helpers['waiter'].wait()

        @ovs_task(name='unittest_task2', ensure_single_info={'mode': 'DEFAULT', 'callback': Callback.call_back_function})
        def _function_w_callback(arg1):
            _ = arg1
            helpers['waiter'].wait()

        @ovs_task(name='unittest_task3', ensure_single_info={'mode': 'DEFAULT', 'extra_task_names': ['unittest_task1']})
        def _function_w_extra_task_names(arg1):
            _ = arg1
            helpers['waiter'].wait()

        # | Task 1 Sync | Task 2 Sync | Callback |
        # |    False    |    False    |   No CB  |
        # |    True     |    False    |   No CB  |
        # |    False    |    True     |   No CB  |
        # |    True     |    True     |   No CB  |
        # |    False    |    False    |    CB    |
        # |    True     |    False    |    CB    |
        # |    False    |    True     |    CB    |
        # |    True     |    True     |    CB    |
        global helpers, exceptions
        counter = 0
        helpers = {'waiter': None}
        exceptions = []
        for task1_delayed, task2_delayed, func in [(False, False, _function),
                                                   (True, False, _function),
                                                   (False, True, _function),
                                                   (True, True, _function),
                                                   (False, False, _function_w_callback),
                                                   (True, False, _function_w_callback),
                                                   (False, True, _function_w_callback),
                                                   (True, True, _function_w_callback)]:
            waiter = Waiter(2)
            helpers['waiter'] = waiter
            thread1 = Thread(target=Helpers._execute_delayed_or_inline, name='unittest_thread1', args=(func, task1_delayed), kwargs={'arg1': 'arg1'})
            thread2 = Thread(target=Helpers._execute_delayed_or_inline, name='unittest_thread2', args=(func, task2_delayed), kwargs={'arg1': 'arg2'})

            thread1.start()
            while waiter.get_counter() < 1:  # Wait for thread1 to be waiting, which will increase the counter to 1
                time.sleep(0.01)
            thread2.start()
            thread2.join()
            waiter.wait()
            thread1.join()

            # Validate
            thread_name_1 = thread1.name if task1_delayed is False else '{0}_delayed'.format(thread1.name)
            thread_name_2 = thread2.name if task2_delayed is False else '{0}_delayed'.format(thread2.name)
            for thread_name in [thread_name_1, thread_name_2]:
                self.assertIn(member=thread_name,
                              container=Decorators.unittest_thread_info_by_name)
            self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_1][0],
                             second='FINISHED')
            if func == _function or task2_delayed is True:  # Callback function is only executed when waiting task is executed non-delayed
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_2][0],
                                 second='DISCARDED')
            else:
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_2][0],
                                 second='CALLBACK')
            counter += 1
            Decorators._clean()

        # | Task 1 Sync | Task 2 Sync | Function order  |
        # |    False    |    False    | func 1 - func 2 |
        # |    True     |    False    | func 1 - func 2 |
        # |    False    |    True     | func 1 - func 2 |
        # |    True     |    True     | func 1 - func 2 |
        # |    False    |    False    | func 2 - func 1 |
        # |    True     |    False    | func 2 - func 1 |
        # |    False    |    True     | func 2 - func 1 |
        # |    True     |    True     | func 2 - func 1 |
        for task1_delayed, task2_delayed, first_func, second_func in [(False, False, _function, _function_w_extra_task_names),
                                                                      (True, False, _function, _function_w_extra_task_names),
                                                                      (False, True, _function, _function_w_extra_task_names),
                                                                      (True, True, _function, _function_w_extra_task_names),
                                                                      (False, False, _function_w_extra_task_names, _function),
                                                                      (True, False, _function_w_extra_task_names, _function),
                                                                      (False, True, _function_w_extra_task_names, _function),
                                                                      (True, True, _function_w_extra_task_names, _function)]:
            waiter = Waiter(2)
            helpers['waiter'] = waiter
            thread1 = Thread(target=Helpers._execute_delayed_or_inline, name='unittest_thread1', args=(first_func, task1_delayed), kwargs={'arg1': 'arg1'})
            thread2 = Thread(target=Helpers._execute_delayed_or_inline, name='unittest_thread2', args=(second_func, task2_delayed), kwargs={'arg1': 'arg2'})

            thread1.start()
            while waiter.get_counter() < 1:  # Wait for thread1 to be waiting, which will increase the counter to 1
                time.sleep(0.01)
            thread2.start()
            thread2.join()
            waiter.wait()
            thread1.join()

            # Validate
            thread_name_1 = thread1.name if task1_delayed is False else '{0}_delayed'.format(thread1.name)
            thread_name_2 = thread2.name if task2_delayed is False else '{0}_delayed'.format(thread2.name)
            for thread_name in [thread_name_1, thread_name_2]:
                self.assertIn(member=thread_name,
                              container=Decorators.unittest_thread_info_by_name)
            self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_1][0],
                             second='FINISHED')
            if first_func == _function:  # Function 1 called first, so should have been completed, starting function 2 should be discarded due to 'extra_task_names'
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_2][0],
                                 second='DISCARDED')
            else:  # Function 2 called first, which should not block the execution of another function: function 1
                self.assertEqual(first=Decorators.unittest_thread_info_by_name[thread_name_2][0],
                                 second='FINISHED')
            Decorators._clean()

    def test_ensure_single_decorator_exceptions(self):
        """
        Tests Helpers._ensure_single decorator basic exception handling
        """
        # Use unsupported mode
        @ovs_task(name='unittest_task', ensure_single_info={'mode': 'UNKNOWN'})
        def _function1():
            pass

        with self.assertRaises(ValueError) as raise_info:
            _function1()
        self.assertEqual(first=raise_info.exception.message,
                         second='Unsupported mode "UNKNOWN" provided')

        # Use ensure_single without bind=True
        @_ensure_single(task_name='unittest_task', mode='DEFAULT')
        def _function1():
            pass

        with self.assertRaises(RuntimeError) as raise_info:
            _function1('no_valid_celery_request_object')
        self.assertEqual(first=raise_info.exception.message,
                         second='The decorator ensure_single can only be applied to bound tasks (with bind=True argument)')


class Callback(object):
    """
    Class containing call back functions used by the _ensure_single decorator
    """
    @staticmethod
    def call_back_function(arg1):
        """
        Call back function (Needs to have identical parameters as first function trying to be executed)
        """
        _ = arg1

    @staticmethod
    def call_back_function2(arg1, arg2):
        """
        Call back function with different amount of required arguments, to invoke error in unittests
        """
        _ = arg1, arg2
