import logging
import ovn_stats
import time
import asyncio
from collections import Counter

log = logging.getLogger(__name__)

active_context = None


ITERATION_STAT_NAME = 'Iteration Total'


class Context(object):
    def __init__(self, test_name, max_iterations=None, brief_report=False,
                 test=None):
        self.iteration = -1
        self.test_name = test_name
        self.max_iterations = max_iterations
        self.brief_report = brief_report
        self.test = test
        self.iterations = dict()
        if self.max_iterations is None:
            self.iteration_singleton = ContextIteration(0, self)
        else:
            self.iteration_singleton = None

    def __enter__(self):
        global active_context
        log.info(f'Entering context {self.test_name}')
        ovn_stats.clear()
        active_context = self
        return self

    def __exit__(self, type, value, traceback):
        ovn_stats.report(self.test_name, brief=self.brief_report)
        log.info(f'Exiting context {self.test_name}')

    async def iteration_started(self, iteration):
        '''Explicitly begin iteration 'n'. This is necessary when running
        asynchronously since task starts and ends can overlap.'''
        iteration.start()
        log.info(f'Context {self.test_name}, Iteration {iteration.num}')
        if self.test:
            await self.test.exec_cmd(iteration.num, self.test_name)

    def iteration_completed(self, iteration):
        ''' Explicitly end iteration 'n'. This is necessary when running
        asynchronously since task starts and ends can overlap.'''
        iteration.end()
        duration = iteration.end_time - iteration.start_time
        ovn_stats.add(ITERATION_STAT_NAME, duration, iteration.failed,
                      iteration)
        self.iterations = {task_name: it for task_name, it in
                           self.iterations.items() if it != iteration}
        if self.test:
            self.test.terminate_process(self.test_name)
        log.log(logging.WARNING if iteration.failed else logging.INFO,
                f'Context {self.test_name}, Iteration {iteration.num}, '
                f'Result: {"FAILURE" if iteration.failed else "SUCCESS"}')

    def all_iterations_completed(self):
        if self.iteration_singleton is not None:
            # Weird, but you do you man.
            self.iteration_completed(self.iteration_singleton)
            return

        # self.iterations may have the same iteration value for multiple
        # keys. We need to first get the unique list of iterations.
        iter_list = Counter(self.iterations.values())
        for iteration in iter_list:
            self.iteration_completed(iteration)

    def create_task(self, coro, iteration=None):
        '''Create a task to run in this context.'''
        if iteration is None:
            iteration = get_current_iteration()
        task = asyncio.create_task(coro)
        self.iterations[task.get_name()] = iteration
        return task

    async def qps_test(self, qps, coro, *args, **kwargs):
        tasks = []
        for i in range(self.max_iterations):
            iteration = ContextIteration(i, self)
            tasks.append(self.create_task(
                self.qps_task(iteration, coro, *args, **kwargs), iteration)
            )
            # Use i+1 so that we don't sleep on task 0 and so that
            # we sleep after 20 iterations instead of 21.
            if (i + 1) % qps == 0:
                await asyncio.sleep(1)
        await asyncio.gather(*tasks)

    async def qps_task(self, iteration, coro, *args, **kwargs):
        await self.iteration_started(iteration)
        await coro(*args)
        if kwargs.get('end_iteration', True):
            self.iteration_completed(iteration)


def get_current_iteration():
    ctx = active_context
    if ctx.iteration_singleton is not None:
        return ctx.iteration_singleton

    cur_task = asyncio.current_task()
    return active_context.iterations[cur_task.get_name()]


class ContextIteration(object):
    def __init__(self, num, context):
        self.num = num
        self.context = context
        self.start_time = None
        self.end_time = None
        self.failed = False

    def fail(self):
        self.failed = True

    def start(self):
        self.start_time = time.perf_counter()

    def end(self):
        self.end_time = time.perf_counter()
